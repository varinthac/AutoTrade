#!/usr/bin/env python3
"""EXP-027 harness -- MEASUREMENT of Risk Voice's news ENTRY blackout against the
real historical calendar, and of the first FULL news-parity (2x2) grid this
project has ever had.

Family: "news-entry-blackout parity" (NEW; sibling of EXP-024's
"news-protection backtest/live parity"). **MEASUREMENT, not selection**: there
is no grid over any parameter, no candidate, no winner, and no `config/base.yaml`
or `src/` change can follow from it directly. Nothing here may spend any family's
one-touch Test budget -- the Test year is REFUSED unconditionally (there is no
`--allow-test` escape hatch in this file, deliberately).

THE 2x2 (two independent, separately-wired news mechanisms; both now modelable):
    C0  blackout OFF, protection OFF  = every historical baseline in the log
    E   blackout ON,  protection OFF  = the entry mechanism alone
    P   blackout OFF, protection ON   = EXP-024's engine "A@real"
    EP  blackout ON,  protection ON   = full news parity (closest to live)

MEASUREMENTS (per window and pooled Train / Val separately, rule 6):
 (a) entry-blackout hit rate on the C0 trade population + the vetoed population's
     COUNTERFACTUAL outcomes under C0 (what the blocked entries actually did) --
     the deciding descriptive.
 (b) portfolio deltas E-C0 and EP-P (and, free, P-C0 / EP-E).
 (c) interaction/overlap: are the vetoed entries the same trades Watchman's news
     PROTECTION would later have truncated anyway? (sequence-fixed replay of each
     C0 position with protection ON.)
 (d) reconciliation of the entry diagnostic's predicted ~10-13% trade-count
     reduction against the engine's informational ~5.1% -- decomposed into
     (arm/denominator) + (clock convention) + (slot dynamics), quantified.

CLOCK CONVENTIONS (this is a real methodological choice and is measured, not
assumed). `backtest/engine.py` sets its `SimulatedClock` to the SIGNAL bar's OPEN
time `t`, so the modeled blackout vetoes a signal iff some high-impact USD event
`e` satisfies `t-30min <= e <= t+45min`. Live (`orchestrator/shadow_loop.py`)
evaluates `check_risk_voice` when the H1 bar CLOSES -- i.e. at `~t+60min`, which
is also the instant the backtest fills at (next bar's open) -- so live's veto
condition on the same signal is `t+30min <= e <= t+105min`. This file reports the
vetoed population under BOTH conventions ("signal-open" = the engine's, the one
the portfolio cells actually use; "fill-time" = live's), because the two select
overlapping-but-different bars. No `src/` change is made here (rule: a
measurement experiment changes nothing).

MODES (one window per invocation -- keeps every call well under the 600s cap):
  --mode fidelity --scope fastpath   fast-path shim identity for all 4 cells
  --mode fidelity --scope anchor     C0 + P recorded-row anchors for --window
  --mode fidelity --scope replay     conditional-replay self-check on C0 (--window)
  --mode grid                        the 4 cells, full sequence, for --window
  --mode veto                        (a)+(c)+(d) per-position rows for --window
  --mode pool --infile               pool VETO rows into Train / Val statistics
  --mode reconcile --gridfile --vetofile   (d)'s decomposition
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from autotrade.backtest.historical_news_calendar import HistoricalNewsCalendarProvider  # noqa: E402
from autotrade.backtest.report import generate_report  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.council.risk_voice import get_symbol_currencies  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402
from autotrade.watchman.news_protection import NewsProtectionConfig  # noqa: E402

import exp022_minlot_harness as e22  # noqa: E402
import exp026_minlot_skip_harness as e26  # noqa: E402

SYMBOL = "XAUUSD"
CALENDAR = HISTORICAL_DIR / "news_calendar_backtest.csv"
EQUITY = 3000.0
COMMISSION = 0.0
YEARS = dict((name, (a, b)) for name, a, b in e22.YEARS)
TRAIN = ("y1_2021-22", "y2_2022-23", "y3_2023-24")
VAL = ("y4_VAL_2024-25",)
CELLS = ("C0", "E", "P", "EP")
_CELL_FLAGS = {  # (model_risk_voice_news, model_news_protection)
    "C0": (False, False), "E": (True, False), "P": (False, True), "EP": (True, True),
}

# Recorded rows every cell must reproduce (cited BEFORE any run -- an anchor is
# only an anchor if it is named first). C0 = this log's universal baseline
# (EXP-022 cap-1.5 / EXP-023/024/025/026 §4 "C_none"); P = EXP-026 §4 "A_live"
# == the 2026-08-04 engine NOTE's A@real.
ANCHORS = {
    "C0": {
        "y1_2021-22": (266, 1.0159, 14.49), "y2_2022-23": (254, 0.9949, 26.12),
        "y3_2023-24": (233, 1.2020, 12.27), "y4_VAL_2024-25": (254, 1.0961, 9.9895),
    },
    "P": {
        "y1_2021-22": (391, 1.0256, 13.87), "y2_2022-23": (399, 0.8974, 29.11),
        "y3_2023-24": (358, 1.1328, 15.83), "y4_VAL_2024-25": (350, 1.0667, 11.4154),
    },
}
# Informational only (the 3ec55ee commit message's own y4 row for cell E; the
# commit explicitly disclaims measuring it -- that is this experiment's job).
E_Y4_INFORMATIONAL = (241, 1.1432, 490.82, 8.82)


# ------------------------------------------------------------------ setup
def load_window(window: str) -> pd.DataFrame:
    if window.startswith("y5"):
        raise SystemExit(
            "REFUSED: EXP-027 is a Train+Val MEASUREMENT. The Test year's one authorised "
            "measurement touch is already spent (2026-08-04 honest Test baseline); a full-parity "
            "Test re-measurement is a separate USER decision, not this harness's to make."
        )
    start, end = YEARS[window]
    df = pd.read_csv(HISTORICAL_DIR / f"{SYMBOL}_H1.csv", parse_dates=["time"])
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end) + pd.Timedelta(hours=23))]
    return df.reset_index(drop=True)


def build_cfg(cfg, cell: str) -> BacktestConfig:
    """The ONLY thing that varies across cells is the pair of independent
    news-modeling flags; everything else is EXP-022/023/024/025/026's context."""
    blackout, protection = _CELL_FLAGS[cell]
    rv_cfg, wm_cfg, sh_cfg, order = e22.build_cfgs(cfg)
    np_cfg = calendar = None
    if protection:
        np_cfg = NewsProtectionConfig(
            news_window_minutes=cfg["watchman"]["news_window_minutes"],
            profit_threshold_r=cfg["watchman"]["news_profit_threshold_r"],
            close_mode=cfg["watchman"]["news_close_mode"],
        )
    if protection or blackout:
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
        model_risk_voice_news=blackout,
        news_protection_cfg=np_cfg, news_calendar=calendar,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    )


def run_full(df, bt, *, recorder=None):
    engine_mod._OpenPosition = recorder if recorder is not None else e26._REAL_OPEN_POSITION
    try:
        return run_backtest(df, SYMBOL, e22._SPEC, bt)
    finally:
        engine_mod._OpenPosition = e26._REAL_OPEN_POSITION


# --------------------------------------------------- the blackout condition
def blackout_active(calendar, now, rv_cfg) -> bool:
    """Mirrors `council/risk_voice.check_risk_voice` condition 2 verbatim (its
    lines "window_start = now - after / window_end = now + before" and the
    per-currency loop over `get_symbol_currencies`). The empty-currency
    fail-safe branch cannot apply to XAUUSD (it maps to ("USD",)), and this
    provider never returns None (it validates eagerly at construction), so the
    fail-safe channel is structurally absent here -- see LIMITATIONS."""
    window_start = now - timedelta(minutes=rv_cfg.news_blackout_after_min)
    window_end = now + timedelta(minutes=rv_cfg.news_blackout_before_min)
    for currency in get_symbol_currencies(SYMBOL):
        events = calendar.get_high_impact_events(currency, window_start, window_end)
        if events is None or events:
            return True
    return False


# ------------------------------------------------------------------ stats
def _pf(vals):
    gp = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    if not vals:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)


def _mix(reasons):
    m = {}
    for r in reasons:
        m[r] = m.get(r, 0) + 1
    return dict(sorted(m.items(), key=lambda kv: -kv[1]))


def group_stats(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0}
    n = len(rs)
    mean = sum(rs) / n
    sd = statistics.stdev(rs) if n > 1 else 0.0
    return {
        "n": n, "avgR": round(mean, 4), "se": round(sd / math.sqrt(n), 4) if n > 1 else None,
        "medR": round(statistics.median(rs), 4), "PF": _pf(rs),
        "win_rate": round(sum(1 for r in rs if r > 0) / n, 4),
        "worstR": round(min(rs), 4), "bestR": round(max(rs), 4),
        "sum": sum(rs), "sum2": sum(r * r for r in rs),
    }


def portfolio_metrics(trades) -> dict:
    rep = generate_report(trades, EQUITY)
    return {
        "records": rep.trade_count,
        "positions": len({str(t.entry_time) for t in trades}),
        "PF": None if rep.profit_factor is None else round(rep.profit_factor, 4),
        "net$": round(rep.total_net_pnl, 2),
        "maxDD%": None if rep.max_drawdown_pct is None else round(rep.max_drawdown_pct, 4),
        "avgR": None if rep.avg_r_multiple is None else round(rep.avg_r_multiple, 4),
        "pf_ex5": None if rep.profit_factor_excluding_top_5 is None else round(rep.profit_factor_excluding_top_5, 4),
        "win_rate": None if rep.win_rate is None else round(rep.win_rate, 4),
        "news_exits": sum(1 for t in trades if t.exit_reason == "news_protection"),
    }


# ------------------------------------------------------------------ modes
def mode_fidelity(args) -> int:
    cfg = load_yaml_config("base")
    out = {"scope": args.scope}

    if args.scope == "fastpath":
        df = load_window("y4_VAL_2024-25").iloc[:4000].reset_index(drop=True)
        ok = True
        for cell in CELLS:
            bt = build_cfg(cfg, cell)
            slow = run_full(df, bt)
            e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
            try:
                fast = run_full(df, bt)
            finally:
                e22.uninstall_fast_path()
            same = [asdict(t) for t in slow] == [asdict(t) for t in fast]
            ok = ok and same
            out[f"fastpath_{cell}"] = {"n": len(slow), "identical": same}
        print("FIDELITY " + json.dumps(out, default=str))
        return 0 if ok else 1

    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        if args.scope == "anchor":
            ok = True
            for cell in ("C0", "P"):
                m = portfolio_metrics(run_full(df, build_cfg(cfg, cell)))
                exp_trades, exp_pf, exp_dd = ANCHORS[cell][args.window]
                match = (m["records"] == exp_trades and abs(m["PF"] - exp_pf) <= 0.0006
                         and abs(m["maxDD%"] - exp_dd) <= 0.006)
                ok = ok and match
                out[f"anchor_{cell}_{args.window}"] = {
                    "measured": m, "recorded": {"trades": exp_trades, "PF": exp_pf, "maxDD%": exp_dd},
                    "match": match,
                }
        elif args.scope == "replay":
            bt = build_cfg(cfg, "C0")
            span = engine_mod._infer_bar_span_minutes(df)
            rec = e26._Recorder()
            full_c0 = run_full(df, bt, recorder=rec)
            idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
            rebuilt = []
            for kw in rec.entries:
                rebuilt.extend(e26.replay_one(df, idx[pd.Timestamp(kw["entry_time"])], kw, bt, span,
                                              e26.MinLotSeam(skip=False)))
            same = [asdict(t) for t in full_c0] == [asdict(t) for t in rebuilt]
            ok = same
            out["replay_selfcheck_C0"] = {"window": args.window, "n_full": len(full_c0),
                                          "n_replay": len(rebuilt), "identical": same}
    finally:
        e22.uninstall_fast_path()
    print("FIDELITY " + json.dumps(out, default=str))
    return 0 if ok else 1


def mode_grid(args) -> int:
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        for cell in CELLS:
            trades = run_full(df, build_cfg(cfg, cell))
            row = {"window": args.window, "cell": cell, **portfolio_metrics(trades),
                   "entries": sorted({str(t.entry_time) for t in trades}),
                   "exit_mix": _mix([t.exit_reason for t in trades])}
            print("PORT " + json.dumps(row, default=str))
    finally:
        e22.uninstall_fast_path()
    return 0


def mode_veto(args) -> int:
    """Everything that is sequence-FIXED, computed on the C0 trade population:
    which of its entries the blackout would veto (both clock conventions), what
    those entries actually did (the counterfactual), and whether Watchman's news
    protection would later have truncated the same position anyway."""
    cfg = load_yaml_config("base")
    rv_cfg, _, _, _ = e22.build_cfgs(cfg)
    calendar = HistoricalNewsCalendarProvider(CALENDAR)
    df = load_window(args.window)
    span = engine_mod._infer_bar_span_minutes(df)

    # calendar-side density, independent of any trade (EXP-024 §1's "elig%")
    bar_times = [pd.Timestamp(t).to_pydatetime() for t in df["time"]]
    dens_sig = sum(1 for t in bar_times if blackout_active(calendar, t, rv_cfg))
    dens_fill = sum(1 for t in bar_times
                    if blackout_active(calendar, t + timedelta(minutes=span), rv_cfg))

    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        bt_c0 = build_cfg(cfg, "C0")
        bt_p = build_cfg(cfg, "P")
        rec = e26._Recorder()
        full_c0 = run_full(df, bt_c0, recorder=rec)
        idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
        by_entry = {}
        for t in full_c0:
            by_entry.setdefault(str(t.entry_time), []).append(t)

        rows = []
        for kw in rec.entries:
            entry_time = pd.Timestamp(kw["entry_time"])
            i_fill = idx[entry_time]
            i_sig = i_fill - 1  # engine: pending set on bar i, filled on bar i+1
            t_sig = pd.Timestamp(df["time"].iloc[i_sig]).to_pydatetime()
            recs = by_entry[str(kw["entry_time"])]
            assert len(recs) == 1, "C0 must emit exactly one record per position"
            # protection overlap: same position, same bars, protection ON
            seam = e26.MinLotSeam(skip=False)
            rp = e26.replay_one(df, i_fill, kw, bt_p, span, seam)
            rows.append({
                "window": args.window,
                "signal_time": str(t_sig), "entry": str(entry_time),
                "dir": kw["plan"].direction, "lot": kw["lot_size"],
                "rC0": round(recs[0].r_multiple, 6), "exitC0": recs[0].exit_reason,
                "black_sig": blackout_active(calendar, t_sig, rv_cfg),
                "black_fill": blackout_active(calendar, entry_time.to_pydatetime(), rv_cfg),
                "prot_actions": seam.min_lot_events + seam.partial_events,
                "rP": round(e26.position_r(rp, kw), 6), "exitP": rp[-1].exit_reason,
            })
    finally:
        e22.uninstall_fast_path()

    vet = [r for r in rows if r["black_sig"]]
    kept = [r for r in rows if not r["black_sig"]]
    vet_f = [r for r in rows if r["black_fill"]]
    aff = [r for r in rows if r["prot_actions"] > 0]
    both = [r for r in rows if r["black_sig"] and r["prot_actions"] > 0]
    out = {
        "window": args.window, "bars": len(df), "positions": len(rows),
        "blackout_bar_density_signal_conv": {"n": dens_sig, "pct": round(100.0 * dens_sig / len(df), 2)},
        "blackout_bar_density_fill_conv": {"n": dens_fill, "pct": round(100.0 * dens_fill / len(df), 2)},
        "vetoed_signal_conv": {"n": len(vet), "pct_of_C0_trades": round(100.0 * len(vet) / len(rows), 2)},
        "vetoed_fill_conv": {"n": len(vet_f), "pct_of_C0_trades": round(100.0 * len(vet_f) / len(rows), 2)},
        "convention_overlap": {"both": sum(1 for r in rows if r["black_sig"] and r["black_fill"]),
                               "sig_only": sum(1 for r in rows if r["black_sig"] and not r["black_fill"]),
                               "fill_only": sum(1 for r in rows if r["black_fill"] and not r["black_sig"])},
        "counterfactual_vetoed_C0": group_stats([r["rC0"] for r in vet]),
        "counterfactual_kept_C0": group_stats([r["rC0"] for r in kept]),
        "counterfactual_vetoed_fill_conv_C0": group_stats([r["rC0"] for r in vet_f]),
        "exit_mix_vetoed": _mix([r["exitC0"] for r in vet]),
        "exit_mix_kept": _mix([r["exitC0"] for r in kept]),
        "protection_affected": {"n": len(aff), "pct_of_C0_trades": round(100.0 * len(aff) / len(rows), 2)},
        "overlap_vetoed_and_protected": {
            "n": len(both),
            "pct_of_vetoed": round(100.0 * len(both) / len(vet), 2) if vet else None,
            "pct_of_affected": round(100.0 * len(both) / len(aff), 2) if aff else None,
        },
        "vetoed_rP_minus_rC0": group_stats([r["rP"] - r["rC0"] for r in vet]),
    }
    print("VETO " + json.dumps(out, default=str))
    for r in rows:
        print("POS " + json.dumps(r, default=str))
    return 0


def _pool_groups(recs, key):
    n = sum(r[key]["n"] for r in recs if r[key].get("n"))
    if n < 2:
        return {"n": n}
    s = sum(r[key]["sum"] for r in recs if r[key].get("n"))
    s2 = sum(r[key]["sum2"] for r in recs if r[key].get("n"))
    mean = s / n
    var = (s2 - n * mean * mean) / (n - 1)
    se = math.sqrt(max(var, 0.0) / n)
    return {"n": n, "avgR": round(mean, 4), "se": round(se, 4),
            "t": round(mean / se, 3) if se else None,
            "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)],
            "below_rule6_floor": n < 100}


def mode_pool(args) -> int:
    rows = [json.loads(l[5:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
            if l.startswith("VETO ")]

    def _pool(recs):
        if not recs:
            return {}
        pos = sum(r["positions"] for r in recs)
        vet = sum(r["vetoed_signal_conv"]["n"] for r in recs)
        vetf = sum(r["vetoed_fill_conv"]["n"] for r in recs)
        aff = sum(r["protection_affected"]["n"] for r in recs)
        both = sum(r["overlap_vetoed_and_protected"]["n"] for r in recs)
        v = _pool_groups(recs, "counterfactual_vetoed_C0")
        k = _pool_groups(recs, "counterfactual_kept_C0")
        diff = None
        if v.get("n", 0) > 1 and k.get("n", 0) > 1:
            se = math.sqrt(v["se"] ** 2 + k["se"] ** 2)
            d = v["avgR"] - k["avgR"]
            diff = {"vetoed_minus_kept": round(d, 4), "se": round(se, 4),
                    "t": round(d / se, 3) if se else None,
                    "ci95": [round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)]}
        return {
            "positions": pos, "vetoed_signal_conv": vet,
            "veto_rate_pct": round(100.0 * vet / pos, 2) if pos else None,
            "vetoed_fill_conv": vetf,
            "veto_rate_fill_pct": round(100.0 * vetf / pos, 2) if pos else None,
            "protection_affected": aff,
            "affected_rate_pct": round(100.0 * aff / pos, 2) if pos else None,
            "overlap_n": both,
            "overlap_pct_of_vetoed": round(100.0 * both / vet, 2) if vet else None,
            "overlap_pct_of_affected": round(100.0 * both / aff, 2) if aff else None,
            "vetoed_C0": v, "kept_C0": k, "vetoed_minus_kept": diff,
            "vetoed_fill_conv_C0": _pool_groups(recs, "counterfactual_vetoed_fill_conv_C0"),
        }

    print("POOL " + json.dumps({
        "TRAIN": _pool([r for r in rows if r["window"] in TRAIN]),
        "VAL": _pool([r for r in rows if r["window"] in VAL]),
        "per_window": {r["window"]: {
            "positions": r["positions"],
            "vetoed": r["vetoed_signal_conv"], "vetoed_fill": r["vetoed_fill_conv"],
            "vetoed_C0": r["counterfactual_vetoed_C0"], "kept_C0": r["counterfactual_kept_C0"],
            "overlap": r["overlap_vetoed_and_protected"],
            "protection_affected": r["protection_affected"],
        } for r in rows},
    }, default=str))
    return 0


def mode_reconcile(args) -> int:
    """(d): why the engine's E-cell trade count falls ~5% when the diagnostic
    predicted 10-13%. Decomposes the gap into gross vetoes vs net trade-count
    change (slot dynamics) and reports the fill-time-convention count too."""
    port = [json.loads(l[5:]) for l in Path(args.gridfile).read_text(encoding="utf-8").splitlines()
            if l.startswith("PORT ")]
    veto = [json.loads(l[5:]) for l in Path(args.vetofile).read_text(encoding="utf-8").splitlines()
            if l.startswith("VETO ")]
    cells = {(r["window"], r["cell"]): r for r in port}
    out = {}
    for v in veto:
        w = v["window"]
        c0, e = cells.get((w, "C0")), cells.get((w, "E"))
        p, ep = cells.get((w, "P")), cells.get((w, "EP"))
        if not (c0 and e):
            continue
        s_c0, s_e = set(c0["entries"]), set(e["entries"])
        out[w] = {
            "C0_positions": c0["positions"], "E_positions": e["positions"],
            "net_change": e["positions"] - c0["positions"],
            "net_change_pct": round(100.0 * (e["positions"] - c0["positions"]) / c0["positions"], 2),
            "gross_vetoed_C0_entries": v["vetoed_signal_conv"]["n"],
            "gross_veto_pct": v["vetoed_signal_conv"]["pct_of_C0_trades"],
            "gross_vetoed_fill_conv": v["vetoed_fill_conv"]["n"],
            "C0_entries_absent_from_E": len(s_c0 - s_e),
            "E_entries_absent_from_C0": len(s_e - s_c0),
            "shared_entries": len(s_c0 & s_e),
            "identity_check": (len(s_c0) - len(s_c0 - s_e) + len(s_e - s_c0)) == len(s_e),
            "P_positions": p["positions"] if p else None,
            "EP_positions": ep["positions"] if ep else None,
            "EP_minus_P_positions": (ep["positions"] - p["positions"]) if (p and ep) else None,
            "P_records": p["records"] if p else None, "EP_records": ep["records"] if ep else None,
        }
    print("RECON " + json.dumps(out, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["fidelity", "grid", "veto", "pool", "reconcile"])
    p.add_argument("--scope", default="anchor", choices=["fastpath", "anchor", "replay"])
    p.add_argument("--window", default="y1_2021-22", choices=sorted(YEARS))
    p.add_argument("--infile", default="experiments/exp027_veto_out.txt")
    p.add_argument("--gridfile", default="experiments/exp027_port_out.txt")
    p.add_argument("--vetofile", default="experiments/exp027_veto_out.txt")
    args = p.parse_args()
    return {"fidelity": mode_fidelity, "grid": mode_grid, "veto": mode_veto,
            "pool": mode_pool, "reconcile": mode_reconcile}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
