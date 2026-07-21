#!/usr/bin/env python3
"""EXP-009 harness: sweep `tp_r_multiple` and `pivot_bars` under BOTH
Watchman-OFF and Watchman-ON conditions, per-year Train+Validation.

WHY: EXP-002 rejected tp 2.25/2.5 but ran on the OLD engine where Watchman
exits were not modeled at all (watchman_cfg=None). Engine commit 67df406 now
simulates Watchman when a WatchmanConfig is passed. So `tp_r_multiple` needs a
fresh look under the REAL Watchman interaction EXP-006 diagnosed (breakeven 1.0
/ trail 1.5 both sit below the 2.0 target, cutting winners). `pivot_bars` is a
brand-new family: it is a hardcoded default (3) never exposed in base.yaml.

Two conditions, selected by --watchman:
  off : watchman_cfg=None, risk_voice_cfg=None  -> reproduces EXP-002's setup
        (fidelity target: tp2.0 Train 587 tr PF 1.084; Val 223 tr PF 1.064).
  on  : watchman_cfg + risk_voice_cfg from base.yaml -> reproduces EXP-006's
        setup (fidelity target: tp2.0 Train agg PF 0.9922; Val PF 0.9884).

No production code modified -- only public BacktestConfig fields + the same
RiskVoiceConfig/WatchmanConfig injection points the existing harnesses use.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
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

_WATCHMAN_PARAMS = (
    "breakeven_at_r", "trail_start_r", "trail_distance_atr",
    "time_stop_hours", "dead_trade_r_band",
)


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


def _build_watchman_cfgs(cfg):
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
    wm = WatchmanConfig(**{p: cfg["watchman"][p] for p in _WATCHMAN_PARAMS})
    return rv, wm


def run_one(df, *, tp, pivot_bars, watchman_on, cfg, commission, start, end):
    sdf = _slice(df, start, end)
    if watchman_on:
        rv, wm = _build_watchman_cfgs(cfg)
    else:
        rv, wm = None, None
    bt = BacktestConfig(
        starting_equity=10_000.0,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
        tp_r_multiple=tp,
        pivot_bars=pivot_bars,
        risk_voice_cfg=rv,
        watchman_cfg=wm,
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
    p.add_argument("--param", choices=["tp", "pivot"], required=True)
    p.add_argument("--values", required=True, help="comma-separated values")
    p.add_argument("--watchman", choices=["on", "off"], required=True)
    p.add_argument("--windows", default="y1,y2,y3,val")
    p.add_argument("--commission", type=float, default=7.0)
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    watchman_on = args.watchman == "on"
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]

    for raw in args.values.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if args.param == "tp":
            tp, pivot_bars = float(raw), 3
            val_label = tp
        else:
            tp, pivot_bars = 2.0, int(raw)
            val_label = pivot_bars
        for w in windows:
            start, end = WINDOWS[w]
            res = run_one(df, tp=tp, pivot_bars=pivot_bars, watchman_on=watchman_on,
                          cfg=cfg, commission=args.commission, start=start, end=end)
            print("RESULT " + json.dumps({
                "param": args.param, "value": val_label, "watchman": args.watchman,
                "window": w, **res,
            }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
