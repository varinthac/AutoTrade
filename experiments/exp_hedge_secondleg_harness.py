#!/usr/bin/env python3
"""EXPLORATORY (NOT an EXP) — OPPOSITE-DIRECTION "hedge" second-position harness.

User idea (verbatim TH): "ในทางกลับกันถ้าทำ hedging ถ้า buy อยู่ แล้ว sell สวนมา
อีกไม้จะเป็นยังไง ทำเฉพาะ Profile เริ่มติดลบมากๆ" — conversely to the martingale
probe, HEDGE: while leg 1 (say BUY) is open and its floating loss reaches −X·R,
open leg 2 in the OPPOSITE direction (SELL) as a hedge. Same lot as leg 1 (so the
pair is NET-FLAT while both open). This is a *different mechanism* from the
martingale add (which doubled the SAME directional bet) and must be judged on its
own risk shape, not assumed to fail because martingale did.

TRAIN-ONLY (2021-07-22 → 2024-07-21). Validation and Test DELIBERATELY NOT TOUCHED
— risk-first diagnostic, same convention as the martingale NOTE. Baseline B0 MUST
reproduce the stock run_backtest on the Train window (fidelity asserted at start).

Everything reuses production code (engine signal fn / check_exit / cost model /
Watchman / Shield / sizing / order construction) under the CURRENTLY ADOPTED config
(pivot 3, tp 2.0, all-24h [0,24), be/trail OFF, structure+time-stop ON, Shield
cooldown ON, min_lot_risk_cap 1.5, risk 1.0%). Start equity $3,000. Commission $0
(IC Markets *Standard*, per project memory); spread baked per-bar (min-1-spread).

--- THE EXIT RULE IS THE CRUX OF A HEDGE (unlike martingale's independent adds) ---
A hedge is a NET-FLAT position while both legs are open, so "when does each leg
close?" defines the whole risk/reward. Two well-motivated rules are tested:

  (Ha) INDEPENDENT MIRRORED EXITS — the direct analog of the martingale harness's
       leg-2 handling, flipped in direction. Hedge SELL gets its own SL (=entry+d)
       and 2R TP (=entry−2d), where d = leg-1 stop distance. Both legs then exit
       independently via their own SL/TP/Watchman. Simple, symmetric, maximally
       comparable to the martingale test. Failure mode it exposes: WHIPSAW — price
       stops leg 1 out AND then reverses to stop the hedge out too (−2R pair).

  (Hb) LOCK-AND-RELEASE — the classic "cap the loss, wait" hedge intent. The hedge
       has no exit of its own; instead: (1) if leg 1 recovers to breakeven (float
       ≥ 0), CLOSE the hedge (lock its small loss) and let leg 1 ride its own TP/SL
       — you "paid" the hedge cost to survive the dip; (2) if leg 1 hits its own
       SL/Watchman exit while hedged, CLOSE BOTH the same bar — the hedge has by
       then offset part of leg-1's loss, so the *combined* loss is CAPPED below the
       plain −1R. The hedge can never outlive leg 1. Exit price for the hedge leg =
       bar close (realistic, not the optimistic leg-1 SL touch).

"Net position" P&L is honest: mark-to-market (MTM) equity = realized + floating of
BOTH legs each bar; while both are open and same-lot, their floats offset (net
flat), so MTM is frozen — that IS the hedge. Per-EPISODE combined P&L (leg1 total +
hedge total, from equity_at_trigger to fully-flat) is tracked to show whether the
hedge actually CAPS the tail (the martingale FATTENED it: worst single −45→−113,
worst streak −276→−546).

Trigger X = fraction of leg-1's own initial risk R (regime-normalized). Swept at
−0.5R and −1.0R for each rule. Sizing = same lot as leg 1 (net-flat; the simplest,
most directly comparable variant; a bigger hedge would just re-introduce net
directional exposure, i.e. a disguised reversal, out of scope here).

No src/ or config/ file is modified. Read-only reuse of production functions.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import pandas as pd

import autotrade.backtest.engine as eng
from autotrade.backtest.clock import SimulatedClock
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import (
    BacktestConfig,
    ClosedTrade,
    _OpenPosition,
    _build_watchman_metadata,
    _close_trade,
    _fill_entry_price,
    _swing_index_at,
    check_exit,
    run_backtest,
)
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan
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
_POINT_VALUE = _SPEC.tick_value / _SPEC.tick_size  # 100.0

TRAIN_START = "2021-07-22"
TRAIN_END = "2024-07-21"


def _cfgs(cfg):
    rv = cfg["risk_voice"]
    wm = cfg["watchman"]
    sh = cfg["shield"]
    rvc = RiskVoiceConfig(
        max_spread_multiple=rv["max_spread_multiple"],
        max_spread_points_xauusd=rv["max_spread_points_xauusd"],
        news_blackout_before_min=rv["news_blackout_before_min"],
        news_blackout_after_min=rv["news_blackout_after_min"],
        max_stop_atr_multiple=rv["max_stop_atr_multiple"],
        session_start_hour=rv["session_start_hour"],
        session_end_hour=rv["session_end_hour"],
        friday_close_hour=rv["friday_close_hour"],
        max_atr_panic_multiple=rv["max_atr_panic_multiple"],
    )
    wmc = WatchmanConfig(
        breakeven_at_r=wm["breakeven_at_r"], trail_start_r=wm["trail_start_r"],
        trail_distance_atr=wm["trail_distance_atr"], time_stop_hours=wm["time_stop_hours"],
        dead_trade_r_band=wm["dead_trade_r_band"],
        breakeven_enabled=wm["breakeven_enabled"], trail_enabled=wm["trail_enabled"],
    )
    shc = ShieldConfig(
        min_rr=sh["min_rr"], max_correlation=sh["max_correlation"],
        max_positions_per_symbol=sh["max_positions_per_symbol"],
        max_positions_total=sh["max_positions_total"],
        total_risk_ceiling_pct=sh["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=sh["duplicate_signal_cooldown_hours"],
    )
    return rvc, wmc, shc


def _floating(pos: _OpenPosition, price: float) -> float:
    sign = 1.0 if pos.plan.direction == "BUY" else -1.0
    return sign * (price - pos.entry_price) * _POINT_VALUE * pos.lot_size


def _risk_usd(pos: _OpenPosition) -> float:
    return pos.plan.stop_distance * _POINT_VALUE * pos.lot_size


def _make_hedge_plan(leg1: _OpenPosition, entry: float) -> OrderPlan:
    """OPPOSITE direction to leg 1, mirroring leg-1's stop DISTANCE d. For Ha this
    SL/TP governs the hedge's independent exit; for Hb it is a placeholder (the
    lock-and-release logic drives the hedge, not these levels)."""
    d = leg1.plan.stop_distance
    if leg1.plan.direction == "BUY":
        hd, sl, tp = "SELL", entry + d, entry - 2.0 * d
    else:
        hd, sl, tp = "BUY", entry - d, entry + 2.0 * d
    return OrderPlan(direction=hd, entry=entry, stop_loss=sl, take_profit=tp, stop_distance=d)


@dataclass
class _Pending:
    plan: OrderPlan
    lot: float
    signal_index: int
    swing_index: int | None
    is_leg2: bool


def precompute_signals(df, cfg):
    from autotrade.features.indicators import atr as _atr
    rvc, _, _ = _cfgs(cfg)
    pivot = cfg["global"]["swing_pivot_bars"]
    tp_r = cfg["order"]["tp_r_multiple"]
    bt = cfg["order"]
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    cache = [None] * len(df)
    for i in range(len(df)):
        clock.set(pd.Timestamp(df["time"].iloc[i]).to_pydatetime())
        plan = eng._council_signal_fn(
            df, i, symbol="XAUUSD", symbol_spec=_SPEC,
            sl_buffer_atr=bt["sl_buffer_atr"], sl_min_atr=bt["sl_min_atr"],
            sl_max_atr=bt["sl_max_atr"], tp_r_multiple=tp_r, pivot_bars=pivot,
            bull_threshold=cfg["council"]["bull_threshold"],
            bear_threshold=cfg["council"]["bear_threshold"],
            conflict_threshold=cfg["council"]["conflict_threshold"],
            risk_voice_cfg=rvc, clock=clock,
        )
        if plan is not None:
            swing_index = _swing_index_at(df, i, plan.direction, pivot)
            cache[i] = (plan, swing_index)
    atr14 = _atr(df["high"], df["low"], df["close"], period=14).to_numpy()
    return cache, atr14


def run_variant(df, cfg, *, variant, trigger_r, commission, start_equity,
                cache=None, atr14=None):
    """variant in {"baseline","Ha","Hb"}. Hedge leg is always same-lot (net-flat)."""
    if cache is None:
        cache, atr14 = precompute_signals(df, cfg)
    rvc, wmc, shc = _cfgs(cfg)
    pivot = cfg["global"]["swing_pivot_bars"]
    risk_pct = cfg["cfo"]["risk_per_trade_pct"]
    cap = cfg["cfo"]["min_lot_risk_cap_pct"]
    bt = cfg["order"]
    cm = CostModelConfig(commission_per_lot=commission, slippage_points=None)
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    shield = Shield(
        min_rr=shc.min_rr, max_correlation=shc.max_correlation,
        max_positions_per_symbol=shc.max_positions_per_symbol,
        max_positions_total=shc.max_positions_total,
        total_risk_ceiling_pct=shc.total_risk_ceiling_pct,
        duplicate_signal_cooldown_hours=shc.duplicate_signal_cooldown_hours,
    )

    equity = start_equity
    slotA: _OpenPosition | None = None
    slotB: _OpenPosition | None = None   # the hedge
    pendA: _Pending | None = None
    pendB: _Pending | None = None
    episode_used = False
    cur_ep: dict | None = None

    trades: list[ClosedTrade] = []
    mtm_curve: list[tuple[pd.Timestamp, float]] = []
    episodes: list[dict] = []

    def signal_at(i):
        cached = cache[i]
        if cached is None:
            return None
        plan, swing_index = cached
        if swing_index is not None:
            dec = shield.check(order_plan=plan, symbol="XAUUSD", open_positions=[],
                               new_trade_risk_pct=risk_pct, swing_index=swing_index, clock=clock)
            if dec.blocked:
                return None
        lot = compute_lot_size(
            equity=equity, risk_per_trade_pct=risk_pct, entry=plan.entry,
            stop_loss=plan.stop_loss, point_value=_POINT_VALUE,
            volume_min=_SPEC.volume_min, volume_max=_SPEC.volume_max,
            volume_step=_SPEC.volume_step, min_lot_risk_cap_pct=cap,
        )
        if lot is None:
            return None
        return _Pending(plan=plan, lot=lot, signal_index=i, swing_index=swing_index, is_leg2=False)

    def fill(pend: _Pending, bar) -> _OpenPosition:
        entry_price, delta = _fill_entry_price(pend.plan.direction, bar["open"], bar["spread"], _SPEC, cm)
        etime = pd.Timestamp(bar["time"])
        if pend.swing_index is not None:
            shield.record_trade_opened(symbol="XAUUSD", direction=pend.plan.direction,
                                       opened_at=etime.to_pydatetime(), swing_index=pend.swing_index)
        meta = _build_watchman_metadata("XAUUSD", pend.plan, entry_price, etime, df, pend.signal_index, pivot)
        return _OpenPosition(plan=pend.plan, lot_size=pend.lot, entry_time=etime,
                             entry_price=entry_price, spread_slippage_price_delta=delta,
                             current_sl=pend.plan.stop_loss, metadata=meta)

    def try_exit(pos: _OpenPosition, i, bar) -> ClosedTrade | None:
        er = check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar)
        if er is not None:
            xp, xr = er
            return _close_trade("XAUUSD", pos, pd.Timestamp(bar["time"]), xp, xr, _POINT_VALUE, cm)
        if pos.metadata is not None:
            dec = evaluate_watchman(
                position_metadata=pos.metadata, current_sl=pos.current_sl,
                current_price=float(bar["close"]), current_atr=float(atr14[i]),
                df=df, as_of_index=i, now=pd.Timestamp(bar["time"]).to_pydatetime(), config=wmc,
            )
            if dec.action == "CLOSE":
                return _close_trade("XAUUSD", pos, pd.Timestamp(bar["time"]), float(bar["close"]),
                                    eng._classify_watchman_exit_reason(dec.reason), _POINT_VALUE, cm)
            if dec.action == "MODIFY_SL":
                pos.current_sl = dec.new_stop_loss
        return None

    n = len(df)
    for i in range(n):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())
        close = float(bar["close"])

        # 1) fills
        if pendA is not None:
            slotA = fill(pendA, bar); pendA = None
        if pendB is not None:
            slotB = fill(pendB, bar); pendB = None

        # 2) exits
        if variant == "Hb" and slotB is not None and slotA is not None:
            # lock-and-release: hedge cannot outlive leg 1
            t1 = try_exit(slotA, i, bar)
            if t1 is not None:
                # leg 1 exited (SL/TP/Watchman) -> close hedge SAME bar at close (capped loss)
                trades.append(t1); equity += t1.net_pnl; slotA = None
                th = _close_trade("XAUUSD", slotB, pd.Timestamp(bar["time"]), close,
                                  "hedge_release_leg1_exit", _POINT_VALUE, cm)
                trades.append(th); equity += th.net_pnl; slotB = None
            elif _floating(slotA, close) >= 0.0:
                # leg 1 recovered to breakeven -> lock the hedge loss, let leg 1 ride on
                th = _close_trade("XAUUSD", slotB, pd.Timestamp(bar["time"]), close,
                                  "hedge_release_breakeven", _POINT_VALUE, cm)
                trades.append(th); equity += th.net_pnl; slotB = None
        else:
            # baseline / Ha: each leg exits independently
            if slotA is not None:
                t = try_exit(slotA, i, bar)
                if t is not None:
                    trades.append(t); equity += t.net_pnl; slotA = None
            if slotB is not None:
                t = try_exit(slotB, i, bar)
                if t is not None:
                    trades.append(t); equity += t.net_pnl; slotB = None

        # episode bookkeeping: reset only when FULLY flat
        if slotA is None and slotB is None and pendA is None and pendB is None:
            if cur_ep is not None:
                cur_ep["combined_pnl"] = round(equity - cur_ep["equity_at_trigger"], 2)
                episodes.append(cur_ep); cur_ep = None
            episode_used = False

        # 3) mark-to-market (realized + floating of both legs; net-flat while hedged)
        floatA = _floating(slotA, close) if slotA is not None else 0.0
        floatB = _floating(slotB, close) if slotB is not None else 0.0
        mtm = equity + floatA + floatB
        mtm_curve.append((pd.Timestamp(bar["time"]), mtm))
        if cur_ep is not None:
            cur_ep["min_mtm_float"] = min(cur_ep["min_mtm_float"], floatA + floatB)
            cur_ep["min_mtm_equity"] = min(cur_ep["min_mtm_equity"], mtm)

        # 4) hedge trigger (only when exactly leg A open, hedge slot free, not used this episode)
        if variant in ("Ha", "Hb") and slotA is not None and slotB is None and pendB is None and not episode_used:
            if floatA <= -trigger_r * _risk_usd(slotA):
                plan2 = _make_hedge_plan(slotA, close)
                lot2 = slotA.lot_size  # SAME lot -> net-flat while both open
                pendB = _Pending(plan=plan2, lot=lot2, signal_index=i, swing_index=None, is_leg2=True)
                episode_used = True
                cur_ep = {"kind": variant, "trigger_time": pd.Timestamp(bar["time"]),
                          "leg1_risk": _risk_usd(slotA), "leg1_lot": slotA.lot_size,
                          "leg2_lot": lot2, "equity_at_trigger": equity,
                          "price_at_trigger": close, "direction": slotA.plan.direction,
                          "min_mtm_float": floatA, "min_mtm_equity": mtm}

        # 5) primary signal only when FULLY flat (bounds concurrency to 2)
        if slotA is None and pendA is None and slotB is None and pendB is None:
            p = signal_at(i)
            if p is not None and i + 1 < n:
                pendA = p

    # close any open legs at end of data
    last = df.iloc[-1]
    for pos in (slotA, slotB):
        if pos is not None:
            trades.append(_close_trade("XAUUSD", pos, pd.Timestamp(last["time"]),
                                       float(last["close"]), "end_of_data", _POINT_VALUE, cm))
            equity += trades[-1].net_pnl
    if cur_ep is not None:
        cur_ep["combined_pnl"] = round(equity - cur_ep["equity_at_trigger"], 2)
        episodes.append(cur_ep)

    return trades, mtm_curve, episodes, equity


# ---------------- metrics ----------------

def _pf(trades):
    gp = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gl = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


def _mtm_maxdd(curve):
    peak = curve[0][1] if curve else 0.0
    worst_pct = 0.0; worst_usd = 0.0; trough = curve[0][1] if curve else 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = peak - v
        if peak > 0 and dd / peak > worst_pct:
            worst_pct = dd / peak
        worst_usd = max(worst_usd, dd)
        trough = min(trough, v)
    return worst_pct * 100.0, worst_usd, trough


def _worst_streak(trades):
    worst = 0.0; run = 0.0
    for t in trades:
        if t.net_pnl < 0:
            run += t.net_pnl; worst = min(worst, run)
        else:
            run = 0.0
    return worst


def _per_year_pf(trades):
    out = {}
    for t in trades:
        y = t.exit_time.year
        out.setdefault(y, []).append(t.net_pnl)
    return {y: (round(_pf([type("x", (), {"net_pnl": p}) for p in v]), 3),
                round(sum(v), 1), len(v)) for y, v in sorted(out.items())}


def summarize(label, trades, curve, episodes, final_equity, start_equity):
    net = sum(t.net_pnl for t in trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    ddpct, ddusd, trough = _mtm_maxdd(curve)
    hedge_eps = [e for e in episodes if "combined_pnl" in e]
    worst_combined = min((e["combined_pnl"] for e in hedge_eps), default=0.0)
    n_hedge_neg = sum(1 for e in hedge_eps if e["combined_pnl"] < 0)
    combined_sum = round(sum(e["combined_pnl"] for e in hedge_eps), 1)
    ep_floats = sorted(episodes, key=lambda e: e.get("combined_pnl", 0.0))[:10]
    out = {
        "label": label,
        "trades": len(trades),
        "win_rate": round(100 * wins / len(trades), 1) if trades else None,
        "PF": round(_pf(trades), 4) if _pf(trades) != float("inf") else "inf",
        "net": round(net, 1),
        "final_equity": round(final_equity, 1),
        "mtm_maxDD_pct": round(ddpct, 2),
        "mtm_maxDD_usd": round(ddusd, 1),
        "mtm_trough_equity": round(trough, 1),
        "worst_single_loss": round(min((t.net_pnl for t in trades), default=0.0), 2),
        "worst_streak_usd": round(_worst_streak(trades), 1),
        "n_hedge_episodes": len(hedge_eps),
        "n_hedge_neg": n_hedge_neg,
        "worst_combined_episode": round(worst_combined, 2),
        "hedge_episodes_net": combined_sum,
        "per_year": _per_year_pf(trades),
        "worst10_combined_episode": [
            {"t": str(e["trigger_time"]), "combined": round(e.get("combined_pnl", 0.0), 1),
             "min_float": round(e.get("min_mtm_float", 0.0), 1)} for e in ep_floats
        ],
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--equity", type=float, default=3000.0)
    args = ap.parse_args()

    cfg = load_yaml_config("base")
    df_all = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    df = df_all[(df_all["time"] >= TRAIN_START) & (df_all["time"] < TRAIN_END)].reset_index(drop=True)
    print(f"# Train bars: {len(df)}  {df['time'].iloc[0]} -> {df['time'].iloc[-1]}", flush=True)

    # ---- fidelity: harness baseline vs stock run_backtest on a fast slice ----
    rvc, wmc, shc = _cfgs(cfg)
    df_fid = df.iloc[:5000].reset_index(drop=True)
    stock_bt = BacktestConfig(
        starting_equity=args.equity, risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(commission_per_lot=args.commission, slippage_points=None),
        sl_buffer_atr=cfg["order"]["sl_buffer_atr"], sl_min_atr=cfg["order"]["sl_min_atr"],
        sl_max_atr=cfg["order"]["sl_max_atr"], tp_r_multiple=cfg["order"]["tp_r_multiple"],
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        bull_threshold=cfg["council"]["bull_threshold"], bear_threshold=cfg["council"]["bear_threshold"],
        conflict_threshold=cfg["council"]["conflict_threshold"],
        risk_voice_cfg=rvc, watchman_cfg=wmc, shield_cfg=shc,
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
    )
    stock_trades = run_backtest(df_fid, "XAUUSD", _SPEC, stock_bt)
    fid_cache, fid_atr = precompute_signals(df_fid, cfg)
    h_trades, _hc, _he, _hf = run_variant(
        df_fid, cfg, variant="baseline", trigger_r=0.0,
        commission=args.commission, start_equity=args.equity, cache=fid_cache, atr14=fid_atr)
    fid = {"slice_bars": len(df_fid),
           "stock_trades": len(stock_trades), "stock_pf": round(_pf(stock_trades), 4),
           "stock_net": round(sum(t.net_pnl for t in stock_trades), 2),
           "harness_trades": len(h_trades), "harness_pf": round(_pf(h_trades), 4),
           "harness_net": round(sum(t.net_pnl for t in h_trades), 2)}
    print("FIDELITY " + json.dumps(fid), flush=True)
    assert fid["stock_trades"] == fid["harness_trades"], "FIDELITY FAIL: trade count"
    assert abs(fid["stock_net"] - fid["harness_net"]) < 1.0, "FIDELITY FAIL: net"

    print("# precomputing full-Train signals...", flush=True)
    cache, atr14 = precompute_signals(df, cfg)
    print("# precompute done", flush=True)

    conditions = [
        ("B0_baseline", "baseline", 0.0),
        ("Ha_indep_0.5R", "Ha", 0.5),
        ("Ha_indep_1.0R", "Ha", 1.0),
        ("Hb_lock_0.5R", "Hb", 0.5),
        ("Hb_lock_1.0R", "Hb", 1.0),
    ]
    for label, variant, tr in conditions:
        trades, curve, episodes, final_eq = run_variant(
            df, cfg, variant=variant, trigger_r=tr,
            commission=args.commission, start_equity=args.equity, cache=cache, atr14=atr14)
        s = summarize(label, trades, curve, episodes, final_eq, args.equity)
        print("RESULT " + json.dumps(s), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
