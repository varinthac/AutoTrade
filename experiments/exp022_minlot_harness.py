#!/usr/bin/env python3
"""EXP-022 harness — min-lot floor / `cfo.min_lot_risk_cap_pct` at ~$3,000 equity.

WHY (2026-07-31): on the live $2,940 demo, 11 of 19 evaluated H1 bars produced
BUY signals that passed EVERY gate and were then refused at CFO sizing with
"computed lot size below broker minimum 0.01". Root arithmetic: XAUUSD 0.01 lot
=> $1 per $1 price move, so trading the broker minimum at all costs
`stop_distance` dollars of risk; `cfo.min_lot_risk_cap_pct: 1.5` caps that at
1.5% * equity ~= $44. Gold's current ATR-derived stops exceed that.

This harness measures (read-only; NO file under src/ or config/ is modified):
  --mode regime  : structural stop-distance vs affordability by year (no backtest)
  --mode sweep   : sweep min_lot_risk_cap_pct at small-account equity, per-year
  --mode risk    : sweep risk_per_trade_pct (the alternative knob), per-year
  --mode slmax   : sweep order.sl_max_atr (the "tighter stop" alternative)
  --mode fidelity: prove the fast-path shim below is exactly equivalent

METHOD REUSE: the "rescued subset in isolation" attribution is the same method
as the 2026-07-22 NOTE "small-account sizing REFRESH + min-lot-fallback
measurement" (ordered sizing-call log zipped 1:1 against the trade list with a
lot-value equality assert), so results are comparable. Two deliberate upgrades:
  * the engine now supports `min_lot_risk_cap_pct` NATIVELY, so the wrapper here
    only OBSERVES (it calls the real sizer twice -- once as configured, once
    forced cap=None -- to label a rescue); it never changes behavior.
  * cost model is now COMPLETE per scripts/run_backtest.py's own definition
    (slippage = min 1 spread AND swap modeled). Commission defaults to $0
    (IC Markets Standard, the real account) rather than the older $7 convention.

FAST-PATH SHIM: the real pipeline recomputes every indicator and re-detects every
swing over the whole bar prefix on every bar -- O(n^2) -- which makes one
29,543-bar run take ~1 hour. `install_fast_path()` memoises the FULL-series
result of each pure indicator / of swing detection and serves prefixes from it.
This is EXACTLY equivalent, not an approximation: `ema`/`rsi`/`atr` are causal
recursive `ewm(adjust=False)` from bar 0 (prefix value == full-series value at
that index), and a fractal pivot at i depends only on bars i-p..i+p while
`_confirmed_swing_indices` already restricts itself to i <= as_of_index - p.
`--mode fidelity` proves it by running a window with and without the shim and
asserting byte-identical trades. No src/ file is edited; the shim is a
monkeypatch installed by THIS file only.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys


import pandas as pd

import autotrade.backtest.engine as engine_mod
import autotrade.council.decision_matrix as dmx_mod
import autotrade.council.scoring as scoring_mod
import autotrade.features.indicators as ind_mod
import autotrade.features.swing as swing_mod
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.shield.checkpoint import ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig

# IC Markets XAUUSD spec (same values run_backtest.py resolves from MT5).
_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)
POINT_VALUE = _SPEC.tick_value / _SPEC.tick_size  # 100.0 -> 0.01 lot = $1 per $1 move

# EXP-018's rates (IC Markets demo measurement; the only ones on record here).
SWAP_LONG = -53.2
SWAP_SHORT = 36.8

# Chronological windows, IDENTICAL to EXP-001..021 (60/20/20 by year).
YEARS = [
    ("y1_2021-22", "2021-07-22", "2022-07-21"),
    ("y2_2022-23", "2022-07-22", "2023-07-21"),
    ("y3_2023-24", "2023-07-22", "2024-07-21"),
    ("y4_VAL_2024-25", "2024-07-22", "2025-07-21"),
    ("y5_TEST_2025-26", "2025-07-22", "2026-07-21"),
]


# ---------------------------------------------------------------- fast path
_REAL = {
    "ema": ind_mod.ema, "rsi": ind_mod.rsi, "atr": ind_mod.atr,
    "macd": ind_mod.macd_histogram, "conf": swing_mod._confirmed_swing_indices,
    "sizer": engine_mod.compute_lot_size,
}


class _FastPath:
    def __init__(self, df: pd.DataFrame, pivot_bars: int):
        self.n = len(df)
        self.close = df["close"]
        self.high = df["high"]
        self.low = df["low"]
        self.pivot_bars = pivot_bars
        self._ind: dict = {}
        full_swings = swing_mod.detect_swings(df, pivot_bars=pivot_bars)
        self._swings = {
            "swing_high": sorted(int(i) for i in full_swings.index[full_swings["swing_high"]]),
            "swing_low": sorted(int(i) for i in full_swings.index[full_swings["swing_low"]]),
        }

    def _is_prefix(self, s: pd.Series, ref: pd.Series) -> bool:
        length = len(s)
        return 0 < length <= self.n and float(s.iloc[-1]) == float(ref.iloc[length - 1])

    def _serve(self, key, builder, s: pd.Series, ref: pd.Series, fallback):
        if not self._is_prefix(s, ref):
            return fallback()
        if key not in self._ind:
            self._ind[key] = builder()
        return self._ind[key].iloc[: len(s)]

    def ema(self, closes, period):
        return self._serve(("ema", period), lambda: _REAL["ema"](self.close, period),
                           closes, self.close, lambda: _REAL["ema"](closes, period))

    def rsi(self, closes, period=14):
        return self._serve(("rsi", period), lambda: _REAL["rsi"](self.close, period),
                           closes, self.close, lambda: _REAL["rsi"](closes, period))

    def macd(self, closes, fast=12, slow=26, signal=9):
        return self._serve(("macd", fast, slow, signal),
                           lambda: _REAL["macd"](self.close, fast, slow, signal),
                           closes, self.close, lambda: _REAL["macd"](closes, fast, slow, signal))

    def atr(self, high, low, close, period=14):
        return self._serve(("atr", period), lambda: _REAL["atr"](self.high, self.low, self.close, period),
                           close, self.close, lambda: _REAL["atr"](high, low, close, period))

    def confirmed_swings(self, df, as_of_index, pivot_bars, column):
        if len(df) != self.n or pivot_bars != self.pivot_bars:
            return _REAL["conf"](df, as_of_index, pivot_bars, column)
        if as_of_index < 0 or as_of_index >= self.n:
            raise ValueError(f"as_of_index {as_of_index} is out of bounds for df of length {self.n}")
        last_allowed = as_of_index - pivot_bars
        if last_allowed < pivot_bars:
            return []
        arr = self._swings[column]
        return arr[: bisect.bisect_right(arr, last_allowed)]


def install_fast_path(df: pd.DataFrame, pivot_bars: int) -> _FastPath:
    fp = _FastPath(df, pivot_bars)
    for mod in (scoring_mod,):
        mod.ema, mod.rsi, mod.atr, mod.macd_histogram = fp.ema, fp.rsi, fp.atr, fp.macd
    dmx_mod.atr = fp.atr
    engine_mod.atr = fp.atr
    swing_mod._confirmed_swing_indices = fp.confirmed_swings
    return fp


def uninstall_fast_path() -> None:
    scoring_mod.ema, scoring_mod.rsi = _REAL["ema"], _REAL["rsi"]
    scoring_mod.atr, scoring_mod.macd_histogram = _REAL["atr"], _REAL["macd"]
    dmx_mod.atr = _REAL["atr"]
    engine_mod.atr = _REAL["atr"]
    swing_mod._confirmed_swing_indices = _REAL["conf"]


# ---------------------------------------------------------------- config
def build_cfgs(cfg, sl_max_atr=None):
    rv = cfg["risk_voice"]
    wm = cfg["watchman"]
    sh = cfg["shield"]
    order = cfg["order"]
    rv_cfg = RiskVoiceConfig(
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
    wm_cfg = WatchmanConfig(
        breakeven_at_r=wm["breakeven_at_r"], trail_start_r=wm["trail_start_r"],
        trail_distance_atr=wm["trail_distance_atr"], time_stop_hours=wm["time_stop_hours"],
        dead_trade_r_band=wm["dead_trade_r_band"],
        breakeven_enabled=wm["breakeven_enabled"], trail_enabled=wm["trail_enabled"],
    )
    sh_cfg = ShieldConfig(
        min_rr=sh["min_rr"], max_correlation=sh["max_correlation"],
        max_positions_per_symbol=sh["max_positions_per_symbol"],
        max_positions_total=sh["max_positions_total"],
        total_risk_ceiling_pct=sh["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=sh["duplicate_signal_cooldown_hours"],
    )
    return rv_cfg, wm_cfg, sh_cfg, {
        "sl_buffer_atr": order["sl_buffer_atr"],
        "sl_min_atr": order["sl_min_atr"],
        "sl_max_atr": sl_max_atr if sl_max_atr is not None else order["sl_max_atr"],
        "tp_r_multiple": order["tp_r_multiple"],
    }


def _make_observer(cap_pct):
    """Observe-only wrapper: calls the REAL sizer as configured, and a second
    time forced to cap=None purely to LABEL whether this call was a min-lot
    rescue. Behavior is unchanged (the returned lot is the real one)."""
    real = _REAL["sizer"]
    state = {"calls": [], "raw_none": 0, "rescued": 0}

    def wrapper(**kwargs):
        lot = real(**kwargs)
        nocap = kwargs | {"min_lot_risk_cap_pct": None}
        lot_nocap = real(**nocap)
        rescued = lot is not None and lot_nocap is None
        if lot_nocap is None:
            state["raw_none"] += 1
        if rescued:
            state["rescued"] += 1
        state["calls"].append({
            "lot": lot, "rescued": rescued,
            "stop_dist": abs(kwargs["entry"] - kwargs["stop_loss"]),
            "equity": kwargs["equity"],
        })
        return lot

    assert cap_pct is None or cap_pct > 0
    return wrapper, state


# ---------------------------------------------------------------- metrics
def _pf(trades):
    gp = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gl = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if not trades:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)


def _dd_usd(trades, start):
    eq = peak = start
    worst = 0.0
    for t in trades:
        eq += t.net_pnl
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def _worst_streak(trades):
    worst = 0.0
    run = 0.0
    worst_n = n = 0
    for t in trades:
        if t.net_pnl < 0:
            run += t.net_pnl
            n += 1
            if run < worst:
                worst, worst_n = run, n
        else:
            run, n = 0.0, 0
    return worst, worst_n


def _attribute(state, trades):
    sized = [c for c in state["calls"] if c["lot"] is not None]
    if len(sized) == len(trades):
        pairs = list(zip(sized, trades))
    elif len(sized) == len(trades) + 1:
        pairs = list(zip(sized[:-1], trades))
    else:
        raise AssertionError(f"sizing-call/trade mismatch: {len(sized)} vs {len(trades)}")
    for call, trade in pairs:
        assert abs(call["lot"] - trade.lot_size) < 1e-9, "attribution ordering violated"
    return pairs


def _pct_risk_stats(pairs):
    """Risk expressed against the equity AT THE TIME of the trade -- the only
    honest denominator once equity compounds inside a window. `planned_risk_pct`
    is the trade's own full-stop risk (stop_distance * point_value * lot /
    equity); `loss_pct` is realized net loss over the same equity."""
    if not pairs:
        return {}
    planned = [100.0 * c["stop_dist"] * POINT_VALUE * c["lot"] / c["equity"] for c, _ in pairs]
    loss = [100.0 * t.net_pnl / c["equity"] for c, t in pairs]
    # worst consecutive-loss run measured in % of then-equity
    worst = run = 0.0
    worst_n = n = 0
    for lp in loss:
        if lp < 0:
            run += lp
            n += 1
            if run < worst:
                worst, worst_n = run, n
        else:
            run, n = 0.0, 0
    s = pd.Series(planned)
    return {
        "max_planned_risk_pct": round(max(planned), 2),
        "p90_planned_risk_pct": round(float(s.quantile(0.90)), 2),
        "median_planned_risk_pct": round(float(s.median()), 2),
        "worst_loss_pct_of_equity": round(min(loss), 2),
        "worst_streak_pct_of_equity": round(worst, 2),
        "worst_streak_len_pct": worst_n,
        "worst_3_consecutive_pct": round(min(
            [sum(loss[i:i + 3]) for i in range(max(1, len(loss) - 2))]
        ), 2),
    }


def cell_metrics(trades, start_equity):
    rep = generate_report(trades, start_equity)
    streak, streak_n = _worst_streak(trades)
    return {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4) if rep.win_rate is not None else None,
        "PF": round(rep.profit_factor, 4) if rep.profit_factor not in (None, float("inf")) else rep.profit_factor,
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 not in (None, float("inf")) else rep.profit_factor_excluding_top_5,
        "net": round(rep.total_net_pnl, 2),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "maxDD_pct": round(rep.max_drawdown_pct, 2) if rep.max_drawdown_pct is not None else None,
        "maxDD_usd": round(_dd_usd(trades, start_equity), 2),
        "max_single_loss": round(min((t.net_pnl for t in trades), default=0.0), 2),
        "worst_streak_usd": round(streak, 2),
        "worst_streak_len": streak_n,
    }


def run_cell(df, *, cap_pct, risk_pct, cfg, commission, start_equity,
             swap=True, sl_max_atr=None, label=""):
    rv_cfg, wm_cfg, sh_cfg, order = build_cfgs(cfg, sl_max_atr=sl_max_atr)
    wrapper, state = _make_observer(cap_pct)
    engine_mod.compute_lot_size = wrapper
    try:
        bt = BacktestConfig(
            starting_equity=start_equity,
            risk_per_trade_pct=risk_pct,
            cost_model=CostModelConfig(
                commission_per_lot=commission, slippage_points=None,
                swap_model=SwapModelConfig(long_per_lot_per_night=SWAP_LONG,
                                           short_per_lot_per_night=SWAP_SHORT) if swap else None,
            ),
            risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
            pivot_bars=cfg["global"]["swing_pivot_bars"],
            min_lot_risk_cap_pct=cap_pct,
            **order,
        )
        trades = run_backtest(df, "XAUUSD", _SPEC, bt)
    finally:
        engine_mod.compute_lot_size = _REAL["sizer"]

    calls = len(state["calls"])
    skips = state["raw_none"] - state["rescued"]
    out = {
        "label": label, "cap_pct": cap_pct, "risk_pct": risk_pct,
        "sl_max_atr": sl_max_atr if sl_max_atr is not None else cfg["order"]["sl_max_atr"],
        "start_equity": start_equity, "commission": commission, "swap": swap,
        "signals_to_sizing": calls,
        "sub_min_raw": state["raw_none"],
        "rescued": state["rescued"],
        "skips": skips,
        "skip_pct": round(100.0 * skips / calls, 2) if calls else 0.0,
        "sub_min_pct": round(100.0 * state["raw_none"] / calls, 2) if calls else 0.0,
        **cell_metrics(trades, start_equity),
    }
    pairs = _attribute(state, trades)
    resc_pairs = [(c, t) for c, t in pairs if c["rescued"]]
    resc = [t for _, t in resc_pairs]
    out["risk_vs_equity"] = _pct_risk_stats(pairs)
    out["rescued_trades"] = len(resc)
    if resc:
        out["rescued_risk_vs_equity"] = _pct_risk_stats(resc_pairs)
        out["rescued_subset"] = {
            "trades": len(resc),
            "pct_of_executed": round(100.0 * len(resc) / len(trades), 2),
            "net": round(sum(t.net_pnl for t in resc), 2),
            "PF": _pf(resc),
            "win_rate": round(sum(1 for t in resc if t.net_pnl > 0) / len(resc), 4),
            "avgR": round(sum(t.r_multiple for t in resc) / len(resc), 4),
            "max_single_loss": round(min(t.net_pnl for t in resc), 2),
            "max_single_loss_pct_of_start": round(100.0 * min(t.net_pnl for t in resc) / start_equity, 2),
            "median_stop_dist": round(pd.Series([c["stop_dist"] for c, _ in resc_pairs]).median(), 2),
        }
    # stop-distance distribution of everything that reached sizing
    sd = pd.Series([c["stop_dist"] for c in state["calls"]])
    if len(sd):
        out["stop_dist"] = {
            "n": int(len(sd)),
            "p25": round(float(sd.quantile(0.25)), 2),
            "median": round(float(sd.median()), 2),
            "p75": round(float(sd.quantile(0.75)), 2),
            "p90": round(float(sd.quantile(0.90)), 2),
            "max": round(float(sd.max()), 2),
        }
    return out


def slice_year(df, start, end):
    m = (df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end) + pd.Timedelta(hours=23))
    return df.loc[m].reset_index(drop=True)


# ---------------------------------------------------------------- modes
def mode_regime(df, cfg, equity):
    """Structural, trade-sequence-independent: how wide are ATR-derived stops in
    dollars vs what the account can afford, per year."""
    a = _REAL["atr"](df["high"], df["low"], df["close"], 14)
    out = []
    for name, s, e in YEARS:
        m = (df["time"] >= pd.Timestamp(s)) & (df["time"] <= pd.Timestamp(e) + pd.Timedelta(hours=23))
        atr_y = a[m]
        px = df.loc[m, "close"]
        floor_stop = cfg["order"]["sl_min_atr"] * atr_y      # narrowest stop the rules ever allow
        cap_stop = cfg["order"]["sl_max_atr"] * atr_y        # widest
        typical = 1.5 * atr_y                                # representative mid stop
        aff_size = 1.0 / 100 * equity          # max stop $ sizeable at risk 1.0% (0.01 lot => $1/$1)
        aff_cap15 = 1.5 / 100 * equity
        out.append({
            "window": name, "bars": int(m.sum()),
            "median_price": round(float(px.median()), 1),
            "median_atr": round(float(atr_y.median()), 2),
            "median_atr_pct_of_price": round(100.0 * float(atr_y.median()) / float(px.median()), 3),
            "median_min_stop_usd": round(float(floor_stop.median()), 2),
            "median_typ_stop_usd": round(float(typical.median()), 2),
            "median_max_stop_usd": round(float(cap_stop.median()), 2),
            "affordable_stop_at_risk1pct": round(aff_size, 2),
            "affordable_stop_at_cap1.5pct": round(aff_cap15, 2),
            "pct_bars_minstop_unaffordable_risk1": round(100.0 * float((floor_stop > aff_size).mean()), 1),
            "pct_bars_typstop_unaffordable_risk1": round(100.0 * float((typical > aff_size).mean()), 1),
            "pct_bars_typstop_unaffordable_cap15": round(100.0 * float((typical > aff_cap15).mean()), 1),
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True,
                   choices=["regime", "sweep", "risk", "slmax", "fidelity", "full"])
    p.add_argument("--equity", type=float, default=3000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--window", default=None, help="year key from YEARS, or 'all'")
    p.add_argument("--caps", default="none,1.25,1.5,2.0,2.5,3.0")
    p.add_argument("--risks", default="1.0,1.25,1.5,2.0")
    p.add_argument("--slmax", default="2.5,2.0,1.5,1.2")
    p.add_argument("--no-swap", action="store_true")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    if args.mode == "regime":
        for row in mode_regime(df, cfg, args.equity):
            print("REGIME " + json.dumps(row), flush=True)
        return 0

    if args.mode == "fidelity":
        sub = df.iloc[:3000].reset_index(drop=True)
        slow = run_cell(sub, cap_pct=1.5, risk_pct=1.0, cfg=cfg, commission=0.0,
                        start_equity=3000.0, label="slow")
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            fast = run_cell(sub, cap_pct=1.5, risk_pct=1.0, cfg=cfg, commission=0.0,
                            start_equity=3000.0, label="fast")
        finally:
            uninstall_fast_path()
        slow.pop("label"); fast.pop("label")
        print("FIDELITY identical=" + str(slow == fast), flush=True)
        print("SLOW " + json.dumps(slow), flush=True)
        print("FAST " + json.dumps(fast), flush=True)
        return 0 if slow == fast else 1

    windows = YEARS if args.window in (None, "all") else [y for y in YEARS if y[0] == args.window]
    if args.window == "full":
        windows = [("full_2021-26", "2021-07-22", "2026-07-21")]

    for name, s, e in windows:
        sub = slice_year(df, s, e)
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            if args.mode == "sweep":
                for c in args.caps.split(","):
                    cap = None if c.strip().lower() == "none" else float(c)
                    r = run_cell(sub, cap_pct=cap, risk_pct=1.0, cfg=cfg,
                                 commission=args.commission, start_equity=args.equity,
                                 swap=not args.no_swap, label=name)
                    print("RESULT " + json.dumps(r), flush=True)
            elif args.mode == "risk":
                for rk in args.risks.split(","):
                    r = run_cell(sub, cap_pct=1.5, risk_pct=float(rk), cfg=cfg,
                                 commission=args.commission, start_equity=args.equity,
                                 swap=not args.no_swap, label=name)
                    print("RESULT " + json.dumps(r), flush=True)
            elif args.mode == "slmax":
                for sm in args.slmax.split(","):
                    r = run_cell(sub, cap_pct=1.5, risk_pct=1.0, cfg=cfg,
                                 commission=args.commission, start_equity=args.equity,
                                 swap=not args.no_swap, sl_max_atr=float(sm), label=name)
                    print("RESULT " + json.dumps(r), flush=True)
        finally:
            uninstall_fast_path()
    return 0


if __name__ == "__main__":
    sys.exit(main())
