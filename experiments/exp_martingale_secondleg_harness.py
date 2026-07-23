#!/usr/bin/env python3
"""EXPLORATORY (NOT an EXP) — "add-to-a-loser" second-position harness.

User idea (verbatim TH): "หลัง order 1 ไม้ไปแล้ว และเกิดกรณี Buy/Sell แล้ว
Profit ติดลบ ...% อนุญาติให้บอทเปิดไม้เพิ่ม 1 ไม้ได้ดีมั้ย เผื่อเอากำไรมาถัว
กับที่เสียไป" -- once leg 1 is floating at a loss of X%, allow ONE extra
position to average out / offset the loss. This is a martingale/grid pattern.

The phrasing is ambiguous on direction, so this harness models BOTH readings
as separate variants, ON TRAIN ONLY (2021-07-22 -> 2024-07-21). Validation and
Test are DELIBERATELY NOT TOUCHED -- this is a risk-first diagnostic; if the
risk profile alone disqualifies the idea, no split-based tuning is warranted.

  (A) SAME-DIRECTION martingale: while leg 1 is open and floating loss reaches
      -T (as a fraction of leg 1's own initial risk R), open leg 2 in the SAME
      direction (no fresh Council signal needed). Leg 2 uses leg 1's stop
      DISTANCE as its risk unit (own entry, own SL = entry -/+ stop_distance,
      own 2R TP), and exits independently. Sizing mult tested: 1.0x (pure
      averaging) and 2.0x (classic double-down).
  (B) INDEPENDENT-SECOND-SLOT unlock: while leg 1 is floating at -T, unlock a
      second slot and let a genuinely fresh Council/Risk-Voice-approved signal
      (any direction) take it, sized normally (1.0% risk). NOT averaging the
      same trade -- just "don't sit idle while the loser resolves".

Baseline B0 = single-position, two-slot logic disabled: MUST reproduce the
stock `run_backtest` on the Train window (fidelity asserted at startup).

Everything reuses production code (engine signal fn / check_exit / cost model /
Watchman / Shield / sizing / order construction) under the CURRENTLY ADOPTED
config (pivot 3, tp 2.0, all-24h [0,24), be/trail OFF, structure+time-stop ON,
Shield cooldown ON, min_lot_risk_cap 1.5, risk 1.0%). Starting equity $3,000.

Commission: $0 (the user's REAL IC Markets *Standard* account, per project
memory) is primary; spread is baked per-bar from the data's own spread column
(min-1-spread). A $7/lot Raw-Spread pass is also emitted for cross-reference
with older logged experiments. Circuit breakers (2% daily / 8% halt) are NOT
simulated (same as the stock engine) -- instead we REPORT whether the honest
mark-to-market equity path would have breached them or gone bust at $3,000.

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
    _watchman_current_atr,
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
TRAIN_END = "2024-07-21"  # exclusive upper edge for Train


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


def _make_leg2_plan(leg1: _OpenPosition, entry: float) -> OrderPlan:
    """Variant-A synthetic add: SAME direction as leg 1, using leg 1's stop
    DISTANCE as the risk unit (own entry, own SL, own 2R TP). Independent exit."""
    d = leg1.plan.stop_distance
    if leg1.plan.direction == "BUY":
        sl = entry - d
        tp = entry + 2.0 * d
    else:
        sl = entry + d
        tp = entry - 2.0 * d
    return OrderPlan(direction=leg1.plan.direction, entry=entry, stop_loss=sl,
                     take_profit=tp, stop_distance=d)


@dataclass
class _Pending:
    plan: OrderPlan
    lot: float
    signal_index: int
    swing_index: int | None
    is_leg2: bool


def precompute_signals(df, cfg):
    """The council+RiskVoice OrderPlan at each bar is IDENTICAL across all
    variants (same _council_signal_fn, same params) -- the O(n^2) part. Compute
    it ONCE (plan + its swing_index) and reuse across every variant pass, so
    each pass drops to O(n). Shield is NOT applied here (it is stateful per
    pass). Also precompute the full-series ATR(14) for Watchman (be/trail are
    OFF, so current_atr does not affect the structure/time-stop exits)."""
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


def run_variant(df, cfg, *, variant, trigger_r, mult, commission, start_equity,
                cache=None, atr14=None):
    """variant in {"baseline","A","B"}. trigger_r/mult ignored for baseline.
    `cache`/`atr14` from precompute_signals() make this O(n); if None they are
    computed inline (slow, used only for the fidelity smoke check)."""
    if cache is None:
        cache, atr14 = precompute_signals(df, cfg)
    rvc, wmc, shc = _cfgs(cfg)
    pivot = cfg["global"]["swing_pivot_bars"]
    risk_pct = cfg["cfo"]["risk_per_trade_pct"]
    cap = cfg["cfo"]["min_lot_risk_cap_pct"]
    tp_r = cfg["order"]["tp_r_multiple"]
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

    equity = start_equity                 # realized only (drives sizing/compounding)
    slotA: _OpenPosition | None = None    # primary (drives signals when fully flat)
    slotB: _OpenPosition | None = None    # the extra slot (A: martingale add, B: fresh)
    pendA: _Pending | None = None
    pendB: _Pending | None = None
    episode_used = False                  # this leg1-episode has spawned/unlocked
    unlocked = False                      # variant B: slot open for a fresh signal

    trades: list[ClosedTrade] = []
    mtm_curve: list[tuple[pd.Timestamp, float]] = []
    episodes: list[dict] = []             # per doubled/unlocked episode risk record
    cur_ep: dict | None = None

    def signal_at(i, for_leg2):
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
        return _Pending(plan=plan, lot=lot, signal_index=i, swing_index=swing_index, is_leg2=for_leg2)

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

        # 1) fills
        if pendA is not None:
            slotA = fill(pendA, bar); pendA = None
        if pendB is not None:
            slotB = fill(pendB, bar); pendB = None

        # 2) exits (A first, then B)
        if slotA is not None:
            t = try_exit(slotA, i, bar)
            if t is not None:
                trades.append(t); equity += t.net_pnl; slotA = None
        if slotB is not None:
            t = try_exit(slotB, i, bar)
            if t is not None:
                trades.append(t); equity += t.net_pnl; slotB = None

        # episode bookkeeping: reset when FULLY flat
        if slotA is None and slotB is None and pendA is None and pendB is None:
            if cur_ep is not None:
                episodes.append(cur_ep); cur_ep = None
            episode_used = False
            unlocked = False

        close = float(bar["close"])

        # 3) mark-to-market equity (realized + floating of both open legs)
        floatA = _floating(slotA, close) if slotA is not None else 0.0
        floatB = _floating(slotB, close) if slotB is not None else 0.0
        mtm = equity + floatA + floatB
        mtm_curve.append((pd.Timestamp(bar["time"]), mtm))
        if cur_ep is not None:
            cur_ep["min_mtm_float"] = min(cur_ep["min_mtm_float"], floatA + floatB)
            cur_ep["min_mtm_equity"] = min(cur_ep["min_mtm_equity"], mtm)

        # 4) second-slot trigger (only when exactly leg A open, extra slot free, not used yet)
        if variant in ("A", "B") and slotA is not None and slotB is None and pendB is None and not episode_used:
            if floatA <= -trigger_r * _risk_usd(slotA):
                if variant == "A":
                    entry_ref = close  # add decision now; fills next-bar open
                    plan2 = _make_leg2_plan(slotA, entry_ref)
                    raw = mult * slotA.lot_size
                    # round down to step, floor at volume_min
                    steps = int((raw + 1e-9) / _SPEC.volume_step)
                    lot2 = max(_SPEC.volume_min, round(steps * _SPEC.volume_step, 2))
                    pendB = _Pending(plan=plan2, lot=lot2, signal_index=i, swing_index=None, is_leg2=True)
                    episode_used = True
                    cur_ep = {"kind": "A", "trigger_time": pd.Timestamp(bar["time"]),
                              "leg1_risk": _risk_usd(slotA), "leg1_lot": slotA.lot_size,
                              "leg2_lot": lot2, "equity_at_trigger": equity,
                              "price_at_trigger": close, "direction": slotA.plan.direction,
                              "min_mtm_float": floatA, "min_mtm_equity": mtm}
                else:  # variant B: just unlock; a fresh signal fills the slot
                    unlocked = True
                    episode_used = True
                    cur_ep = {"kind": "B", "trigger_time": pd.Timestamp(bar["time"]),
                              "equity_at_trigger": equity, "min_mtm_float": floatA,
                              "min_mtm_equity": mtm, "leg2_taken": False}

        # 5) signals
        # primary leg A only when FULLY flat (bounds concurrency to 2)
        if slotA is None and pendA is None and slotB is None and pendB is None:
            p = signal_at(i, for_leg2=False)
            if p is not None and i + 1 < n:
                pendA = p
        # variant B fresh second signal, once unlocked
        elif variant == "B" and unlocked and slotA is not None and slotB is None and pendB is None:
            p = signal_at(i, for_leg2=True)
            if p is not None and i + 1 < n:
                pendB = p
                if cur_ep is not None:
                    cur_ep["leg2_taken"] = True

    # close any open legs at end of data
    last = df.iloc[-1]
    for pos in (slotA, slotB):
        if pos is not None:
            trades.append(_close_trade("XAUUSD", pos, pd.Timestamp(last["time"]),
                                       float(last["close"]), "end_of_data", _POINT_VALUE, cm))
    if cur_ep is not None:
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
    worst_pct = 0.0
    worst_usd = 0.0
    trough = curve[0][1] if curve else 0.0
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
    doubled = [e for e in episodes if e.get("kind") == "A"] + \
              [e for e in episodes if e.get("kind") == "B" and e.get("leg2_taken")]
    ep_floats = sorted(episodes, key=lambda e: e.get("min_mtm_float", 0.0))[:10]
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
        "n_episodes": len(episodes),
        "n_second_leg_taken": len(doubled),
        "per_year": _per_year_pf(trades),
        "worst10_episode_float": [
            {"t": str(e["trigger_time"]), "min_float": round(e.get("min_mtm_float", 0.0), 1),
             "min_equity": round(e.get("min_mtm_equity", 0.0), 1)} for e in ep_floats
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
        df_fid, cfg, variant="baseline", trigger_r=0.0, mult=1.0,
        commission=args.commission, start_equity=args.equity, cache=fid_cache, atr14=fid_atr)
    fid = {"slice_bars": len(df_fid),
           "stock_trades": len(stock_trades), "stock_pf": round(_pf(stock_trades), 4),
           "stock_net": round(sum(t.net_pnl for t in stock_trades), 2),
           "harness_trades": len(h_trades), "harness_pf": round(_pf(h_trades), 4),
           "harness_net": round(sum(t.net_pnl for t in h_trades), 2)}
    print("FIDELITY " + json.dumps(fid), flush=True)
    assert fid["stock_trades"] == fid["harness_trades"], "FIDELITY FAIL: trade count"
    assert abs(fid["stock_net"] - fid["harness_net"]) < 1.0, "FIDELITY FAIL: net"

    # ---- precompute the full-Train signals ONCE, reuse across all variants ----
    print("# precomputing full-Train signals...", flush=True)
    cache, atr14 = precompute_signals(df, cfg)
    print("# precompute done", flush=True)

    conditions = [
        ("B0_baseline", "baseline", 0.0, 1.0),
        ("A1_same_0.5R_1x", "A", 0.5, 1.0),
        ("A2_same_0.5R_2x", "A", 0.5, 2.0),
        ("A3_same_0.75R_2x", "A", 0.75, 2.0),
        ("B1_freshslot_0.5R", "B", 0.5, 1.0),
    ]
    results = []
    for label, variant, tr, mult in conditions:
        trades, curve, episodes, final_eq = run_variant(
            df, cfg, variant=variant, trigger_r=tr, mult=mult,
            commission=args.commission, start_equity=args.equity, cache=cache, atr14=atr14)
        s = summarize(label, trades, curve, episodes, final_eq, args.equity)
        results.append(s)
        print("RESULT " + json.dumps(s), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
