#!/usr/bin/env python3
"""EXP-012 / EXP-013 — PURE ADDITIVE cross-timeframe CONFIRMATION FILTER on the
current-live H1 pipeline (analysis-only; NEW family = "confluence filter").

WHAT THIS IS
------------
The current-live H1 Council/RiskVoice/Shield/CFO/Watchman pipeline is kept
COMPLETELY UNCHANGED as the decision-maker (Watchman struct+time ON / be+trail
OFF, RiskVoice ON all-24h, tp 2.0, pivot 3 — the exact config/base.yaml). On top
of it we add a boolean GATE: an H1 signal that would normally trade is only taken
if a second timeframe AGREES with the H1 signal's direction. This can ONLY REDUCE
trade count (it is a filter, never adds signals).

  EXP-012: M30 momentum agreement. Confirm iff the last CLOSED M30 bar's close is
           on the same side of a short M30 EMA(P) as the H1 signal direction
           (BUY needs m30_close > ema, SELL needs m30_close < ema). Knob = P.
  EXP-013: H4 trend agreement. H4 is DERIVED from H1 (byte-exact 4h resample;
           fills still use H1 bars, so the cost model is untouched). Confirm iff
           the last CLOSED H4 bar's close is on the same side of an H4 EMA(Q) as
           the H1 signal direction. Knob = Q.

WHY THE GATE IS INJECTED INTO signal_fn (not a post-hoc trade filter)
--------------------------------------------------------------------
max_positions_per_symbol=1: dropping a trade frees the engine to take a LATER
signal it previously blocked (the "reshuffle" this log flags repeatedly). Only a
gate INSIDE signal_fn models that faithfully — same pattern as
session_window_harness.py. With `--filter none` the gate is a pass-through and
must reproduce the exp_reverify baseline (fidelity check).

NO LOOKAHEAD: the confirming TF bar used at H1 decision (close of H1 bar i,
= H1 open + 1h) is the last TF bar whose CLOSE time <= that instant. An M30/H4
bar closing exactly at the H1 close is composed only of price up to that instant
(the same data H1 bar i saw) — so it is known, not future.

Config building mirrors exp_reverify_costfix_harness.py (all 7 WatchmanConfig
fields incl. breakeven_enabled/trail_enabled). No production code modified.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, _council_signal_fn, run_backtest
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.watchman.evaluate import WatchmanConfig

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

WINDOWS = {
    "train": ("2021-07-22", "2024-07-21"),
    "val":   ("2024-07-21", "2025-07-21"),
    "test":  ("2025-07-21", "2026-07-21"),
    "y1":    ("2021-07-22", "2022-07-21"),
    "y2":    ("2022-07-21", "2023-07-21"),
    "y3":    ("2023-07-21", "2024-07-21"),
    "y4":    ("2024-07-21", "2025-07-21"),  # == val
}


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


def _risk_voice(cfg):
    rv = dict(cfg["risk_voice"])
    return RiskVoiceConfig(
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


def _watchman(cfg):
    w = cfg["watchman"]
    return WatchmanConfig(
        breakeven_at_r=w["breakeven_at_r"],
        trail_start_r=w["trail_start_r"],
        trail_distance_atr=w["trail_distance_atr"],
        time_stop_hours=w["time_stop_hours"],
        dead_trade_r_band=w["dead_trade_r_band"],
        breakeven_enabled=w["breakeven_enabled"],
        trail_enabled=w["trail_enabled"],
    )


# ---------------------------------------------------------------------------
# Confirmation-diff series, keyed by H1 OPEN timestamp (full history, warm EMA).
# diff > 0  => confirming TF is bullish (confirms a BUY)
# diff < 0  => confirming TF is bearish (confirms a SELL)
# ---------------------------------------------------------------------------
def _resample_h4(h1: pd.DataFrame) -> pd.DataFrame:
    s = h1.set_index("time")
    h4 = pd.DataFrame({
        "open": s["open"].resample("4h").first(),
        "high": s["high"].resample("4h").max(),
        "low": s["low"].resample("4h").min(),
        "close": s["close"].resample("4h").last(),
    }).dropna()
    return h4.reset_index()  # columns: time (H4 open), open, high, low, close


def build_conf_diff(h1: pd.DataFrame, tf: str, period: int) -> pd.Series:
    """Return a Series (index = H1 open timestamp) of (tf_close - tf_ema) for the
    last TF bar CLOSED at or before each H1 bar's close (= H1 open + 1h)."""
    if tf == "m30":
        tfdf = pd.read_csv(HISTORICAL_DIR / "XAUUSD_M30.csv", parse_dates=["time"])
        tf_delta = pd.Timedelta(minutes=30)
    elif tf == "h4":
        tfdf = _resample_h4(h1)
        tf_delta = pd.Timedelta(hours=4)
    else:
        raise ValueError(tf)

    tfdf = tfdf.sort_values("time").reset_index(drop=True)
    ema = tfdf["close"].ewm(span=period, adjust=False).mean()
    diff = (tfdf["close"] - ema).to_numpy()
    tf_close_times = (tfdf["time"] + tf_delta).to_numpy()  # when each TF bar closes

    h1_open = h1["time"].to_numpy()
    h1_close = (h1["time"] + pd.Timedelta(hours=1)).to_numpy()  # H1 decision instant
    # last TF bar with close_time <= h1_close  (include == : same-instant, no lookahead)
    idx = tf_close_times.searchsorted(h1_close, side="right") - 1
    vals = [diff[j] if j >= 0 else float("nan") for j in idx]
    return pd.Series(vals, index=pd.DatetimeIndex(h1_open))


def make_confluence_signal_fn(conf_diff: pd.Series | None):
    """Wrap the stock council signal fn with a cross-TF agreement gate. If
    conf_diff is None -> pass-through (fidelity/baseline). A NaN diff (TF data
    not yet available) => NOT confirmed (conservative: filter can only remove)."""
    if conf_diff is None:
        return _council_signal_fn

    lut = conf_diff.to_dict()

    def gated(df, as_of_index, **kwargs):
        plan = _council_signal_fn(df, as_of_index, **kwargs)
        if plan is None:
            return None
        ts = pd.Timestamp(df["time"].iloc[as_of_index])
        d = lut.get(ts)
        if d is None or d != d:  # missing or NaN
            return None
        if plan.direction == "BUY":
            return plan if d > 0 else None
        else:  # SELL
            return plan if d < 0 else None

    return gated


def run_one(df, conf_diff, *, cfg, commission, start, end):
    sdf = _slice(df, start, end)
    bt = BacktestConfig(
        starting_equity=10_000.0,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
        tp_r_multiple=cfg["order"]["tp_r_multiple"],
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        risk_voice_cfg=_risk_voice(cfg),
        watchman_cfg=_watchman(cfg),
        signal_fn=make_confluence_signal_fn(conf_diff),
    )
    trades = run_backtest(sdf, "XAUUSD", _SPEC, bt)
    rep = generate_report(trades, 10_000.0)
    return {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4),
        "PF": round(rep.profit_factor, 4) if rep.profit_factor is not None else None,
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 is not None else None,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--windows", default="y1,y2,y3,val")
    p.add_argument("--filter", choices=["none", "m30", "h4"], default="none")
    p.add_argument("--period", type=int, default=14, help="EMA period on the confirming TF")
    p.add_argument("--label", default="run")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    conf_diff = None
    if args.filter != "none":
        conf_diff = build_conf_diff(df, args.filter, args.period)

    for w in [x.strip() for x in args.windows.split(",") if x.strip()]:
        start, end = WINDOWS[w]
        res = run_one(df, conf_diff, cfg=cfg, commission=args.commission, start=start, end=end)
        print("RESULT " + json.dumps({
            "label": args.label, "window": w, "filter": args.filter,
            "period": args.period if args.filter != "none" else None,
            "comm": args.commission, **res,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
