#!/usr/bin/env python3
"""Experiment harness for EXP-006: the Watchman exit-management parameter family
(breakeven_at_r, trail_start_r, trail_distance_atr, time_stop_hours,
dead_trade_r_band).

WHY THIS EXISTS: as of commit 67df406 (2026-07-21) `backtest/engine.py` now
actually simulates Watchman's exit management when a `WatchmanConfig` is passed,
and `scripts/run_backtest.py` ALWAYS builds one from `config/base.yaml`'s
`watchman:` block. So for the first time these five parameters genuinely move the
backtest. This harness reproduces `scripts/run_backtest.py`'s CLI run EXACTLY
(same Risk Voice cfg from base.yaml, same Watchman cfg from base.yaml, same cost
model: commission $7/lot, slippage = bar's own spread) but lets us OVERRIDE one
Watchman parameter at a time and run many (value x window) combinations
in-process, cheaply, without shelling out to the CLI or re-resolving the MT5
SymbolSpec on every run.

Fidelity target: baseline (no override) on the Test slice 2025-07-21..2026-07-21
should reproduce the session's CLI baseline (243 trades, PF 1.21, net ~+$1,121,
maxDD 3.81%). If it does, the harness is faithful and safe to sweep with.

Cached SymbolSpec is the real IC Markets XAUUSD spec (identical to
session_window_harness.py), so no MT5 session is needed.
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

_CACHED_SPEC = {
    "XAUUSD": SymbolSpec(
        canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
        tick_size=0.01, tick_value=1.0, contract_size=100.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, freeze_level=0,
    ),
}

# Chronological splits — identical to EXP-001..005.
WINDOWS = {
    "train": ("2021-07-22", "2024-07-21"),
    "val":   ("2024-07-21", "2025-07-21"),
    "test":  ("2025-07-21", "2026-07-21"),
    "y1":    ("2021-07-22", "2022-07-21"),
    "y2":    ("2022-07-21", "2023-07-21"),
    "y3":    ("2023-07-21", "2024-07-21"),
    "y4":    ("2024-07-21", "2025-07-21"),  # == val
}

WATCHMAN_PARAMS = (
    "breakeven_at_r", "trail_start_r", "trail_distance_atr",
    "time_stop_hours", "dead_trade_r_band",
)


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


def _build_cfgs(cfg: dict, override_param: str | None, override_value: float | None):
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
    wm_vals = {p: cfg["watchman"][p] for p in WATCHMAN_PARAMS}
    if override_param is not None:
        wm_vals[override_param] = override_value
    wm = WatchmanConfig(**wm_vals)
    return rv, wm


def run_slice(df, symbol, spec, cfg, rv, wm, commission, start, end) -> dict:
    sdf = _slice(df, start, end)
    cost_model = CostModelConfig(commission_per_lot=commission, slippage_points=None)
    bt = BacktestConfig(
        starting_equity=10_000.0,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=cost_model,
        risk_voice_cfg=rv,
        watchman_cfg=wm,
    )
    trades = run_backtest(sdf, symbol, spec, bt)
    rep = generate_report(trades, 10_000.0)
    return {
        "trade_count": rep.trade_count,
        "win_rate": round(rep.win_rate, 4),
        "profit_factor": round(rep.profit_factor, 4) if rep.profit_factor is not None else None,
        "net": round(rep.total_net_pnl, 1),
        "avg_r": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "dd_pct": round(rep.max_drawdown_pct, 2),
        "pf_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 is not None else None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbol", nargs="?", default="XAUUSD")
    p.add_argument("--param", default="none",
                   help="watchman param to sweep, or 'none' for baseline only")
    p.add_argument("--values", default="",
                   help="comma-separated override values for --param")
    p.add_argument("--windows", default="train,val",
                   help="comma-separated window keys: " + ",".join(WINDOWS))
    p.add_argument("--commission-per-lot", type=float, default=7.0)
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / f"{args.symbol}_H1.csv", parse_dates=["time"])
    spec = _CACHED_SPEC[args.symbol]
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]

    # Baseline (config defaults) across all requested windows.
    rv, wm = _build_cfgs(cfg, None, None)
    for w in windows:
        start, end = WINDOWS[w]
        res = run_slice(df, args.symbol, spec, cfg, rv, wm, args.commission_per_lot, start, end)
        print("RESULT " + json.dumps({"param": "baseline", "value": None, "window": w, **res}))

    if args.param != "none" and args.values:
        base_default = cfg["watchman"][args.param]
        for v in [float(x) for x in args.values.split(",")]:
            if v == base_default:
                continue  # baseline already emitted
            rv, wm = _build_cfgs(cfg, args.param, v)
            for w in windows:
                start, end = WINDOWS[w]
                res = run_slice(df, args.symbol, spec, cfg, rv, wm, args.commission_per_lot, start, end)
                print("RESULT " + json.dumps({"param": args.param, "value": v, "window": w, **res}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
