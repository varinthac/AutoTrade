#!/usr/bin/env python3
"""EXP-019 (swap cost re-run) + EXP-020 (trend-regime filter) harness for XAUUSD.

Two mechanisms, one harness (they share the same verbatim engine-loop copy and
the same signal memoization, and EXP-020 must ALSO be measurable with swap on):

  EXP-019 (Option 1, cost-honesty ONLY, NO behavior change): re-run the current
    adopted config with `CostModelConfig.swap_model` set (long -53.2 / short
    +36.8 per lot per night, 3x Wednesday, per EXP-018's sourced rates) vs. the
    same run with swap OFF. Report PF/avgR/trades/DD and the Gate-1 picture.

  EXP-020 (Option 2, an ACTUAL strategy change): a trend-regime gate applied at
    signal-acceptance. Using ema(period) on H1 closed bars (a standard long-
    trend proxy already in features/indicators.py) and its slope over the last K
    bars: ALLOW a Council BUY only if close[i] > ema[i] AND ema[i] > ema[i-K];
    ALLOW a SELL only if close[i] < ema[i] AND ema[i] < ema[i-K]; otherwise SKIP
    the signal (flat / counter-trend regime => stand aside). This is a pure,
    causal function of the closed signalling bar i -- no look-ahead.

WHY A STANDALONE LOOP: identical rationale to exp017_deeploss_timeout_harness --
the regime gate must run at the signal-acceptance point of the engine's loop,
and src/ must not be modified. This re-implements backtest.engine.run_backtest's
loop VERBATIM, reusing every engine helper, adding ONLY (a) the regime gate at
the flat-bar accept step and (b) nothing for swap (the engine's own _close_trade
already folds swap in when cost_model.swap_model is set -- so EXP-019 needs no
loop change at all, just a cost_model with swap_model populated).

FIDELITY CHECK (run first, every time): with the regime gate DISABLED and swap
OFF, this loop must reproduce the real run_backtest output trade-for-trade. If
not, the harness is untrusted and no candidate number means anything.

Baseline = the live adopted config (be/trail OFF per EXP-008, structure+time ON,
tp 2.0, pivot 3, all-24h Risk Voice, Shield cooldown, min-lot cap 1.5, risk
1.0%), commission $0 (IC Markets Standard -- the current account per MEMORY),
$10k starting equity for a clean PF compare (matches EXP-017's per-year baseline
so y1/y2/y3 are directly comparable).
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
    _watchman_current_atr,
    check_exit,
    run_backtest,
)
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.features.indicators import atr, ema
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

# EXP-018's sourced IC Markets demo swap rates (broker sign: neg = charged).
SWAP = SwapModelConfig(long_per_lot_per_night=-53.2, short_per_lot_per_night=36.8)

WINDOWS = {
    "train":    ("2021-07-22", "2024-07-21"),
    "y1":       ("2021-07-22", "2022-07-21"),
    "y2":       ("2022-07-21", "2023-07-21"),
    "y3":       ("2023-07-21", "2024-07-21"),
    "val":      ("2024-07-21", "2025-07-21"),
    "trainval": ("2021-07-22", "2025-07-21"),
    # test intentionally NOT run (held-out; needs explicit user sign-off).
}


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


@dataclass(frozen=True)
class RegimeGate:
    """Trend-regime filter. `enabled=False` -> loop is byte-identical to the
    real engine. `period` = EMA lookback (long-trend proxy). `k` = slope
    lookback in bars (ema[i] vs ema[i-k])."""
    enabled: bool = False
    period: int = 200
    k: int = 24


def _regime_allows(direction: str, i: int, close: np.ndarray, ema_arr: np.ndarray, k: int) -> bool:
    if i - k < 0 or np.isnan(ema_arr[i]) or np.isnan(ema_arr[i - k]):
        return True  # insufficient history: don't gate (fail-open, causal)
    rising = ema_arr[i] > ema_arr[i - k]
    if direction == "BUY":
        return close[i] > ema_arr[i] and rising
    return close[i] < ema_arr[i] and not rising


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
            regime: RegimeGate, close_arr=None, ema_arr=None):
    """VERBATIM run_backtest loop + ONE addition: at the flat-bar accept step, an
    enabled regime gate may veto (set plan=None) before the Shield check. Swap
    needs NO loop change -- it lives in config.cost_model.swap_model, consumed by
    the reused _close_trade."""
    if len(df) < 2:
        return []
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

    pending = None
    position = None
    trades = []

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
                trades.append(_close_trade(
                    symbol, position, pd.Timestamp(bar["time"]), exit_price, exit_reason,
                    point_value, config.cost_model,
                ))
                equity += trades[-1].net_pnl
                position = None
            else:
                closed = False
                pending_sl = None
                if config.watchman_cfg is not None and position.metadata is not None:
                    decision = evaluate_watchman(
                        position_metadata=position.metadata, current_sl=position.current_sl,
                        current_price=float(bar["close"]), current_atr=float(atr14[i]),
                        df=df, as_of_index=i, now=pd.Timestamp(bar["time"]).to_pydatetime(),
                        config=config.watchman_cfg,
                    )
                    if decision.action == "CLOSE":
                        trades.append(_close_trade(
                            symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                            _classify_watchman_exit_reason(decision.reason), point_value, config.cost_model,
                        ))
                        equity += trades[-1].net_pnl
                        position = None
                        closed = True
                    elif decision.action == "MODIFY_SL":
                        pending_sl = decision.new_stop_loss
                if not closed and pending_sl is not None:
                    position.current_sl = pending_sl

        if position is None and pending is None:
            plan = signals[i]
            if plan is not None and regime.enabled and not _regime_allows(
                plan.direction, i, close_arr, ema_arr, regime.k
            ):
                plan = None
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
        trades.append(_close_trade(
            symbol, position, pd.Timestamp(last_bar["time"]), last_bar["close"], "end_of_data",
            point_value, config.cost_model,
        ))
    return trades


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


def _metrics(trades, equity):
    rep = generate_report(trades, equity)
    longs = [t for t in trades if t.direction == "BUY"]
    shorts = [t for t in trades if t.direction == "SELL"]
    return {
        "trades": rep.trade_count, "win_rate": round(rep.win_rate, 4),
        "PF": round(rep.profit_factor, 4) if rep.profit_factor is not None else None,
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 is not None else None,
        "n_long": len(longs), "n_short": len(shorts),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--mode", choices=["swap", "regime", "both"], default="both")
    p.add_argument("--windows", default="y1,y2,y3,val,trainval")
    p.add_argument("--periods", default="100,200,300")
    p.add_argument("--ks", default="12,24,48")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df_all = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    base_noswap = _build_config(cfg, args.commission, args.equity, swap_on=False)
    base_swap = _build_config(cfg, args.commission, args.equity, swap_on=True)
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]
    periods = [int(v) for v in args.periods.split(",") if v.strip()]
    ks = [int(v) for v in args.ks.split(",") if v.strip()]

    # FIDELITY (on y1): real engine == my re-sim, regime OFF + swap OFF.
    fsdf = _slice(df_all, *WINDOWS["y1"])
    ref = run_backtest(fsdf, "XAUUSD", _SPEC, base_noswap)
    fsig, fswing, fatr = precompute_signals(fsdf, "XAUUSD", _SPEC, base_noswap)
    mine = run_sim(fsdf, "XAUUSD", _SPEC, base_noswap, fsig, fswing, fatr, RegimeGate(enabled=False))
    ok = [_trade_key(t) for t in ref] == [_trade_key(t) for t in mine]
    print("FIDELITY " + json.dumps({"window": "y1", "ref_trades": len(ref),
          "mine_trades": len(mine), "identical": ok}), flush=True)
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
            sig, sw, at = precompute_signals(wdf, "XAUUSD", _SPEC, base_noswap)
        close_arr = wdf["close"].to_numpy()
        ema_cache = {per: ema(wdf["close"], per).to_numpy() for per in periods}

        # --- EXP-019: swap OFF vs ON, no behavior change ---
        if args.mode in ("swap", "both"):
            for tag, bcfg in (("swapOFF", base_noswap), ("swapON", base_swap)):
                tr = run_sim(wdf, "XAUUSD", _SPEC, bcfg, sig, sw, at, RegimeGate(enabled=False))
                print("RESULT " + json.dumps({"exp": "019", "cfg": tag, "period": None, "k": None,
                      "window": w, **_metrics(tr, args.equity)}), flush=True)

        # --- EXP-020: regime grid, swap OFF (baseline is swapOFF regime-off above) ---
        if args.mode in ("regime", "both"):
            for per in periods:
                for k in ks:
                    tr = run_sim(wdf, "XAUUSD", _SPEC, base_noswap, sig, sw, at,
                                 RegimeGate(enabled=True, period=per, k=k),
                                 close_arr=close_arr, ema_arr=ema_cache[per])
                    print("RESULT " + json.dumps({"exp": "020", "cfg": f"regime_p{per}_k{k}",
                          "period": per, "k": k, "window": w, **_metrics(tr, args.equity)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
