#!/usr/bin/env python3
"""EXP-017 harness: an ADDITIVE "deep-loss timeout" exit condition for XAUUSD.

MECHANISM UNDER TEST (brand-new, NOT a re-litigation of EXP-006/008's Watchman
breakeven/trail, which act on PROFIT thresholds and cut WINNERS). While a
position is open, if its floating R-multiple is <= -X (X a POSITIVE deep-loss
threshold) AND it has been held >= Y hours, AND none of the existing exit
conditions (fixed SL/TP via `check_exit`, structure_invalidation, time_stop via
`evaluate_watchman`) fired this bar, close it at THIS bar's close price. It can
NEVER touch a flat or profitable trade (floating R must be <= a negative number).

WHY A STANDALONE LOOP: the deep-loss check must run per-bar on the open position,
at a precedence the real engine's `run_backtest` loop does not expose an
injection point for, and src/ must not be modified. So this file re-implements
`backtest.engine.run_backtest`'s loop VERBATIM, reusing every engine helper
(_fill_entry_price, check_exit, _build_watchman_metadata, _watchman_current_atr,
_swing_index_at, _close_trade, _classify_watchman_exit_reason, evaluate_watchman,
Shield, compute_lot_size, SimulatedClock) -- nothing is reimplemented -- and adds
ONLY the deep-loss check. FIDELITY CHECK (run first, every time): with deep-loss
DISABLED, and again with an unreachable X=5.0, this loop must reproduce the real
`run_backtest` output trade-for-trade. If it does not, the harness is untrusted
and no candidate number means anything.

Baseline = the live adopted config (be/trail OFF per EXP-008, structure+time ON,
tp 2.0, pivot 3, all-24h Risk Voice, Shield cooldown, min-lot cap 1.5, risk 1.0%),
commission $0 (IC Markets Standard), $10k starting equity for a clean PF compare.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from autotrade.backtest.clock import SimulatedClock
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import (
    BacktestConfig,
    _build_watchman_metadata,
    _classify_watchman_exit_reason,
    _close_trade,
    _fill_entry_price,
    _OpenPosition,
    _PendingOrder,
    _swing_index_at,
    _watchman_current_atr,
    check_exit,
    run_backtest,
)
from autotrade.backtest.report import generate_report
from autotrade.features.indicators import atr
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.risk.sizing import compute_lot_size
from autotrade.shield.checkpoint import Shield, ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig, evaluate_watchman

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

WINDOWS = {
    "train": ("2021-07-22", "2024-07-21"),
    "y1":    ("2021-07-22", "2022-07-21"),
    "y2":    ("2022-07-21", "2023-07-21"),
    "y3":    ("2023-07-21", "2024-07-21"),
    "val":   ("2024-07-21", "2025-07-21"),
    "test":  ("2025-07-21", "2026-07-21"),
}


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


@dataclass(frozen=True)
class DeepLoss:
    """Deep-loss timeout params. `x_r` is a POSITIVE magnitude: fires when
    floating R <= -x_r. `y_hours` is the minimum hold before it can apply.
    `enabled=False` -> loop is byte-identical to the real engine."""
    enabled: bool = False
    x_r: float = 0.5
    y_hours: float = 8.0


def _floating_r(position: _OpenPosition, price: float) -> float:
    """Price-based floating R, same convention as
    `exit_conditions.check_time_stop` (uses initial stop distance, pre-cost),
    denominator = plan.stop_distance (same risk unit `_close_trade` divides by)."""
    entry = position.entry_price
    dist = position.plan.stop_distance
    if position.plan.direction == "BUY":
        return (price - entry) / dist
    return (entry - price) / dist


def precompute_signals(df, symbol, symbol_spec, config):
    """One expensive council/risk-voice pass over the window. `config.signal_fn`
    is a PURE function of `(df, i)` (it reads no position/shield state -- see
    `_council_signal_fn`), so its output for bar `i` is identical across every
    deep-loss config run on this same window. Precomputing it once and sharing
    it lets the 7 re-simulations (baseline + 6 grid) skip re-deriving signals,
    turning ~7 full council passes into 1. Also caches the shield swing-index
    per signalling bar (deterministic given the bar's fixed direction). The
    `clock` is set to each bar's time exactly as `run_backtest`'s loop does, so
    memoized outputs are byte-identical to inline calls (verified by fidelity)."""
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    signals: list = [None] * len(df)
    swing_idx: list = [None] * len(df)
    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())
        plan = config.signal_fn(
            df, i, symbol=symbol, symbol_spec=symbol_spec,
            sl_buffer_atr=config.sl_buffer_atr, sl_min_atr=config.sl_min_atr,
            sl_max_atr=config.sl_max_atr, tp_r_multiple=config.tp_r_multiple,
            pivot_bars=config.pivot_bars, bull_threshold=config.bull_threshold,
            bear_threshold=config.bear_threshold, conflict_threshold=config.conflict_threshold,
            risk_voice_cfg=config.risk_voice_cfg, clock=clock,
        )
        signals[i] = plan
        if plan is not None:
            swing_idx[i] = _swing_index_at(df, i, plan.direction, config.pivot_bars)
    # Precompute ATR(14) once. `_watchman_current_atr(df, i)` is ATR(14) over
    # bars [0..i] -- ATR is causal (Wilder recursion / trailing mean of TR), so
    # the full-series value at position i equals the truncated-to-i value at its
    # last position. Memoizing it removes the O(n^2) per-bar recompute from every
    # re-sim. (In this config be/trail are OFF, so this value feeds no decision
    # at all; the fidelity check confirms exact equivalence regardless.)
    atr14 = atr(df["high"], df["low"], df["close"], period=14).to_numpy()
    return signals, swing_idx, atr14


def run_deeploss_backtest(
    df: pd.DataFrame,
    symbol: str,
    symbol_spec: SymbolSpec,
    config: BacktestConfig,
    deep_loss: DeepLoss,
    signals=None,
    swing_idx=None,
    atr14=None,
):
    """VERBATIM copy of `backtest.engine.run_backtest`'s loop, reusing every
    engine helper, with ONE addition: after `check_exit` and `evaluate_watchman`
    both decline to close on a bar, an enabled deep-loss timeout may close the
    trade at that bar's close. Precedence exactly matches the task spec:
    existing exits (SL/TP/structure/time) always win; deep-loss only ever fires
    when none of them did. A `MODIFY_SL` from Watchman is applied only if
    deep-loss did not fire (identical to the engine when deep-loss is off).

    When `signals`/`swing_idx` (from `precompute_signals`) are given, the flat-bar
    signal derivation is a lookup instead of a live `config.signal_fn` call --
    a pure-function memoization, byte-identical to the inline path (fidelity-
    checked). Shield's stateful cooldown check is still run live (cheap, and its
    state legitimately differs per run as exit timing shifts trade selection)."""
    if len(df) < 2:
        return []
    memoized = signals is not None

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

    pending: _PendingOrder | None = None
    position: _OpenPosition | None = None
    trades = []

    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())

        if pending is not None:
            entry_price, spread_slippage_price_delta = _fill_entry_price(
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
            position = _OpenPosition(
                plan=pending.plan, lot_size=pending.lot_size, entry_time=entry_time,
                entry_price=entry_price, spread_slippage_price_delta=spread_slippage_price_delta,
                current_sl=pending.plan.stop_loss, metadata=metadata,
            )
            pending = None

        if position is not None:
            exit_result = check_exit(position.plan.direction, position.current_sl, position.plan.take_profit, bar)
            if exit_result is not None:
                exit_price, exit_reason = exit_result
                trades.append(_close_trade(
                    symbol, position, pd.Timestamp(bar["time"]), exit_price, exit_reason,
                    point_value, config.cost_model,
                ))
                equity += trades[-1].net_pnl
                position = None
            else:
                closed = False
                pending_sl: float | None = None
                if config.watchman_cfg is not None and position.metadata is not None:
                    decision = evaluate_watchman(
                        position_metadata=position.metadata,
                        current_sl=position.current_sl,
                        current_price=float(bar["close"]),
                        current_atr=float(atr14[i]) if memoized else _watchman_current_atr(df, i),
                        df=df, as_of_index=i,
                        now=pd.Timestamp(bar["time"]).to_pydatetime(),
                        config=config.watchman_cfg,
                    )
                    if decision.action == "CLOSE":
                        trades.append(_close_trade(
                            symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                            _classify_watchman_exit_reason(decision.reason),
                            point_value, config.cost_model,
                        ))
                        equity += trades[-1].net_pnl
                        position = None
                        closed = True
                    elif decision.action == "MODIFY_SL":
                        pending_sl = decision.new_stop_loss

                # ---- deep-loss timeout: only when nothing above closed the trade ----
                if not closed and deep_loss.enabled:
                    held = pd.Timestamp(bar["time"]).to_pydatetime() - position.entry_time.to_pydatetime()
                    if held >= pd.Timedelta(hours=deep_loss.y_hours).to_pytimedelta():
                        if _floating_r(position, float(bar["close"])) <= -deep_loss.x_r:
                            trades.append(_close_trade(
                                symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                                "deep_loss_timeout", point_value, config.cost_model,
                            ))
                            equity += trades[-1].net_pnl
                            position = None
                            closed = True

                if not closed and pending_sl is not None:
                    position.current_sl = pending_sl

        if position is None and pending is None:
            if memoized:
                plan = signals[i]
            else:
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
                swing_index = swing_idx[i] if memoized else _swing_index_at(df, i, plan.direction, config.pivot_bars)
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
                    pending = _PendingOrder(plan=plan, lot_size=lot, signal_index=i, swing_index=swing_index)

    if position is not None:
        last_bar = df.iloc[-1]
        trades.append(_close_trade(
            symbol, position, pd.Timestamp(last_bar["time"]), last_bar["close"], "end_of_data",
            point_value, config.cost_model,
        ))
    return trades


def _build_config(cfg, commission, equity) -> BacktestConfig:
    rv = RiskVoiceConfig(
        max_spread_multiple=cfg["risk_voice"]["max_spread_multiple"],
        max_spread_points_xauusd=cfg["risk_voice"]["max_spread_points_xauusd"],
        news_blackout_before_min=cfg["risk_voice"]["news_blackout_before_min"],
        news_blackout_after_min=cfg["risk_voice"]["news_blackout_after_min"],
        max_stop_atr_multiple=cfg["risk_voice"]["max_stop_atr_multiple"],
        session_start_hour=cfg["risk_voice"]["session_start_hour"],
        session_end_hour=cfg["risk_voice"]["session_end_hour"],
        friday_close_hour=cfg["risk_voice"]["friday_close_hour"],
        max_atr_panic_multiple=cfg["risk_voice"]["max_atr_panic_multiple"],
    )
    wm = WatchmanConfig(
        breakeven_at_r=cfg["watchman"]["breakeven_at_r"],
        trail_start_r=cfg["watchman"]["trail_start_r"],
        trail_distance_atr=cfg["watchman"]["trail_distance_atr"],
        time_stop_hours=cfg["watchman"]["time_stop_hours"],
        dead_trade_r_band=cfg["watchman"]["dead_trade_r_band"],
        breakeven_enabled=cfg["watchman"]["breakeven_enabled"],
        trail_enabled=cfg["watchman"]["trail_enabled"],
    )
    sh = ShieldConfig(
        min_rr=cfg["shield"]["min_rr"], max_correlation=cfg["shield"]["max_correlation"],
        max_positions_per_symbol=cfg["shield"]["max_positions_per_symbol"],
        max_positions_total=cfg["shield"]["max_positions_total"],
        total_risk_ceiling_pct=cfg["shield"]["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=cfg["shield"]["duplicate_signal_cooldown_hours"],
    )
    return BacktestConfig(
        starting_equity=equity, risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
        tp_r_multiple=cfg["order"]["tp_r_multiple"], pivot_bars=cfg["global"]["swing_pivot_bars"],
        risk_voice_cfg=rv, watchman_cfg=wm, shield_cfg=sh,
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
    )


def _trade_key(t):
    """Byte-for-byte comparison key for a ClosedTrade (fidelity check)."""
    return (
        str(t.entry_time), round(t.entry_price, 6), str(t.exit_time),
        round(t.exit_price, 6), t.exit_reason, round(t.lot_size, 6),
        round(t.net_pnl, 6), round(t.r_multiple, 6),
    )


def _metrics(trades, equity) -> dict:
    rep = generate_report(trades, equity)
    losers = [t for t in trades if t.net_pnl < 0]
    worst_net = min((t.net_pnl for t in trades), default=0.0)
    worst_r = min((t.r_multiple for t in trades), default=0.0)
    # left-tail severity: mean of the worst-10% net P&L (or worst 5 trades, whichever larger sample)
    net_sorted = sorted(t.net_pnl for t in trades)
    k = max(5, len(net_sorted) // 10)
    tail_mean = sum(net_sorted[:k]) / k if net_sorted else 0.0
    dl = [t for t in trades if t.exit_reason == "deep_loss_timeout"]
    return {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4),
        "PF": round(rep.profit_factor, 4) if rep.profit_factor is not None else None,
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 is not None else None,
        "worst_net": round(worst_net, 2),
        "worst_R": round(worst_r, 4),
        "tail_mean_worst10pct": round(tail_mean, 2),
        "n_losers": len(losers),
        "n_deeploss_exits": len(dl),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--windows", default="y1,y2,y3")
    p.add_argument("--x", default="0.5,0.7", help="deep-loss X magnitudes")
    p.add_argument("--y", default="4,8,12", help="deep-loss Y hold hours")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    base_config = _build_config(cfg, args.commission, args.equity)
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]
    xs = [float(v) for v in args.x.split(",") if v.strip()]
    ys = [float(v) for v in args.y.split(",") if v.strip()]

    # ---------------- FIDELITY CHECK (on y1: real engine vs my memoized re-sim) ----------------
    fsdf = _slice(df, *WINDOWS["y1"])
    ref = run_backtest(fsdf, "XAUUSD", _SPEC, base_config)
    fsig, fswing, fatr = precompute_signals(fsdf, "XAUUSD", _SPEC, base_config)
    mine_off = run_deeploss_backtest(fsdf, "XAUUSD", _SPEC, base_config, DeepLoss(enabled=False), fsig, fswing, fatr)
    mine_x5 = run_deeploss_backtest(fsdf, "XAUUSD", _SPEC, base_config, DeepLoss(enabled=True, x_r=5.0, y_hours=8.0), fsig, fswing, fatr)
    ok_off = [_trade_key(t) for t in ref] == [_trade_key(t) for t in mine_off]
    ok_x5 = [_trade_key(t) for t in ref] == [_trade_key(t) for t in mine_x5]
    print("FIDELITY " + json.dumps({
        "window": "y1", "ref_trades": len(ref), "mine_off_trades": len(mine_off), "mine_x5_trades": len(mine_x5),
        "identical_memoized_disabled": ok_off, "identical_X5_unreachable": ok_x5,
    }), flush=True)
    if not (ok_off and ok_x5):
        print("FIDELITY FAILED -- harness untrusted, aborting sweep.")
        return 1

    # ---------------- per-window: precompute signals ONCE, share across all configs ----------------
    # Reuse the fidelity y1 precompute (fsig/fswing/fatr) instead of recomputing it.
    precomputed = {"y1": (fsdf, fsig, fswing, fatr)}
    for w in windows:
        s, e = WINDOWS[w]
        if w in precomputed:
            wdf, sig, sw, at = precomputed[w]
        else:
            wdf = _slice(df, s, e)
            sig, sw, at = precompute_signals(wdf, "XAUUSD", _SPEC, base_config)
        trades = run_deeploss_backtest(wdf, "XAUUSD", _SPEC, base_config, DeepLoss(enabled=False), sig, sw, at)
        print("RESULT " + json.dumps({"cfg": "baseline", "x": None, "y": None, "window": w,
                                      **_metrics(trades, args.equity)}), flush=True)
        for x in xs:
            for y in ys:
                trades = run_deeploss_backtest(
                    wdf, "XAUUSD", _SPEC, base_config, DeepLoss(enabled=True, x_r=x, y_hours=y), sig, sw, at,
                )
                print("RESULT " + json.dumps({"cfg": "deeploss", "x": x, "y": y, "window": w,
                                              **_metrics(trades, args.equity)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
