#!/usr/bin/env python3
"""EXP-026 harness -- news-protection min-lot fallback: **SKIP vs CLOSE_ALL**.

The question EXP-025 §6(iii) and EXP-024 §8(2) both deferred, now measurable
against the real historical calendar. Family: EXP-023's "news-protection min-lot
fallback" (cumulative config count continues from its 20; its one-touch Test
budget is UNSPENT as of this file).

MECHANISM UNDER TEST (one, and only one): live's
`watchman/loop.py::_act_on_news_decision` degenerates `CLOSE_HALF_AND_BREAKEVEN`
into `CLOSE_ALL` whenever half the position's volume rounds below the broker's
`volume_min` -- a fail-direction the code comment itself justifies on instinct
("closing the WHOLE position instead (still risk-reducing) rather than skipping
protection entirely"), never on evidence. Candidate **B_skip**: when, and ONLY
when, half-lot < `volume_min`, SKIP the protection action entirely (position
untouched, stop unmoved, no suppression window recorded, re-checked on the next
trigger exactly as live's ~5s loop would). Trades whose half-lot IS a valid lot
behave IDENTICALLY in both arms -- the engine's genuine partial-close branch runs
untouched in both -- so the treatment difference is confined, by construction, to
min-lot trades.

WHY THE REAL ENGINE AND NOT THE exp023/024/025 HARNESS FAMILY: per the
2026-08-04 log NOTE, those harnesses model the min-lot CLOSE_ALL degeneration
ONLY and never the genuine partial close, so their arm-level sequences diverge
from live for every larger-than-min-lot trade. This file therefore drives
`backtest.engine.run_backtest` itself and installs B_skip as a MONKEYPATCHED
DECISION SEAM over `engine._act_on_news_decision` -- nothing under `src/` or
`config/` is modified. The conditional (sequence-held-fixed) replay likewise
calls the engine's own `check_exit` / `_step_news_protection` /
`evaluate_watchman` / `_close_trade` per bar rather than reimplementing any of
them; only the bar-loop skeleton (which has no decision content) is local.

D3 (inherited from EXP-023, still binding): the DECIDING metric is trade-matched
and conditional with the sequence held FIXED, because `max_positions_per_symbol:
1` means a later exit reshuffles which signal is taken next and that reshuffling
noise dominates portfolio deltas (EXP-017/020/021). Full-sequence portfolio runs
are reported as VETO-ONLY evidence.

MODES:
  --mode fidelity     fast-path identity + y4 external anchors + arm-A conditional-replay self-check
  --mode conditional  THE DECIDING RUN: per-window treated subset, paired B-A, tails
  --mode portfolio    full-sequence A_live / B_skip / C per window (veto-only)
  --mode pool         pool `--mode conditional` output into Train / Val statistics

The Test year is REFUSED (`y5*`) unless `--allow-test` is passed, which is only
legitimate once every pre-registered acceptance bar has cleared on Train+Val.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from autotrade.backtest.historical_news_calendar import HistoricalNewsCalendarProvider  # noqa: E402
from autotrade.backtest.report import generate_report  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402
from autotrade.watchman.news_protection import NewsProtectionConfig  # noqa: E402

import exp022_minlot_harness as e22  # noqa: E402

SYMBOL = "XAUUSD"
CALENDAR = HISTORICAL_DIR / "news_calendar_backtest.csv"
EQUITY = 3000.0
COMMISSION = 0.0
YEARS = dict((name, (a, b)) for name, a, b in e22.YEARS)
TRAIN = ("y1_2021-22", "y2_2022-23", "y3_2023-24")
VAL = ("y4_VAL_2024-25",)

_REAL_ACT = engine_mod._act_on_news_decision
_REAL_OPEN_POSITION = engine_mod._OpenPosition


# ------------------------------------------------------------------ arms
class MinLotSeam:
    """The ONE behavioural difference between the arms, installed over
    `engine._act_on_news_decision`. `skip=False` reproduces live/current
    behaviour byte-for-byte (it delegates unconditionally); `skip=True` returns
    `None` for the min-lot case only, which `engine._step_news_protection`
    propagates as "no news action this bar" -- so the position is left
    untouched, its stop unmoved, no `news_protected_until` recorded, and the
    engine falls through to `evaluate_watchman` exactly as on a NO_ACTION bar.
    Also COUNTS min-lot degenerations, which is how the treated subset is
    identified (identically in both arms)."""

    def __init__(self, skip: bool):
        self.skip = skip
        self.min_lot_events = 0
        self.partial_events = 0

    def __call__(self, symbol, position, decision, candidate_price, now, bar_time,
                 symbol_spec, point_value, cost_model, config):
        degenerate = (
            decision.action != "CLOSE_ALL"
            and engine_mod._half_lot_rounded(position.lot_size, symbol_spec) is None
        )
        if degenerate:
            self.min_lot_events += 1
            if self.skip:
                return None
        else:
            self.partial_events += 1
        return _REAL_ACT(symbol, position, decision, candidate_price, now, bar_time,
                         symbol_spec, point_value, cost_model, config)


class _Recorder:
    """Captures every position at ENTRY (constructor kwargs, before any
    mutation) so the conditional replay can re-live the identical position."""

    def __init__(self):
        self.entries: list[dict] = []

    def __call__(self, **kw):
        self.entries.append(dict(kw))
        return _REAL_OPEN_POSITION(**kw)


# ------------------------------------------------------------------ setup
def load_window(window: str) -> pd.DataFrame:
    start, end = YEARS[window]
    df = pd.read_csv(HISTORICAL_DIR / f"{SYMBOL}_H1.csv", parse_dates=["time"])
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end) + pd.Timedelta(hours=23))]
    return df.reset_index(drop=True)


def build_cfg(cfg, *, news: bool) -> BacktestConfig:
    rv_cfg, wm_cfg, sh_cfg, order = e22.build_cfgs(cfg)
    np_cfg = calendar = None
    if news:
        np_cfg = NewsProtectionConfig(
            news_window_minutes=cfg["watchman"]["news_window_minutes"],
            profit_threshold_r=cfg["watchman"]["news_profit_threshold_r"],
            close_mode=cfg["watchman"]["news_close_mode"],
        )
        calendar = HistoricalNewsCalendarProvider(CALENDAR)
    return BacktestConfig(
        starting_equity=EQUITY,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=COMMISSION, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=e22.SWAP_LONG,
                                       short_per_lot_per_night=e22.SWAP_SHORT),
        ),
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        news_protection_cfg=np_cfg, news_calendar=calendar,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    )


def run_full(df, bt, *, seam: MinLotSeam | None, recorder: _Recorder | None):
    engine_mod._act_on_news_decision = seam if seam is not None else _REAL_ACT
    engine_mod._OpenPosition = recorder if recorder is not None else _REAL_OPEN_POSITION
    try:
        return run_backtest(df, SYMBOL, e22._SPEC, bt)
    finally:
        engine_mod._act_on_news_decision = _REAL_ACT
        engine_mod._OpenPosition = _REAL_OPEN_POSITION


# ------------------------------------------------------- conditional replay
def replay_one(df, i0: int, kw: dict, bt: BacktestConfig, bar_span: float, seam: MinLotSeam):
    """Re-live ONE already-opened position with everything held fixed (same
    entry bar, fill, lot, metadata, subsequent bars) under the installed seam.
    Every decision comes from the engine's own functions; only the bar-loop
    skeleton is local (it is the engine's, minus the signal/pending branch that
    would reshuffle the sequence)."""
    point_value = e22._SPEC.tick_value / e22._SPEC.tick_size
    kw = dict(kw)
    kw["metadata"] = replace(kw["metadata"]) if kw["metadata"] is not None else None
    pos = _REAL_OPEN_POSITION(**kw)
    out = []
    engine_mod._act_on_news_decision = seam
    try:
        for i in range(i0, len(df)):
            bar = df.iloc[i]
            ex = engine_mod.check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar)
            if ex is not None:
                price, reason = ex
                out.append(engine_mod._close_trade(
                    SYMBOL, pos, pd.Timestamp(bar["time"]), price, reason, point_value, bt.cost_model))
                return out
            news_result = engine_mod._step_news_protection(
                SYMBOL, pos, bar, bar_span, e22._SPEC, point_value, bt)
            if news_result is not None:
                trade, still_open = news_result
                out.append(trade)
                if not still_open:
                    return out
            elif bt.watchman_cfg is not None and pos.metadata is not None:
                decision = engine_mod.evaluate_watchman(
                    position_metadata=pos.metadata, current_sl=pos.current_sl,
                    current_price=float(bar["close"]), current_atr=engine_mod._watchman_current_atr(df, i),
                    df=df, as_of_index=i, now=pd.Timestamp(bar["time"]).to_pydatetime(), config=bt.watchman_cfg,
                )
                if decision.action == "CLOSE":
                    out.append(engine_mod._close_trade(
                        SYMBOL, pos, pd.Timestamp(bar["time"]), float(bar["close"]),
                        engine_mod._classify_watchman_exit_reason(decision.reason), point_value, bt.cost_model))
                    return out
                if decision.action == "MODIFY_SL":
                    pos.current_sl = decision.new_stop_loss
        last = df.iloc[-1]
        out.append(engine_mod._close_trade(
            SYMBOL, pos, pd.Timestamp(last["time"]), last["close"], "end_of_data", point_value, bt.cost_model))
        return out
    finally:
        engine_mod._act_on_news_decision = _REAL_ACT


def position_r(records, kw) -> float:
    """Total R of one POSITION = sum of the net P&L of all its records
    (a genuine partial close emits two) over the ORIGINAL risk amount."""
    point_value = e22._SPEC.tick_value / e22._SPEC.tick_size
    risk = kw["plan"].stop_distance * point_value * kw["lot_size"]
    return sum(t.net_pnl for t in records) / risk if risk else 0.0


# ------------------------------------------------------------------ stats
def _pf(vals):
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    if not vals:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)


def portfolio_metrics(trades) -> dict:
    rep = generate_report(trades, EQUITY)
    rs = [t.r_multiple for t in trades]
    return {
        "trades": rep.trade_count,
        "PF": None if rep.profit_factor is None else round(rep.profit_factor, 4),
        "net$": round(rep.total_net_pnl, 2),
        "maxDD%": None if rep.max_drawdown_pct is None else round(rep.max_drawdown_pct, 4),
        "avgR": None if rep.avg_r_multiple is None else round(rep.avg_r_multiple, 4),
        "pf_ex5": None if rep.profit_factor_excluding_top_5 is None else round(rep.profit_factor_excluding_top_5, 4),
        "win_rate": None if rep.win_rate is None else round(rep.win_rate, 4),
        "loser_rate": round(sum(1 for r in rs if r < 0) / len(rs), 4) if rs else None,
        "worstR": round(min(rs), 4) if rs else None,
        # CONTEXT ONLY, added after the treated-subset numbers were read and
        # disclosed as such in the log: the book's OWN rate of losses worse
        # than a clean stop-out, i.e. the denominator a reader needs to judge
        # criterion (c3)'s absolute 5% bar. It changes no pre-registered bar.
        "frac_below_minus1R": round(sum(1 for r in rs if r < -1.0) / len(rs), 4) if rs else None,
        "news_exits": sum(1 for t in trades if t.exit_reason == "news_protection"),
    }


def paired(a: list[float], b: list[float]) -> dict:
    d = [y - x for x, y in zip(a, b)]
    n = len(d)
    if n < 2:
        return {"n": n}
    mean = sum(d) / n
    sd = statistics.stdev(d)
    se = sd / math.sqrt(n)
    return {
        "n": n, "mean_d": round(mean, 4), "se": round(se, 4),
        "t": round(mean / se, 3) if se else None,
        "sum_d": sum(d), "sum_d2": sum(x * x for x in d),
        "n_d_neg": sum(1 for x in d if x < 0), "n_d_pos": sum(1 for x in d if x > 0),
        "d_p05": round(sorted(d)[max(0, int(0.05 * n) - 1)], 4),
        "worst_d": round(min(d), 4),
    }


def subset_tail(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs), "avgR": round(sum(rs) / len(rs), 4), "medR": round(statistics.median(rs), 4),
        "PF": _pf(rs), "worstR": round(min(rs), 4),
        "n_neg": sum(1 for r in rs if r < 0),
        "frac_neg": round(sum(1 for r in rs if r < 0) / len(rs), 4),
        "n_below_minus1R": sum(1 for r in rs if r < -1.0),
        "frac_below_minus1R": round(sum(1 for r in rs if r < -1.0) / len(rs), 4),
    }


# ------------------------------------------------------------------ modes
def _guard(window: str, allow_test: bool) -> None:
    if window.startswith("y5") and not allow_test:
        raise SystemExit(
            "REFUSED: the Test year is out of scope until every pre-registered acceptance bar has "
            "cleared on Train+Val; re-run with --allow-test only then."
        )


def mode_fidelity(args) -> int:
    cfg = load_yaml_config("base")
    out = {}

    # (1) fast-path shim identity, news OFF and ON, on a 4,000-bar slice.
    df = load_window("y4_VAL_2024-25").iloc[:4000].reset_index(drop=True)
    for label, news in (("newsOFF", False), ("newsON", True)):
        bt = build_cfg(cfg, news=news)
        slow = run_full(df, bt, seam=None, recorder=None)
        e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
        try:
            fast = run_full(df, bt, seam=None, recorder=None)
        finally:
            e22.uninstall_fast_path()
        out[f"fastpath_{label}"] = {"n": len(slow), "identical": [asdict(t) for t in slow] == [asdict(t) for t in fast]}

    # (2) external anchors on y4/VAL + (3) arm-A conditional-replay self-check.
    dfy4 = load_window("y4_VAL_2024-25")
    e22.install_fast_path(dfy4, cfg["global"]["swing_pivot_bars"])
    try:
        btc = build_cfg(cfg, news=False)
        out["anchor_y4_C"] = portfolio_metrics(run_full(dfy4, btc, seam=None, recorder=None))
        bta = build_cfg(cfg, news=True)
        seam_a = MinLotSeam(skip=False)
        rec = _Recorder()
        full_a = run_full(dfy4, bta, seam=seam_a, recorder=rec)
        out["anchor_y4_A"] = portfolio_metrics(full_a)
        out["y4_A_seam_counts"] = {"min_lot": seam_a.min_lot_events, "partial": seam_a.partial_events}

        idx = {pd.Timestamp(t): i for i, t in enumerate(dfy4["time"])}
        span = engine_mod._infer_bar_span_minutes(dfy4)
        rebuilt = []
        for kw in rec.entries:
            rebuilt.extend(replay_one(dfy4, idx[pd.Timestamp(kw["entry_time"])], kw, bta, span, MinLotSeam(skip=False)))
        out["replay_selfcheck_y4_A"] = {
            "n_full": len(full_a), "n_replay": len(rebuilt),
            "identical": [asdict(t) for t in full_a] == [asdict(t) for t in rebuilt],
        }
    finally:
        e22.uninstall_fast_path()

    print("FIDELITY " + json.dumps(out, default=str))
    ok = (out["fastpath_newsOFF"]["identical"] and out["fastpath_newsON"]["identical"]
          and out["replay_selfcheck_y4_A"]["identical"])
    return 0 if ok else 1


def mode_conditional(args) -> int:
    _guard(args.window, args.allow_test)
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        bt = build_cfg(cfg, news=True)
        span = engine_mod._infer_bar_span_minutes(df)
        seam_a = MinLotSeam(skip=False)
        rec = _Recorder()
        full_a = run_full(df, bt, seam=seam_a, recorder=rec)
        idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}

        rebuilt, rows = [], []
        for kw in rec.entries:
            i0 = idx[pd.Timestamp(kw["entry_time"])]
            sa = MinLotSeam(skip=False)
            ra = replay_one(df, i0, kw, bt, span, sa)
            rebuilt.extend(ra)
            sb = MinLotSeam(skip=True)
            rb = replay_one(df, i0, kw, bt, span, sb)
            rows.append({
                "entry": str(kw["entry_time"]), "lot": kw["lot_size"],
                "min_lot_events_A": sa.min_lot_events, "partial_events_A": sa.partial_events,
                "rA": position_r(ra, kw), "rB": position_r(rb, kw),
                "exitA": ra[-1].exit_reason, "exitB": rb[-1].exit_reason,
            })
        selfcheck = [asdict(t) for t in full_a] == [asdict(t) for t in rebuilt]
        assert selfcheck, "conditional-replay self-check FAILED -- arm-A replay != full sequence"

        treated = [r for r in rows if r["min_lot_events_A"] > 0]
        untouched_ok = all(abs(r["rA"] - r["rB"]) < 1e-9 for r in rows if r["min_lot_events_A"] == 0)
        out = {
            "window": args.window, "positions": len(rows), "trade_records_A": len(full_a),
            "treated_n": len(treated), "treated_pct": round(100.0 * len(treated) / len(rows), 2) if rows else None,
            "seam_counts_A": {"min_lot": seam_a.min_lot_events, "partial": seam_a.partial_events},
            "replay_selfcheck": selfcheck,
            "untouched_identical": untouched_ok,
            "A_treated": subset_tail([r["rA"] for r in treated]),
            "B_treated": subset_tail([r["rB"] for r in treated]),
            "paired": paired([r["rA"] for r in treated], [r["rB"] for r in treated]),
            "portfolio_loser_rate_A": round(
                sum(1 for t in full_a if t.r_multiple < 0) / len(full_a), 4) if full_a else None,
            "portfolio_worstR_A": round(min((t.r_multiple for t in full_a), default=0.0), 4),
            "exit_mix_A": _mix([r["exitA"] for r in treated]),
            "exit_mix_B": _mix([r["exitB"] for r in treated]),
        }
        print("COND " + json.dumps(out, default=str))
        for r in treated:
            print("TRT " + json.dumps(r, default=str))
    finally:
        e22.uninstall_fast_path()
    return 0


def _mix(reasons: list[str]) -> dict:
    m: dict[str, int] = {}
    for r in reasons:
        m[r] = m.get(r, 0) + 1
    return dict(sorted(m.items(), key=lambda kv: -kv[1]))


def mode_portfolio(args) -> int:
    _guard(args.window, args.allow_test)
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        for arm in ("C", "A_live", "B_skip"):
            bt = build_cfg(cfg, news=(arm != "C"))
            seam = None if arm == "C" else MinLotSeam(skip=(arm == "B_skip"))
            trades = run_full(df, bt, seam=seam, recorder=None)
            row = {"window": args.window, "arm": arm, **portfolio_metrics(trades)}
            if seam is not None:
                row["seam_counts"] = {"min_lot": seam.min_lot_events, "partial": seam.partial_events}
            print("PORT " + json.dumps(row, default=str))
    finally:
        e22.uninstall_fast_path()
    return 0


def mode_pool(args) -> int:
    rows = [json.loads(l[5:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
            if l.startswith("COND ")]

    def _pool(recs):
        n = sum(r["paired"]["n"] for r in recs)
        if n < 2:
            return {"n": n}
        sd = sum(r["paired"]["sum_d"] for r in recs)
        sd2 = sum(r["paired"]["sum_d2"] for r in recs)
        mean = sd / n
        var = (sd2 - n * mean * mean) / (n - 1)
        se = math.sqrt(max(var, 0.0) / n)
        return {"n": n, "mean_d": round(mean, 4), "se": round(se, 4),
                "t": round(mean / se, 3) if se else None,
                "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)],
                "bar_1.7se": round(1.7 * se, 4), "clears_1.7se": bool(se and mean > 1.7 * se)}

    out = {
        "TRAIN": _pool([r for r in rows if r["window"] in TRAIN]),
        "VAL": _pool([r for r in rows if r["window"] in VAL]),
        "per_window": {r["window"]: r["paired"] for r in rows},
        "treated_n": {r["window"]: r["treated_n"] for r in rows},
    }
    print("POOL " + json.dumps(out, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["fidelity", "conditional", "portfolio", "pool"])
    p.add_argument("--window", default="y1_2021-22", choices=sorted(YEARS))
    p.add_argument("--allow-test", action="store_true")
    p.add_argument("--infile", default="experiments/exp026_cond_out.txt")
    args = p.parse_args()
    return {"fidelity": mode_fidelity, "conditional": mode_conditional,
            "portfolio": mode_portfolio, "pool": mode_pool}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
