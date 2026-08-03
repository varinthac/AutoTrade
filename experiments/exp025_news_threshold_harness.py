#!/usr/bin/env python3
"""EXP-025 harness -- news-protection TRIGGER LEVEL: sweep
`watchman.news_profit_threshold_r` (T) under mode A (close-all at trigger, the
live behavior at min-lot) at ~$3,000 equity.

WHY (2026-08-04): EXP-023 rejected changing the min-lot fallback ACTION
(lock-the-stop is worse than both alternatives -- it parks a stop inside gold's
own hourly noise and deletes the 2R upside). Its section 6(iii) named the
remaining lever explicitly: "the surgical lever is the TRIGGER, not the
fallback action -- e.g. raising `news_profit_threshold_r`". That is this
experiment, and nothing else.

MECHANISM: `watchman/news_protection.check_news_protection` computes
`profit_r = (price - entry) / initial_stop_distance` and short-circuits to
NO_ACTION when `profit_r < profit_threshold_r` -- the news check is not even
consulted. So T decides WHICH TRADES ARE ELIGIBLE at all, and (at min-lot,
where `loop._half_volume_rounded` returns None and CLOSE_HALF_AND_BREAKEVEN
recurses into CLOSE_ALL) therefore which trades get force-closed instead of
running to SL/TP/structure/time-stop. Raising T is a strictly monotone SHRINK
of the treated set: affected(T) is a subset of affected(0.5).

INHERITED FROM EXP-023 (its D1/D2/D3, unchanged and still binding):
  D1. `backtest/engine.py` does not model news protection at all -- every
      baseline in EXP-001..022 is a "mode C" run. Mode A is simulated here by
      a VERBATIM copy of the engine's bar loop, not by production code.
  D2. There is no historical high-impact-news calendar, so the trigger TIME is
      proxied, pre-registered rather than chosen after the fact:
        P1 "always"     -- every bar trigger-eligible => protection fires at
                           the first touch of +T*R. Exactly the fail-safe
                           regime `news_protection.py` documents when the
                           calendar is unavailable; maximum-exposure bound and
                           largest legitimate sample. PRIMARY / selection.
        P2 "newshours"  -- Mon-Fri server hours {14,15,16,20,21}. ROBUSTNESS
                           ONLY, never used for selection.
  D3. The DECIDING metric is trade-matched and conditional (sequence held
      FIXED), because `max_positions_per_symbol: 1` makes portfolio deltas
      dominated by non-causal reshuffling (EXP-017/020/021). Portfolio runs are
      reported but are VETO-ONLY evidence.

NEW TO EXP-025 (pre-registered, see the log's EXP-025 section 2):
  E1. Because affected(T) is a SUBSET of affected(0.5), the arms do not share a
      treated set. The deciding metric is therefore the PAIRED per-trade R
      difference on the affected(0.5) subset -- the trades the CURRENT LIVE
      RULE touches -- i.e. A(T) - A(0.5), same trades. For a trade in
      affected(0.5) that never reaches +T*R, the A(T) outcome is simply
      whatever untreated management produces (stop_loss / take_profit /
      structure / time_stop). That is the point of raising T.
  E2. EXP-023's same-bar/next-bar addendum DOES NOT ARISE here. It existed only
      because mode B places a RESTING BROKER STOP. Mode A is a MARKET CLOSE at
      the trigger instant: no later level exists to be touched, so there is no
      intrabar-path convention to choose. Every arm here is mode A.
  E4. NO lock-SL variants -- EXP-023 answered that. Mode C is carried as a
      REFERENCE BOUND ONLY and is never a candidate.

PER-BAR ORDERING CONVENTION (unchanged from EXP-023 so the numbers are directly
comparable): SL/TP `check_exit` (engine convention, SL wins a double-touch)
  > news-protection intrabar trigger
  > Watchman CLOSE at the bar's close.
Trigger price = the bar's OPEN if the position is already >= T*R at the open,
else the exact +T*R level. Exits fill nominally (engine convention; symmetric
across all arms).

This file modifies NOTHING under `src/` or `config/`.

MODES:
  --mode fidelity     news OFF == real engine, trade-for-trade, field-for-field
  --mode anchor       external cross-check: mode C on y4/VAL at $3,000 must be
                      254 trades / PF 1.0961 / net +$352.60 / maxDD 9.99%
  --mode conditional  DECIDING metric (see E1)
  --mode portfolio    full-sequence re-simulation per T; veto-only evidence
  --mode pool         aggregate a conditional output file into pooled paired
                      statistics (Train = y1+y2+y3, Val = y4)
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import (  # noqa: E402
    BacktestConfig,
    ClosedTrade,
    _build_watchman_metadata,
    _classify_watchman_exit_reason,
    _close_trade,
    _fill_entry_price,
    _swing_index_at,
    _watchman_current_atr,
    check_exit,
    run_backtest,
)
from autotrade.backtest.report import generate_report  # noqa: E402
from autotrade.backtest.clock import SimulatedClock  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402
from autotrade.risk.sizing import compute_lot_size  # noqa: E402
from autotrade.shield.checkpoint import Shield  # noqa: E402
from autotrade.watchman.evaluate import evaluate_watchman  # noqa: E402

# EXP-022's validated pieces, reused verbatim rather than re-derived.
from exp022_minlot_harness import (  # noqa: E402
    SWAP_LONG,
    SWAP_SHORT,
    YEARS,
    _SPEC,
    build_cfgs,
    install_fast_path,
    slice_year,
    uninstall_fast_path,
)

# Windows legitimately available to this experiment. y5 (TEST) is deliberately
# absent -- EXP-025 is pre-registered Train+Val only, under every outcome.
TRAIN_VAL = [y for y in YEARS if not y[0].startswith("y5")]

# P2 proxy (robustness only): Mon-Fri, server hours when high-impact USD macro
# releases and FOMC decisions actually land. NOT a calendar.
NEWSHOURS = frozenset({14, 15, 16, 20, 21})

# The baseline trigger level = `config/base.yaml` watchman.news_profit_threshold_r.
BASE_T = 0.5

# Pre-registered multiple-testing bar (EXP-025 section 3): 1.7 SE, the level
# EXP-023 used at N=20, NOT the nominal 1.6 -- the sibling family searched the
# same data with the same affected subsets.
SE_BAR = 1.7

_ORIGIN_SINK: list | None = None


@dataclass(frozen=True)
class NewsSim:
    """`mode="off"` reproduces `backtest.engine.run_backtest` exactly."""

    mode: str = "off"  # "off" | "close_all"
    profit_threshold_r: float = BASE_T
    hours: frozenset[int] | None = None  # None = P1 (every bar eligible)

    @property
    def label(self) -> str:
        if self.mode == "off":
            return "C_none"
        return f"A_close_all@T{self.profit_threshold_r:g}"


@dataclass
class _Pos:
    """`engine._OpenPosition` plus the news-trigger bookkeeping fields."""

    plan: object
    lot_size: float
    entry_time: pd.Timestamp
    entry_price: float
    spread_slippage_price_delta: float
    current_sl: float
    metadata: object
    entry_index: int
    news_trigger_index: int | None = None
    news_trigger_price: float | None = None


@dataclass
class TradeInfo:
    trade: ClosedTrade
    entry_index: int
    news_triggered: bool
    news_trigger_index: int | None
    news_trigger_price: float | None


def _news_trigger_price(pos: _Pos, bar: pd.Series, news: NewsSim) -> float | None:
    """The price at which live's ~5s-cadence Watchman would first see this
    position at >= `profit_threshold_r` within this bar -- or None if it never
    gets there on this bar (or the bar is not trigger-eligible under the active
    proxy). Mirrors `news_protection.check_news_protection`'s profit_r exactly:
    entry-relative, divided by the ORIGINAL stop distance, never a moved stop."""
    if news.hours is not None:
        ts = bar["time"]
        if ts.hour not in news.hours or ts.weekday() >= 5:
            return None
    thr = pos.plan.stop_distance * news.profit_threshold_r
    if pos.plan.direction == "BUY":
        level = pos.entry_price + thr
        if bar["open"] >= level:
            return float(bar["open"])
        if bar["high"] >= level:
            return float(level)
        return None
    level = pos.entry_price - thr
    if bar["open"] <= level:
        return float(bar["open"])
    if bar["low"] <= level:
        return float(level)
    return None


def _step_position(
    df: pd.DataFrame,
    i: int,
    pos: _Pos,
    news: NewsSim,
    config: BacktestConfig,
) -> tuple[float, str] | None:
    """One bar of position management. Returns `(exit_price, exit_reason)` if
    the position closes on this bar, else None. SINGLE implementation shared by
    `run_backtest_news` and `replay_one`, so the conditional replay cannot
    silently diverge from the full-sequence simulation.

    With `news.mode == "off"` the body is line-for-line
    `backtest.engine.run_backtest`'s open-position block."""
    bar = df.iloc[i]

    exit_result = check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar)
    if exit_result is not None:
        exit_price, exit_reason = exit_result
        return float(exit_price), exit_reason

    if news.mode == "close_all":
        trig = _news_trigger_price(pos, bar, news)
        if trig is not None:
            pos.news_trigger_index = i
            pos.news_trigger_price = trig
            return trig, "news_protection"

    if config.watchman_cfg is not None and pos.metadata is not None:
        decision = evaluate_watchman(
            position_metadata=pos.metadata,
            current_sl=pos.current_sl,
            current_price=float(bar["close"]),
            current_atr=_watchman_current_atr(df, i),
            df=df,
            as_of_index=i,
            now=pd.Timestamp(bar["time"]).to_pydatetime(),
            config=config.watchman_cfg,
        )
        if decision.action == "CLOSE":
            return float(bar["close"]), _classify_watchman_exit_reason(decision.reason)
        if decision.action == "MODIFY_SL":
            if pos.plan.direction == "BUY":
                pos.current_sl = max(pos.current_sl, decision.new_stop_loss)
            else:
                pos.current_sl = min(pos.current_sl, decision.new_stop_loss)
    return None


def _finish(
    symbol: str, pos: _Pos, df: pd.DataFrame, i: int, exit_price: float, exit_reason: str,
    point_value: float, config: BacktestConfig,
) -> TradeInfo:
    # `_close_trade` only type-ANNOTATES exit_reason (a `Literal` is not
    # enforced at runtime) and `report.generate_report` never reads it, so the
    # honest "news_protection" label is carried straight through.
    trade = _close_trade(
        symbol, pos, pd.Timestamp(df.iloc[i]["time"]), exit_price, exit_reason,
        point_value, config.cost_model,
    )
    return TradeInfo(
        trade=trade,
        entry_index=pos.entry_index,
        news_triggered=pos.news_trigger_index is not None,
        news_trigger_index=pos.news_trigger_index,
        news_trigger_price=pos.news_trigger_price,
    )


def run_backtest_news(
    df: pd.DataFrame, symbol: str, symbol_spec, config: BacktestConfig, news: NewsSim,
) -> list[TradeInfo]:
    """VERBATIM copy of `backtest.engine.run_backtest` with the open-position
    block delegated to `_step_position` (which IS the engine's own block when
    `news.mode == "off"`). Proven identical to the real engine at
    `news.mode == "off"` by `--mode fidelity`."""
    if len(df) < 2:
        return []

    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    equity = config.starting_equity
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    shield = Shield(
        min_rr=config.shield_cfg.min_rr,
        max_correlation=config.shield_cfg.max_correlation,
        max_positions_per_symbol=config.shield_cfg.max_positions_per_symbol,
        max_positions_total=config.shield_cfg.max_positions_total,
        total_risk_ceiling_pct=config.shield_cfg.total_risk_ceiling_pct,
        duplicate_signal_cooldown_hours=config.shield_cfg.duplicate_signal_cooldown_hours,
    ) if config.shield_cfg is not None else None

    pending = None
    position: _Pos | None = None
    out: list[TradeInfo] = []

    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())

        if pending is not None:
            entry_price, delta = _fill_entry_price(
                pending.plan.direction, bar["open"], bar["spread"], symbol_spec, config.cost_model
            )
            entry_time = pd.Timestamp(bar["time"])
            if shield is not None and pending.swing_index is not None:
                shield.record_trade_opened(
                    symbol=symbol, direction=pending.plan.direction,
                    opened_at=entry_time.to_pydatetime(), swing_index=pending.swing_index,
                )
            metadata = None
            if config.watchman_cfg is not None:
                metadata = _build_watchman_metadata(
                    symbol, pending.plan, entry_price, entry_time, df, pending.signal_index, config.pivot_bars,
                )
            position = _Pos(
                plan=pending.plan, lot_size=pending.lot_size, entry_time=entry_time,
                entry_price=entry_price, spread_slippage_price_delta=delta,
                current_sl=pending.plan.stop_loss, metadata=metadata, entry_index=i,
            )
            if _ORIGIN_SINK is not None:
                _ORIGIN_SINK.append({
                    "entry_index": i, "plan": pending.plan, "lot_size": pending.lot_size,
                    "entry_time": entry_time, "entry_price": entry_price, "delta": delta,
                    "metadata": metadata,
                })
            pending = None

        if position is not None:
            step = _step_position(df, i, position, news, config)
            if step is not None:
                exit_price, exit_reason = step
                info = _finish(symbol, position, df, i, exit_price, exit_reason, point_value, config)
                out.append(info)
                equity += info.trade.net_pnl
                position = None

        if position is None and pending is None:
            plan = config.signal_fn(
                df, i, symbol=symbol, symbol_spec=symbol_spec,
                sl_buffer_atr=config.sl_buffer_atr, sl_min_atr=config.sl_min_atr,
                sl_max_atr=config.sl_max_atr, tp_r_multiple=config.tp_r_multiple,
                pivot_bars=config.pivot_bars, bull_threshold=config.bull_threshold,
                bear_threshold=config.bear_threshold, conflict_threshold=config.conflict_threshold,
                risk_voice_cfg=config.risk_voice_cfg, clock=clock,
            )
            swing_index = None
            if plan is not None and shield is not None:
                swing_index = _swing_index_at(df, i, plan.direction, config.pivot_bars)
                if swing_index is not None:
                    shield_decision = shield.check(
                        order_plan=plan, symbol=symbol, open_positions=[],
                        new_trade_risk_pct=config.risk_per_trade_pct, swing_index=swing_index, clock=clock,
                    )
                    if shield_decision.blocked:
                        plan = None
            if plan is not None:
                lot = compute_lot_size(
                    equity=equity, risk_per_trade_pct=config.risk_per_trade_pct,
                    entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
                    volume_min=symbol_spec.volume_min, volume_max=symbol_spec.volume_max,
                    volume_step=symbol_spec.volume_step,
                    min_lot_risk_cap_pct=config.min_lot_risk_cap_pct,
                )
                if lot is not None and i + 1 < len(df):
                    pending = engine_mod._PendingOrder(
                        plan=plan, lot_size=lot, signal_index=i, swing_index=swing_index
                    )

    if position is not None:
        last = len(df) - 1
        out.append(
            _finish(symbol, position, df, last, float(df.iloc[last]["close"]), "end_of_data",
                    point_value, config)
        )
    return out


def replay_one(
    df: pd.DataFrame, symbol: str, symbol_spec, config: BacktestConfig, news: NewsSim,
    template: TradeInfo, origin: dict,
) -> TradeInfo:
    """Re-live ONE already-opened position under a different trigger level, with
    everything else held fixed (same entry bar, same fill price, same lot, same
    metadata, same subsequent bars). Deliberately does NOT let an earlier/later
    exit change which signal is taken next: in a `max_positions_per_symbol: 1`
    engine that reshuffling dominates the portfolio delta and is not caused by
    the parameter under test (EXP-017/020/021)."""
    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    pos = _Pos(
        plan=origin["plan"], lot_size=origin["lot_size"], entry_time=origin["entry_time"],
        entry_price=origin["entry_price"], spread_slippage_price_delta=origin["delta"],
        current_sl=origin["plan"].stop_loss, metadata=origin["metadata"],
        entry_index=template.entry_index,
    )
    for i in range(template.entry_index, len(df)):
        step = _step_position(df, i, pos, news, config)
        if step is not None:
            return _finish(symbol, pos, df, i, step[0], step[1], point_value, config)
    last = len(df) - 1
    return _finish(symbol, pos, df, last, float(df.iloc[last]["close"]), "end_of_data", point_value, config)


def build_bt_config(cfg, *, equity: float, commission: float, swap: bool) -> BacktestConfig:
    rv_cfg, wm_cfg, sh_cfg, order = build_cfgs(cfg)
    return BacktestConfig(
        starting_equity=equity,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=commission, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=SWAP_LONG,
                                       short_per_lot_per_night=SWAP_SHORT) if swap else None,
        ),
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    )


# ------------------------------------------------------------------ metrics
def _pf(rs: list[float]) -> float | None:
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    if not rs:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def subset_stats(trades: list[ClosedTrade]) -> dict:
    rs = [t.r_multiple for t in trades]
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "sumR": _r(sum(rs)),
        "avgR": _r(sum(rs) / len(rs)),
        "medR": _r(statistics.median(rs)),
        "PF": _pf(rs),
        "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4),
        "worstR": _r(min(rs)),
        "bestR": _r(max(rs)),
        # criterion (g): raising T re-exposes trades to full SL risk, an
        # outcome the T=0.5 arm makes arithmetically impossible.
        "n_negative": sum(1 for r in rs if r < 0),
        "frac_negative": round(sum(1 for r in rs if r < 0) / len(rs), 4),
        "net$": round(sum(t.net_pnl for t in trades), 2),
    }


def paired_delta(a: list[float], b: list[float]) -> dict:
    """Paired treatment effect B - A over the SAME trades. `sum_d`/`sum_d2` are
    emitted so windows can be pooled EXACTLY later (mean = sum_d/n; variance =
    (sum_d2 - n*mean^2)/(n-1)) without re-running anything."""
    d = [x - y for x, y in zip(b, a)]
    n = len(d)
    if n < 2:
        return {"n": n}
    mean = sum(d) / n
    sd = statistics.stdev(d)
    se = sd / math.sqrt(n)
    return {
        "n": n,
        "mean_dR": _r(mean),
        "se_dR": _r(se),
        "t": _r(mean / se) if se else None,
        f"required_{SE_BAR}se": _r(SE_BAR * se),
        f"passes_{SE_BAR}se": bool(se and mean > SE_BAR * se),
        "n_better": sum(1 for x in d if x > 1e-9),
        "n_worse": sum(1 for x in d if x < -1e-9),
        "n_same": sum(1 for x in d if abs(x) <= 1e-9),
        "sum_d": round(sum(d), 8),
        "sum_d2": round(sum(x * x for x in d), 8),
    }


def cell(trades: list[ClosedTrade], equity: float) -> dict:
    rep = generate_report(trades, equity)
    return {
        "trades": rep.trade_count,
        "PF": _r(rep.profit_factor) if rep.profit_factor not in (None, float("inf")) else rep.profit_factor,
        "PF_ex5": _r(rep.profit_factor_excluding_top_5)
        if rep.profit_factor_excluding_top_5 not in (None, float("inf"))
        else rep.profit_factor_excluding_top_5,
        "net$": round(rep.total_net_pnl, 2),
        "avgR": _r(rep.avg_r_multiple),
        "win_rate": _r(rep.win_rate),
        "maxDD%": _r(rep.max_drawdown_pct),
        "worst$": round(min((t.net_pnl for t in trades), default=0.0), 2),
    }


def exit_mix(infos: list[TradeInfo]) -> dict:
    mix: dict[str, int] = {}
    for i in infos:
        mix[i.trade.exit_reason] = mix.get(i.trade.exit_reason, 0) + 1
    return dict(sorted(mix.items()))


# ------------------------------------------------------------------ modes
def mode_fidelity(df, cfg, args) -> int:
    sub = df.iloc[:4000].reset_index(drop=True)
    bt = build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)

    real = run_backtest(sub, "XAUUSD", _SPEC, bt)
    copy_slow = [t.trade for t in run_backtest_news(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"))]
    same_copy = real == copy_slow

    install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
    try:
        copy_fast = [t.trade for t in run_backtest_news(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"))]
    finally:
        uninstall_fast_path()
    same_fast = real == copy_fast

    print(f"FIDELITY bars={len(sub)} real_trades={len(real)} "
          f"copy_off_identical={same_copy} copy_off_fastpath_identical={same_fast}", flush=True)
    if real:
        print("SAMPLE " + json.dumps({
            "first": [str(real[0].entry_time), real[0].exit_reason, round(real[0].net_pnl, 4)],
            "last": [str(real[-1].entry_time), real[-1].exit_reason, round(real[-1].net_pnl, 4)],
        }), flush=True)
    return 0 if (same_copy and same_fast) else 1


# Previously RECORDED numbers this stack must reproduce before any candidate
# number is trusted (EXP-022's y4 cap-1.5 cell, re-confirmed by EXP-023).
ANCHOR = {"window": "y4_VAL_2024-25", "trades": 254, "PF": 1.0961, "net$": 352.60, "maxDD%": 9.99}


def mode_anchor(df, cfg, args) -> int:
    bt = build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)
    name, s, e = [y for y in TRAIN_VAL if y[0] == ANCHOR["window"]][0]
    sub = slice_year(df, s, e)
    install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
    try:
        trades = [i.trade for i in run_backtest_news(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"))]
    finally:
        uninstall_fast_path()
    got = cell(trades, args.equity)
    ok = bool(
        got["trades"] == ANCHOR["trades"]
        and abs(got["PF"] - ANCHOR["PF"]) < 5e-4
        and abs(got["net$"] - ANCHOR["net$"]) < 0.01
        and abs(got["maxDD%"] - ANCHOR["maxDD%"]) < 0.005
    )
    print("ANCHOR " + json.dumps({"window": name, "config": "C_none", "expected": ANCHOR,
                                  "got": got, "match": ok}), flush=True)
    return 0 if ok else 1


def mode_conditional(df, cfg, args) -> int:
    bt = build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)
    thresholds = [float(x) for x in args.thresholds.split(",")]
    assert abs(thresholds[0] - BASE_T) < 1e-12, "the first threshold must be the 0.5 baseline"
    proxies = [("P1_always", None)]
    if not args.no_p2:
        proxies.append(("P2_newshours", NEWSHOURS))

    for name, s, e in _windows(args):
        sub = slice_year(df, s, e)
        # The fast path stays installed for the replays too -- it is exact
        # (EXP-022's own fidelity mode, re-proven by ours), and the replays
        # re-evaluate Watchman bar by bar, which is where it matters most.
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            base_infos, base_origins = _run_with_origins(sub, bt, NewsSim(mode="off"))
            _conditional_window(sub, bt, base_infos, base_origins, proxies, thresholds, name)
        finally:
            uninstall_fast_path()
    return 0


def _conditional_window(sub, bt, base_infos, base_origins, proxies, thresholds, name) -> None:
    # Self-check: replaying mode C trade-by-trade must reproduce the
    # full-sequence mode C result EXACTLY, or the replay is not a faithful
    # counterfactual and nothing below can be trusted. Proxy-independent, so
    # done once per window.
    selfcheck = [
        replay_one(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"), t, o).trade
        for t, o in zip(base_infos, base_origins)
    ]
    assert selfcheck == [t.trade for t in base_infos], "conditional replay diverged from mode C"

    for pname, hours in proxies:
        # A(0.5) over EVERY trade: this defines affected(0.5), the pre-registered
        # deciding subset (the trades the CURRENT LIVE RULE touches).
        base_sim = NewsSim(mode="close_all", profit_threshold_r=BASE_T, hours=hours)
        a0 = [replay_one(sub, "XAUUSD", _SPEC, bt, base_sim, t, o)
              for t, o in zip(base_infos, base_origins)]
        affected = [k for k, x in enumerate(a0) if x.news_triggered]
        a0r = [a0[k].trade.r_multiple for k in affected]
        cr = [base_infos[k].trade.r_multiple for k in affected]

        row = {
            "window": name, "proxy": pname,
            "trades_total": len(base_infos),
            "affected_n": len(affected),
            "affected_pct": round(100.0 * len(affected) / len(base_infos), 2) if base_infos else 0.0,
            "C_on_affected": subset_stats([base_infos[k].trade for k in affected]),
            "C_exit_mix_on_affected": exit_mix([base_infos[k] for k in affected]),
            "dR_C_minus_A050": paired_delta(a0r, cr),
            "T": {},
        }

        # Monotonicity, pre-registered fidelity gate 5, checked EMPIRICALLY and
        # cheaply: replay the NOT-affected(0.5) trades at the SMALLEST
        # non-baseline T. Triggering at a higher T implies triggering at every
        # lower T (the +T*R level is strictly farther away, same eligibility
        # mask), so if none of them fires at the smallest raised T, none fires
        # at any of them -- i.e. affected(T) is a subset of affected(0.5).
        raised = [t for t in thresholds if abs(t - BASE_T) >= 1e-12]
        if raised:
            probe = NewsSim(mode="close_all", profit_threshold_r=min(raised), hours=hours)
            unaffected = [k for k in range(len(base_infos)) if k not in set(affected)]
            leaked = [
                k for k in unaffected
                if replay_one(sub, "XAUUSD", _SPEC, bt, probe, base_infos[k], base_origins[k]).news_triggered
            ]
            assert not leaked, f"affected(T) is not a subset of affected(0.5): {leaked[:5]}"
            row["monotonicity_probe"] = {"T": min(raised), "unaffected_n": len(unaffected), "leaked": 0}

        for t_val in thresholds:
            if abs(t_val - BASE_T) < 1e-12:
                infos = [a0[k] for k in affected]
            else:
                sim = NewsSim(mode="close_all", profit_threshold_r=t_val, hours=hours)
                infos = [replay_one(sub, "XAUUSD", _SPEC, bt, sim, base_infos[k], base_origins[k])
                         for k in affected]
            rs = [x.trade.r_multiple for x in infos]
            row["T"][f"{t_val:g}"] = {
                **subset_stats([x.trade for x in infos]),
                "still_triggering_n": sum(1 for x in infos if x.news_triggered),
                "still_triggering_pct": round(
                    100.0 * sum(1 for x in infos if x.news_triggered) / len(infos), 2) if infos else 0.0,
                "exit_mix": exit_mix(infos),
                "dR_vs_A050": paired_delta(a0r, rs),
                "dR_vs_C": paired_delta(cr, rs),
            }
        print("COND " + json.dumps(row), flush=True)


def _run_with_origins(sub, bt, news):
    """`run_backtest_news` + the per-trade fill context `replay_one` needs (the
    `OrderPlan`/`PositionMetadata`/fill price are not recoverable from a
    `ClosedTrade` alone). Collected by the run itself, then asserted 1:1
    against the trade list on entry_index/entry_price/lot."""
    global _ORIGIN_SINK
    _ORIGIN_SINK = []
    infos = run_backtest_news(sub, "XAUUSD", _SPEC, bt, news)
    origins = list(_ORIGIN_SINK)
    _ORIGIN_SINK = None
    assert len(origins) == len(infos), f"origin/trade mismatch {len(origins)} vs {len(infos)}"
    for o, t in zip(origins, infos):
        assert o["entry_index"] == t.entry_index
        assert abs(o["entry_price"] - t.trade.entry_price) < 1e-12
        assert abs(o["lot_size"] - t.trade.lot_size) < 1e-12
    return infos, origins


def mode_portfolio(df, cfg, args) -> int:
    equity = args.equity
    bt = build_bt_config(cfg, equity=equity, commission=args.commission, swap=not args.no_swap)
    thresholds = [float(x) for x in args.thresholds.split(",")]
    proxies = [("P1_always", None)]
    if not args.no_p2:
        proxies.append(("P2_newshours", NEWSHOURS))

    for name, s, e in _windows(args):
        sub = slice_year(df, s, e)
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            for pname, hours in proxies:
                sims = [NewsSim(mode="close_all", profit_threshold_r=t, hours=hours) for t in thresholds]
                if pname == "P1_always":
                    sims.append(NewsSim(mode="off"))  # reference bound, proxy-independent
                for sim in sims:
                    infos = run_backtest_news(sub, "XAUUSD", _SPEC, bt, sim)
                    trades = [i.trade for i in infos]
                    print("PORT " + json.dumps({
                        "window": name, "proxy": pname, "config": sim.label,
                        **cell(trades, equity),
                        "news_fired": sum(1 for i in infos if i.news_triggered),
                        "exit_mix": exit_mix(infos),
                    }), flush=True)
        finally:
            uninstall_fast_path()
    return 0


TRAIN_WINDOWS = {"y1_2021-22", "y2_2022-23", "y3_2023-24"}
VAL_WINDOWS = {"y4_VAL_2024-25"}


def mode_pool(args) -> int:
    """Pool per-window paired differences into Train (y1+y2+y3) and Val (y4)
    statistics. Exact: uses the sum_d / sum_d2 emitted per window, so pooling
    reproduces what a single pooled list of per-trade differences would give."""
    rows = []
    for line in Path(args.infile).read_text(encoding="utf-8").splitlines():
        if line.startswith("COND "):
            rows.append(json.loads(line[5:]))

    def _pool(records):
        n = sum(r["n"] for r in records)
        sd = sum(r["sum_d"] for r in records)
        sd2 = sum(r["sum_d2"] for r in records)
        if n < 2:
            return None
        mean = sd / n
        var = (sd2 - n * mean * mean) / (n - 1)
        se = math.sqrt(max(var, 0.0) / n)
        return {"n": n, "mean_dR": round(mean, 4), "se_dR": round(se, 4),
                "t": round(mean / se, 3) if se else None,
                f"bar_{SE_BAR}se": round(SE_BAR * se, 4),
                f"passes_{SE_BAR}se": bool(se and mean > SE_BAR * se)}

    proxies = sorted({r["proxy"] for r in rows})
    ts = sorted({t for r in rows for t in r["T"]}, key=float)
    for proxy in proxies:
        for split, wins in (("TRAIN", TRAIN_WINDOWS), ("VAL", VAL_WINDOWS)):
            sel = [r for r in rows if r["proxy"] == proxy and r["window"] in wins]
            if not sel:
                continue
            for key, getter in (("dR_vs_A050", lambda r, t: r["T"][t]["dR_vs_A050"]),):
                for t in ts:
                    recs = [getter(r, t) for r in sel if t in r["T"]]
                    recs = [x for x in recs if x.get("n", 0) >= 2]
                    if not recs:
                        continue
                    pooled = _pool(recs)
                    print("POOL " + json.dumps({
                        "proxy": proxy, "split": split, "metric": key, "T": t,
                        "windows": [r["window"] for r in sel], **(pooled or {}),
                    }), flush=True)
            # the A(0.5) - C reference bound, pooled the same way
            recs = [r["dR_C_minus_A050"] for r in sel if r["dR_C_minus_A050"].get("n", 0) >= 2]
            if recs:
                print("POOL " + json.dumps({
                    "proxy": proxy, "split": split, "metric": "dR_C_minus_A050", "T": "n.a.",
                    **( _pool(recs) or {}),
                }), flush=True)
    return 0


def _windows(args):
    if args.window in (None, "all"):
        return TRAIN_VAL
    return [y for y in TRAIN_VAL if y[0] == args.window]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True,
                   choices=["fidelity", "anchor", "conditional", "portfolio", "pool"])
    p.add_argument("--equity", type=float, default=3000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--window", default=None,
                   help="y1_2021-22 | y2_2022-23 | y3_2023-24 | y4_VAL_2024-25 | all")
    p.add_argument("--thresholds", default="0.5,0.75,1.0,1.25,1.5",
                   help="news_profit_threshold_r grid; the first MUST be the 0.5 baseline")
    p.add_argument("--no-p2", action="store_true", help="skip the P2 news-hours robustness proxy")
    p.add_argument("--no-swap", action="store_true")
    p.add_argument("--infile", default=None, help="--mode pool: conditional output file to aggregate")
    args = p.parse_args()

    if args.window is not None and args.window.startswith("y5"):
        print("REFUSED: EXP-025 is pre-registered Train+Val only; the Test year stays untouched.", flush=True)
        return 2
    for t in (float(x) for x in args.thresholds.split(",")):
        if not (0.5 <= t <= 1.5):
            print(f"REFUSED: T={t} is outside the pre-registered bound [0.5, 1.5] "
                  "(T >= tp_r_multiple 2.0 degenerates mode A into mode C).", flush=True)
            return 2

    if args.mode == "pool":
        return mode_pool(args)

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    if args.mode == "fidelity":
        return mode_fidelity(df, cfg, args)
    if args.mode == "anchor":
        return mode_anchor(df, cfg, args)
    if args.mode == "conditional":
        return mode_conditional(df, cfg, args)
    return mode_portfolio(df, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
