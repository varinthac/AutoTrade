#!/usr/bin/env python3
"""EXP-021 weekend-close-vs-hold harness for XAUUSD.

Compares (A) current behavior -- hold open positions across the Sat/Sun market
closure, rely on the broker SL only (which the engine models gap-aware) -- vs
(B) force-close every open position at the first Friday bar with server hour >=
friday_close_hour, at that bar's close.

WHY A STANDALONE LOOP (same rationale as exp019_020 / exp017): the weekend-close
must run at an exit point of the engine's per-bar loop, and src/ must NOT be
modified for an experiment. This re-implements backtest.engine.run_backtest's
loop VERBATIM, reusing every engine helper, adding ONLY the weekend-close exit
(checked AFTER the existing SL/TP check_exit so an intra-week SL/TP still wins,
and PRE-EMPTING the Watchman for that bar). It ALSO instruments, for every
closed trade, whether its exit bar is a weekend-gap bar and whether the SL
filled at a gapped-through open (the mechanism Option B is meant to remove).

FIDELITY CHECK (run first, every time): with the weekend-close DISABLED this
loop must reproduce real run_backtest trade-for-trade on y1. If not, untrusted.

Baseline = live adopted config (be/trail OFF per EXP-008, structure+time ON, tp
2.0, pivot 3, all-24h Risk Voice, Shield cooldown, min-lot cap 1.5, risk 1.0%),
commission $0 (IC Markets Standard), $10k equity (matches EXP-017/019 per-year).
Swap OFF for the headline compare (parity with the recorded baselines); a
swap-ON pass is available via --swap for a secondary read.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrade.backtest.clock import SimulatedClock
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig
from autotrade.backtest.engine import (
    BacktestConfig,
    _build_watchman_metadata,
    _classify_watchman_exit_reason,
    _close_trade,
    _fill_entry_price,
    _OpenPosition,
    _PendingOrder,
    _swing_index_at,
    check_exit,
    run_backtest,
)
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.features.indicators import atr
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

SWAP = SwapModelConfig(long_per_lot_per_night=-53.2, short_per_lot_per_night=36.8)

WINDOWS = {
    "y1":       ("2021-07-22", "2022-07-21"),
    "y2":       ("2022-07-21", "2023-07-21"),
    "y3":       ("2023-07-21", "2024-07-21"),
    "val":      ("2024-07-21", "2025-07-21"),
    "trainval": ("2021-07-22", "2025-07-21"),
    # test intentionally NOT run (held-out; explicit user sign-off required).
}


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


@dataclass(frozen=True)
class WeekendClose:
    """Force-close policy. `enabled=False` -> loop is byte-identical to the real
    engine. `cutoff_hour` = server hour on Friday at/after which any open
    position is force-closed at the bar close."""
    enabled: bool = False
    cutoff_hour: int = 20


def precompute_signals(df, symbol, symbol_spec, config):
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    signals = [None] * len(df)
    swing_idx = [None] * len(df)
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
    atr14 = atr(df["high"], df["low"], df["close"], period=14).to_numpy()
    return signals, swing_idx, atr14


def run_sim(df, symbol, symbol_spec, config, signals, swing_idx, atr14,
            weekend: WeekendClose, is_gap_bar: np.ndarray):
    """VERBATIM run_backtest loop + ONE addition: after the existing SL/TP
    check_exit, an enabled weekend-close may force-close the open position at the
    bar close on Friday hour >= cutoff (pre-empting the Watchman that bar).
    Returns (trades, tags) where tags[k] describes trade k's exit-bar context for
    the weekend-gap tail analysis: {gap_bar, gapped_sl_fill}."""
    if len(df) < 2:
        return [], []
    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    equity = config.starting_equity
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    shield = Shield(
        min_rr=config.shield_cfg.min_rr, max_correlation=config.shield_cfg.max_correlation,
        max_positions_per_symbol=config.shield_cfg.max_positions_per_symbol,
        max_positions_total=config.shield_cfg.max_positions_total,
        total_risk_ceiling_pct=config.shield_cfg.total_risk_ceiling_pct,
        duplicate_signal_cooldown_hours=config.shield_cfg.duplicate_signal_cooldown_hours,
    ) if config.shield_cfg is not None else None

    times = df["time"].to_numpy()
    dow = pd.DatetimeIndex(df["time"]).dayofweek.to_numpy()
    hour = pd.DatetimeIndex(df["time"]).hour.to_numpy()

    pending = None
    position = None
    trades = []
    tags = []

    def _append(tr, gap_bar, gapped_sl):
        trades.append(tr)
        tags.append({"gap_bar": bool(gap_bar), "gapped_sl_fill": bool(gapped_sl)})

    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())

        if pending is not None:
            entry_price, ssd = _fill_entry_price(
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
                entry_price=entry_price, spread_slippage_price_delta=ssd,
                current_sl=pending.plan.stop_loss, metadata=metadata,
            )
            pending = None

        if position is not None:
            exit_result = check_exit(position.plan.direction, position.current_sl, position.plan.take_profit, bar)
            if exit_result is not None:
                exit_price, exit_reason = exit_result
                # gapped_sl_fill: an SL that filled at the bar's OPEN because the
                # open gapped past the stop (the real weekend-gap loss mechanism).
                gapped = False
                if exit_reason == "stop_loss":
                    if position.plan.direction == "BUY":
                        gapped = bar["open"] <= position.current_sl
                    else:
                        gapped = bar["open"] >= position.current_sl
                _append(_close_trade(
                    symbol, position, pd.Timestamp(bar["time"]), exit_price, exit_reason,
                    point_value, config.cost_model,
                ), is_gap_bar[i], gapped)
                equity += trades[-1].net_pnl
                position = None
            elif weekend.enabled and dow[i] == 4 and hour[i] >= weekend.cutoff_hour:
                _append(_close_trade(
                    symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                    "weekend_close", point_value, config.cost_model,
                ), is_gap_bar[i], False)
                equity += trades[-1].net_pnl
                position = None
            elif config.watchman_cfg is not None and position.metadata is not None:
                decision = evaluate_watchman(
                    position_metadata=position.metadata, current_sl=position.current_sl,
                    current_price=float(bar["close"]), current_atr=float(atr14[i]),
                    df=df, as_of_index=i, now=pd.Timestamp(bar["time"]).to_pydatetime(),
                    config=config.watchman_cfg,
                )
                if decision.action == "CLOSE":
                    _append(_close_trade(
                        symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                        _classify_watchman_exit_reason(decision.reason), point_value, config.cost_model,
                    ), is_gap_bar[i], False)
                    equity += trades[-1].net_pnl
                    position = None
                elif decision.action == "MODIFY_SL":
                    position.current_sl = decision.new_stop_loss

        if position is None and pending is None:
            plan = signals[i]
            swing_index = None
            if plan is not None and shield is not None:
                swing_index = swing_idx[i]
                if swing_index is not None:
                    sd = shield.check(
                        order_plan=plan, symbol=symbol, open_positions=[],
                        new_trade_risk_pct=config.risk_per_trade_pct, swing_index=swing_index, clock=clock,
                    )
                    if sd.blocked:
                        plan = None
            if plan is not None:
                lot = compute_lot_size(
                    equity=equity, risk_per_trade_pct=config.risk_per_trade_pct,
                    entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
                    volume_min=symbol_spec.volume_min, volume_max=symbol_spec.volume_max,
                    volume_step=symbol_spec.volume_step, min_lot_risk_cap_pct=config.min_lot_risk_cap_pct,
                )
                if lot is not None and i + 1 < len(df):
                    pending = _PendingOrder(plan=plan, lot_size=lot, signal_index=i, swing_index=swing_index)

    if position is not None:
        last_bar = df.iloc[-1]
        _append(_close_trade(
            symbol, position, pd.Timestamp(last_bar["time"]), last_bar["close"], "end_of_data",
            point_value, config.cost_model,
        ), is_gap_bar[len(df) - 1], False)
    return trades, tags


def _build_config(cfg, commission, equity, swap_on) -> BacktestConfig:
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
        breakeven_at_r=cfg["watchman"]["breakeven_at_r"], trail_start_r=cfg["watchman"]["trail_start_r"],
        trail_distance_atr=cfg["watchman"]["trail_distance_atr"], time_stop_hours=cfg["watchman"]["time_stop_hours"],
        dead_trade_r_band=cfg["watchman"]["dead_trade_r_band"], breakeven_enabled=cfg["watchman"]["breakeven_enabled"],
        trail_enabled=cfg["watchman"]["trail_enabled"],
    )
    sh = ShieldConfig(
        min_rr=cfg["shield"]["min_rr"], max_correlation=cfg["shield"]["max_correlation"],
        max_positions_per_symbol=cfg["shield"]["max_positions_per_symbol"],
        max_positions_total=cfg["shield"]["max_positions_total"],
        total_risk_ceiling_pct=cfg["shield"]["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=cfg["shield"]["duplicate_signal_cooldown_hours"],
    )
    cm = CostModelConfig(commission_per_lot=commission, slippage_points=None,
                         swap_model=SWAP if swap_on else None)
    return BacktestConfig(
        starting_equity=equity, risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"], cost_model=cm,
        tp_r_multiple=cfg["order"]["tp_r_multiple"], pivot_bars=cfg["global"]["swing_pivot_bars"],
        risk_voice_cfg=rv, watchman_cfg=wm, shield_cfg=sh, min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
    )


def _trade_key(t):
    return (str(t.entry_time), round(t.entry_price, 6), str(t.exit_time),
            round(t.exit_price, 6), t.exit_reason, round(t.lot_size, 6),
            round(t.net_pnl, 6), round(t.r_multiple, 6))


def _tail(trades):
    nets = sorted(t.net_pnl for t in trades)
    n = len(nets)
    if n == 0:
        return {"worst": None, "worst10_mean": None}
    k = max(1, n // 10)
    return {"worst": round(nets[0], 2), "worst10_mean": round(float(np.mean(nets[:k])), 2), "worst10_n": k}


def _metrics(trades, tags, equity):
    rep = generate_report(trades, equity)
    # weekend-gap alignment on Option-A style runs: how many trades exited on a
    # gap bar, and (of SL exits) how many filled through a gapped open.
    gap_exits = [t for t, tag in zip(trades, tags) if tag["gap_bar"]]
    gapped_sl = [t for t, tag in zip(trades, tags) if tag["gapped_sl_fill"]]
    wk = [t for t in trades if t.exit_reason == "weekend_close"]
    return {
        "trades": rep.trade_count, "win_rate": round(rep.win_rate, 4),
        "PF": round(rep.profit_factor, 4) if rep.profit_factor is not None else None,
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 is not None else None,
        **_tail(trades),
        "n_gap_exit": len(gap_exits),
        "n_gapped_sl": len(gapped_sl),
        "gapped_sl_net": round(sum(t.net_pnl for t in gapped_sl), 2),
        "n_weekend_close": len(wk),
        "weekend_close_net": round(sum(t.net_pnl for t in wk), 2),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--windows", default="y1,y2,y3,val,trainval")
    p.add_argument("--cutoffs", default="18,20,22")
    p.add_argument("--swap", action="store_true")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df_all = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    base = _build_config(cfg, args.commission, args.equity, swap_on=args.swap)
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]
    cutoffs = [int(v) for v in args.cutoffs.split(",") if v.strip()]

    def gap_mask(wdf):
        gh = pd.DatetimeIndex(wdf["time"]).to_series().diff().dt.total_seconds().to_numpy() / 3600.0
        return gh > 30.0  # a Fri->Mon boundary (min observed 50h); NaN(first)->False

    # FIDELITY (on y1): real engine == my re-sim, weekend-close OFF, swap OFF.
    fbase = _build_config(cfg, args.commission, args.equity, swap_on=False)
    fsdf = _slice(df_all, *WINDOWS["y1"])
    ref = run_backtest(fsdf, "XAUUSD", _SPEC, fbase)
    fsig, fswing, fatr = precompute_signals(fsdf, "XAUUSD", _SPEC, fbase)
    mine, _ = run_sim(fsdf, "XAUUSD", _SPEC, fbase, fsig, fswing, fatr,
                      WeekendClose(enabled=False), gap_mask(fsdf))
    ok = [_trade_key(t) for t in ref] == [_trade_key(t) for t in mine]
    print("FIDELITY " + json.dumps({"window": "y1", "ref_trades": len(ref),
          "mine_trades": len(mine), "identical": ok, "swap": args.swap}), flush=True)
    if not ok:
        print("FIDELITY FAILED -- aborting.")
        return 1

    precomp = {"y1": (fsdf, fsig, fswing, fatr)}
    for w in windows:
        s, e = WINDOWS[w]
        if w in precomp:
            wdf, sig, sw, at = precomp[w]
        else:
            wdf = _slice(df_all, s, e)
            sig, sw, at = precompute_signals(wdf, "XAUUSD", _SPEC, base)
        gm = gap_mask(wdf)

        # Option A (baseline, hold over weekend). Also carries the weekend-gap
        # alignment instrumentation (gapped_sl_* / n_gap_exit).
        tr, tg = run_sim(wdf, "XAUUSD", _SPEC, base, sig, sw, at, WeekendClose(enabled=False), gm)
        print("RESULT " + json.dumps({"opt": "A", "cutoff": None, "window": w, **_metrics(tr, tg, args.equity)}), flush=True)

        # Option B (force-close before weekend) at each cutoff hour.
        for c in cutoffs:
            trb, tgb = run_sim(wdf, "XAUUSD", _SPEC, base, sig, sw, at, WeekendClose(enabled=True, cutoff_hour=c), gm)
            print("RESULT " + json.dumps({"opt": "B", "cutoff": c, "window": w, **_metrics(trb, tgb, args.equity)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
