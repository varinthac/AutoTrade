#!/usr/bin/env python3
"""EXP-010 harness: H1 Council bias/veto + M30 entry-TIMING hybrid.

WHAT THIS IS (see experiments/experiments_log.md ## EXP-010 for the full
pre-registration, §0-§8). The H1 Council fires its signal/bias/veto EXACTLY as
production does (same signals, same all-24h gate, same be/trail-OFF Watchman,
same tp_r_multiple=2.0). Instead of filling at H1 bar-i+1's OPEN with the H1 ATR
stop, we ARM a window of `N` M30 bars and enter on an M30 "pullback-then-resume"
trigger with an M30-swing-structure stop (tighter than the H1 ATR stop).

PRODUCTION IS UNTOUCHED. This file lives in experiments/ and only REUSES the
production pure functions (council/order_construction.build_order_plan,
council/risk_voice.check_risk_voice, watchman/evaluate.evaluate_watchman,
features/swing via engine._council_signal_fn, features/indicators.atr,
risk/sizing.compute_lot_size, backtest/cost_model, backtest/engine.{
_council_signal_fn, ClosedTrade, check_exit}, backtest/report.generate_report)
exactly like the EXP-005/007/009 harnesses.

TWO ambiguities the pre-registration does not itself resolve, and the
conservative/defensible calls made here (documented per the analysis-only mandate):

  (1) §2 pins the stop anchor as "pb_low" (the pullback extreme) AND calls it
      "off the M30 swing structure", while §3 sweeps "M30 pivot_bars (swing
      lookback for the stop)". The house swing function needs `pivot_bars` bars of
      RIGHT-side confirmation, which lags PAST the entry bar and would return a
      stale, FAR swing -> a WIDE stop, contradicting §2's tight-stop thesis and F4.
      So the stop anchor here is the pullback pivot low/high found WITHIN the
      arming window as a LEFT-side fractal of depth `pivot_bars` (no lookahead): a
      bar whose low is strictly below the lows of the `pivot_bars` bars immediately
      before it. Keeps the stop tight (M30-scale), keeps `pivot_bars` load-bearing
      (larger lookback => deeper pullback => the F3 starvation the pre-registration
      anticipated at small-N/large-pivot cells), and matches §2's literal
      "SL = pb_low - buffer*ATR_M30".

  (2) The M30 SL is built by the SAME production build_order_plan clamp, passing
      the M30-ATR(14) and the M30 pullback pivot as `swing_price` -- so sl_buffer/
      min/max_atr act in M30-ATR units (§2's pinned ATR-unit decision), and the
      sl_min_atr=0.8 floor stays proportional to M30 volatility (F4).

Fill convention mirrors the H1 engine one timeframe down: decide on an M30 bar's
CLOSE, fill at the NEXT M30 bar's OPEN (+ modeled spread/slippage). SL/TP are the
price LEVELS fixed at decision time (off the decision-bar close), exactly like the
engine fixes them off close[i] and fills at open[i+1].

Watchman on the M30 clock: structure-invalidation (against the M30 pullback pivot
that anchored the stop) + time-stop ALWAYS ON; breakeven/trail OFF -- current-LIVE
config one timeframe down (§6(b)(3)).

COMMISSION: IC Markets **Standard = $0/lot** (cost entirely in the spread, which
the zero-floor fix already handles). Pass --commission 0.0 (NOT the stale $7 in
the pre-registration's §6(b) note, a Raw-Spread assumption since corrected).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from autotrade.backtest.clock import SimulatedClock
from autotrade.backtest.cost_model import CostModelConfig, commission_cost, spread_slippage_price
from autotrade.backtest.engine import ClosedTrade, _council_signal_fn, check_exit
from autotrade.backtest.news_stub import NoHistoricalNewsDataProvider
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan, build_order_plan
from autotrade.council.risk_voice import RiskVoiceConfig, check_risk_voice
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.features.indicators import atr
from autotrade.risk.sizing import compute_lot_size
from autotrade.watchman.evaluate import WatchmanConfig, evaluate_watchman
from autotrade.watchman.position_metadata import PositionMetadata

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)
_POINT_VALUE = _SPEC.tick_value / _SPEC.tick_size  # 100 $/price-unit/lot
_NEWS = NoHistoricalNewsDataProvider()  # "no event" -> never a real news veto (same as engine)

WINDOWS = {
    "train": ("2021-07-22", "2024-07-21"),
    "val":   ("2024-07-21", "2025-07-21"),
    "test":  ("2025-07-21", "2026-07-21"),
    "y1":    ("2021-07-22", "2022-07-21"),
    "y2":    ("2022-07-21", "2023-07-21"),
    "y3":    ("2023-07-21", "2024-07-21"),
    "y4":    ("2024-07-21", "2025-07-21"),  # == val
}

# §6(c): the M30 layer must NOT reuse indicators.rolling_average (480 H1 bars =
# 20 days; on M30 that is only 10 days). 20 trading days in M30 bars = 2*480 = 960.
M30_20D_BARS = 960
M30_ATR_PERIOD = 14


# --------------------------------------------------------------------------- #
# Precomputed, window-independent M30 arrays (built ONCE, reused across cells)
# --------------------------------------------------------------------------- #
@dataclass
class M30Data:
    df: pd.DataFrame           # kept for evaluate_watchman's structure-invalidation
    op: np.ndarray
    hi: np.ndarray
    lo: np.ndarray
    cl: np.ndarray
    sp: np.ndarray
    minute: np.ndarray
    atr: np.ndarray
    avg_sp_960: np.ndarray
    avg_atr_960: np.ndarray
    times: list                # list[pd.Timestamp]
    pydt: list                 # list[datetime]


def build_m30_data(m30: pd.DataFrame) -> M30Data:
    atr_s = atr(m30["high"], m30["low"], m30["close"], period=M30_ATR_PERIOD)
    times = [pd.Timestamp(x) for x in m30["time"]]
    return M30Data(
        df=m30,
        op=m30["open"].to_numpy(float), hi=m30["high"].to_numpy(float),
        lo=m30["low"].to_numpy(float), cl=m30["close"].to_numpy(float),
        sp=m30["spread"].to_numpy(float), minute=m30["time"].dt.minute.to_numpy(),
        atr=atr_s.to_numpy(float),
        avg_sp_960=m30["spread"].rolling(M30_20D_BARS, min_periods=1).mean().to_numpy(float),
        avg_atr_960=atr_s.rolling(M30_20D_BARS, min_periods=1).mean().to_numpy(float),
        times=times, pydt=[t.to_pydatetime() for t in times],
    )


# --------------------------------------------------------------------------- #
# H1 signal pre-computation (pure; window-independent given the slice; cache once)
# --------------------------------------------------------------------------- #
def compute_h1_signals(h1_slice, cfg, rv_cfg, *, tp, pivot_bars) -> dict:
    """Signals for a WINDOW-SLICED, reset-index H1 frame -> {open_time: OrderPlan}.
    Sliced per window (cold-start indicators from the window's own start bar) to be
    byte-apples-to-apples with the H1-as-is baseline harnesses (exp_reverify/exp009
    both `_slice(df).reset_index()` then run_backtest, so evaluate_council warms
    indicators from the slice start, NOT full history)."""
    o = cfg["order"]
    out: dict = {}
    clock = SimulatedClock(pd.Timestamp(h1_slice["time"].iloc[0]).to_pydatetime())
    for i in range(len(h1_slice)):
        t = pd.Timestamp(h1_slice["time"].iloc[i])
        clock.set(t.to_pydatetime())
        plan = _council_signal_fn(
            h1_slice, i, symbol="XAUUSD", symbol_spec=_SPEC,
            sl_buffer_atr=o["sl_buffer_atr"], sl_min_atr=o["sl_min_atr"],
            sl_max_atr=o["sl_max_atr"], tp_r_multiple=tp, pivot_bars=pivot_bars,
            bull_threshold=cfg["council"]["bull_threshold"],
            bear_threshold=cfg["council"]["bear_threshold"],
            conflict_threshold=cfg["council"]["conflict_threshold"],
            risk_voice_cfg=rv_cfg, clock=clock,
        )
        if plan is not None:
            out[t] = plan
    return out


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class _Armed:
    direction: str
    window_end: int
    pb_index: Optional[int] = None


@dataclass
class _Open:
    plan: OrderPlan
    lot: float
    entry_time: pd.Timestamp
    entry_price: float
    ss_delta: float
    current_sl: float
    metadata: Optional[PositionMetadata]


def _close(direction, entry_time, entry_price, ss_delta, exit_time, exit_price,
           exit_reason, lot, stop_distance, cm) -> ClosedTrade:
    """Mirrors engine._close_trade math exactly."""
    sign = 1.0 if direction == "BUY" else -1.0
    gross = sign * (exit_price - entry_price) * _POINT_VALUE * lot
    cost = commission_cost(lot, cm)
    net = gross - cost
    risk_amount = stop_distance * _POINT_VALUE * lot
    r = net / risk_amount if risk_amount else 0.0
    return ClosedTrade(
        symbol="XAUUSD", direction=direction, entry_time=entry_time, entry_price=entry_price,
        exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason, lot_size=lot,
        gross_pnl=gross, cost=cost, spread_slippage_cost=ss_delta * _POINT_VALUE * lot,
        net_pnl=net, r_multiple=r,
    )


def _lot(equity, risk_pct, plan: OrderPlan, min_lot_cap):
    return compute_lot_size(
        equity=equity, risk_per_trade_pct=risk_pct, entry=plan.entry, stop_loss=plan.stop_loss,
        point_value=_POINT_VALUE, volume_min=_SPEC.volume_min, volume_max=_SPEC.volume_max,
        volume_step=_SPEC.volume_step, min_lot_risk_cap_pct=min_lot_cap,
    )


# --------------------------------------------------------------------------- #
# Main hybrid simulation
# --------------------------------------------------------------------------- #
def simulate(
    d: M30Data, signal_by_time: dict,
    *, mode: str, N: int, m30_pivot: int, cfg: dict, commission: float,
    starting_equity: float, watchman_on: bool, wm_cfg: Optional[WatchmanConfig],
    rv_recheck: Optional[RiskVoiceConfig], min_lot_cap: Optional[float],
    win_lo: pd.Timestamp, win_hi: pd.Timestamp,
):
    cm = CostModelConfig(commission_per_lot=commission, slippage_points=None)
    o = cfg["order"]
    risk_pct = cfg["cfo"]["risk_per_trade_pct"]
    op, hi, lo, cl, sp = d.op, d.hi, d.lo, d.cl, d.sp
    minute, atr_a, avgsp, avgatr = d.minute, d.atr, d.avg_sp_960, d.avg_atr_960
    times, pydt = d.times, d.pydt
    one_hour = pd.Timedelta(hours=1)

    tarr = d.df["time"].to_numpy()
    in_win = (tarr >= np.datetime64(win_lo)) & (tarr < np.datetime64(win_hi))
    idx = np.nonzero(in_win)[0]
    counts = {"armed": 0, "expired": 0, "rv_veto": 0, "size_skip": 0, "filled": 0}
    if len(idx) == 0:
        return [], counts
    m_start, m_end = int(idx[0]), int(idx[-1])

    clock = SimulatedClock(pydt[m_start])
    equity = starting_equity
    trades: list[ClosedTrade] = []
    state = "FLAT"
    armed: Optional[_Armed] = None
    pos: Optional[_Open] = None
    pending: Optional[dict] = None

    def _do_fill(m, plan, swing_idx) -> None:
        nonlocal pos, state, equity
        if rv_recheck is not None:
            plan_ = plan
            dec = check_risk_voice(
                symbol="XAUUSD", order_plan=plan_, current_spread_points=float(sp[m]),
                avg_spread_points_20d=float(avgsp[m]), current_atr=float(atr_a[m]),
                avg_atr_20d=float(avgatr[m]), news_provider=_NEWS, clock=clock, config=rv_recheck,
            )
            if dec.vetoed:
                counts["rv_veto"] += 1
                state = "FLAT"
                return
        ss = spread_slippage_price(float(sp[m]), _SPEC, cm)
        entry_price = float(op[m]) + ss if plan.direction == "BUY" else float(op[m]) - ss
        lot = _lot(equity, risk_pct, plan, min_lot_cap)
        if lot is None:
            counts["size_skip"] += 1
            state = "FLAT"
            return
        meta = None
        if watchman_on and swing_idx is not None:
            meta = PositionMetadata(
                ticket=0, symbol="XAUUSD", direction=plan.direction, entry_price=entry_price,
                initial_stop_distance=plan.stop_distance, entry_swing_index=swing_idx,
                opened_at=pydt[m],
            )
        pos = _Open(plan=plan, lot=lot, entry_time=times[m], entry_price=entry_price, ss_delta=ss,
                    current_sl=plan.stop_loss, metadata=meta)
        state = "OPEN"
        counts["filled"] += 1

    def _manage(m) -> None:
        nonlocal pos, state, equity
        bar = {"open": op[m], "high": hi[m], "low": lo[m]}
        ex = check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar)
        if ex is not None:
            px, reason = ex
            trades.append(_close(pos.plan.direction, pos.entry_time, pos.entry_price, pos.ss_delta,
                                 times[m], px, reason, pos.lot, pos.plan.stop_distance, cm))
            equity += trades[-1].net_pnl
            pos, state = None, "FLAT"
            return
        if watchman_on and pos.metadata is not None:
            dec = evaluate_watchman(
                position_metadata=pos.metadata, current_sl=pos.current_sl,
                current_price=float(cl[m]), current_atr=float(atr_a[m]),
                df=d.df, as_of_index=m, now=pydt[m], config=wm_cfg,
            )
            if dec.action == "CLOSE":
                reason = ("structure_invalidation"
                          if dec.reason.startswith("structure invalidation") else "time_stop")
                trades.append(_close(pos.plan.direction, pos.entry_time, pos.entry_price, pos.ss_delta,
                                     times[m], float(cl[m]), reason, pos.lot, pos.plan.stop_distance, cm))
                equity += trades[-1].net_pnl
                pos, state = None, "FLAT"
            elif dec.action == "MODIFY_SL":
                pos.current_sl = dec.new_stop_loss

    for m in range(m_start, m_end + 1):
        clock.set(pydt[m])

        # (1) resolve a pending fill decided on the previous bar
        if pending is not None and pending["m"] == m:
            pf = pending
            pending = None
            _do_fill(m, pf["plan"], pf["swing_idx"])

        # (2) manage an open position (fill bar exit-checked same bar, engine order)
        if state == "OPEN":
            _manage(m)

        # (3) scan an armed window for pullback-then-resume
        if state == "ARMED":
            dirn = armed.direction
            pb = armed.pb_index
            decided = (
                pb is not None and m > pb
                and ((dirn == "BUY" and cl[m] > hi[pb]) or (dirn == "SELL" and cl[m] < lo[pb]))
            )
            if decided:
                swing_price = float(lo[pb]) if dirn == "BUY" else float(hi[pb])
                plan = build_order_plan(
                    direction=dirn, entry_price=float(cl[m]), swing_price=swing_price,
                    atr=float(atr_a[m]), sl_buffer_atr=o["sl_buffer_atr"],
                    sl_min_atr=o["sl_min_atr"], sl_max_atr=o["sl_max_atr"],
                    tp_r_multiple=o["tp_r_multiple"],
                )
                if plan is None or m + 1 > m_end:
                    counts["expired"] += 1
                    state, armed = "FLAT", None
                else:
                    pending = {"m": m + 1, "plan": plan, "swing_idx": pb}
                    state, armed = "PENDING", None
            else:
                if m - m30_pivot >= 0:
                    if dirn == "BUY":
                        is_piv = lo[m] < lo[m - m30_pivot:m].min()
                        deeper = pb is None or lo[m] < lo[pb]
                    else:
                        is_piv = hi[m] > hi[m - m30_pivot:m].max()
                        deeper = pb is None or hi[m] > hi[pb]
                    if is_piv and deeper:
                        armed.pb_index = m
                if m >= armed.window_end:
                    counts["expired"] += 1
                    state, armed = "FLAT", None

        # (4) if flat and this is an H1-close boundary with a signal, arm/enter
        if state == "FLAT" and pending is None and minute[m] == 0:
            plan_h1 = signal_by_time.get(times[m] - one_hour)
            if plan_h1 is not None:
                counts["armed"] += 1
                if mode == "degenerate":
                    _do_fill(m, plan_h1, None)
                    if state == "OPEN":
                        _manage(m)
                else:
                    dirn = plan_h1.direction
                    we = m + N - 1
                    pbx = None
                    if m - m30_pivot >= 0:
                        if (dirn == "BUY" and lo[m] < lo[m - m30_pivot:m].min()) or \
                           (dirn == "SELL" and hi[m] > hi[m - m30_pivot:m].max()):
                            pbx = m
                    armed = _Armed(direction=dirn, window_end=we, pb_index=pbx)
                    state = "ARMED"
                    if m >= we:
                        counts["expired"] += 1
                        state, armed = "FLAT", None

    if state == "ARMED":
        counts["expired"] += 1
    if state == "OPEN" and pos is not None:
        trades.append(_close(pos.plan.direction, pos.entry_time, pos.entry_price, pos.ss_delta,
                             times[m_end], float(cl[m_end]), "end_of_data",
                             pos.lot, pos.plan.stop_distance, cm))
    return trades, counts


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def result_row(trades, counts, rep_equity, extra) -> dict:
    rep = generate_report(trades, rep_equity)
    pf, pf5 = rep.profit_factor, rep.profit_factor_excluding_top_5
    row = {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4) if rep.win_rate is not None else None,
        "PF": (round(pf, 4) if isinstance(pf, float) and pf != float("inf") else pf),
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "DD": round(rep.max_drawdown_pct, 3) if rep.max_drawdown_pct is not None else None,
        "PF_ex5": (round(pf5, 4) if isinstance(pf5, float) and pf5 != float("inf") else pf5),
        **{f"n_{k}": v for k, v in counts.items()},
    }
    row.update(extra)
    return row


def build_rv(cfg, session_start, session_end, on) -> Optional[RiskVoiceConfig]:
    if not on:
        return None
    r = cfg["risk_voice"]
    return RiskVoiceConfig(
        max_spread_multiple=r["max_spread_multiple"], max_spread_points_xauusd=r["max_spread_points_xauusd"],
        news_blackout_before_min=r["news_blackout_before_min"], news_blackout_after_min=r["news_blackout_after_min"],
        max_stop_atr_multiple=r["max_stop_atr_multiple"], session_start_hour=session_start,
        session_end_hour=session_end, friday_close_hour=r["friday_close_hour"],
        max_atr_panic_multiple=r["max_atr_panic_multiple"],
    )


def build_wm(cfg, time_stop, dead_band, on) -> Optional[WatchmanConfig]:
    if not on:
        return None
    w = cfg["watchman"]
    return WatchmanConfig(
        breakeven_at_r=w["breakeven_at_r"], trail_start_r=w["trail_start_r"],
        trail_distance_atr=w["trail_distance_atr"], time_stop_hours=time_stop,
        dead_trade_r_band=dead_band, breakeven_enabled=False, trail_enabled=False,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["degenerate", "pullback"], required=True)
    p.add_argument("--N", type=int, default=4)
    p.add_argument("--pivot", type=int, default=3)
    p.add_argument("--windows", default="y1,y2,y3,val")
    p.add_argument("--commission", type=float, required=True)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--watchman", choices=["on", "off"], default="on")
    p.add_argument("--time-stop", type=float, default=48.0)
    p.add_argument("--dead-band", type=float, default=0.3)
    p.add_argument("--risk-voice", choices=["on", "off"], default="on",
                   help="H1 signal-time Risk Voice gate (inherited from production)")
    p.add_argument("--m30-recheck", choices=["on", "off"], default="on",
                   help="M30 entry-bar Risk-Voice RE-CHECK (§2). OFF for the degenerate "
                        "fidelity twin, since the H1-as-is baseline has no M30 re-check and "
                        "an H1-scale stop checked vs M30-ATR would spuriously veto ~84%.")
    p.add_argument("--session-start", type=int, default=0)
    p.add_argument("--session-end", type=int, default=24)
    p.add_argument("--min-lot-cap", type=float, default=None)
    p.add_argument("--h1-pivot", type=int, default=None)
    p.add_argument("--tp", type=float, default=None)
    p.add_argument("--label", default="run")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    h1_pivot = args.h1_pivot if args.h1_pivot is not None else cfg["global"]["swing_pivot_bars"]
    tp = args.tp if args.tp is not None else cfg["order"]["tp_r_multiple"]
    h1_full = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"]).reset_index(drop=True)
    m30_full = pd.read_csv(HISTORICAL_DIR / "XAUUSD_M30.csv", parse_dates=["time"]).reset_index(drop=True)

    rv = build_rv(cfg, args.session_start, args.session_end, args.risk_voice == "on")
    rv_recheck = rv if args.m30_recheck == "on" else None
    wm = build_wm(cfg, args.time_stop, args.dead_band, args.watchman == "on")
    d = build_m30_data(m30_full)

    for wn in [w.strip() for w in args.windows.split(",") if w.strip()]:
        start, end = WINDOWS[wn]
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        h1_slice = h1_full[(h1_full["time"] >= lo) & (h1_full["time"] < hi)].reset_index(drop=True)
        sig = compute_h1_signals(h1_slice, cfg, rv, tp=tp, pivot_bars=h1_pivot)
        trades, counts = simulate(
            d, sig, mode=args.mode, N=args.N, m30_pivot=args.pivot, cfg=cfg,
            commission=args.commission, starting_equity=args.equity,
            watchman_on=(args.watchman == "on"), wm_cfg=wm, rv_recheck=rv_recheck,
            min_lot_cap=args.min_lot_cap, win_lo=lo, win_hi=hi,
        )
        row = result_row(trades, counts, args.equity, {
            "label": args.label, "mode": args.mode, "N": args.N, "pivot": args.pivot,
            "window": wn, "watchman": args.watchman, "rv": args.risk_voice,
            "time_stop": args.time_stop, "dead_band": args.dead_band, "equity": args.equity,
            "min_lot_cap": args.min_lot_cap, "comm": args.commission, "h1_pivot": h1_pivot, "tp": tp,
        })
        print("RESULT " + json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
