#!/usr/bin/env python3
"""Re-verification harness (2026-07-22) — re-runs prior EXP conclusions after
TWO cost-model corrections: (1) historical `spread` zero-value floor (data on
disk already fixed), (2) commission corrected to IC Markets Standard = $0/lot
(was phantom $7/lot Raw in every prior EXP).

Covers three re-verification questions:
  P1 (EXP-008): Watchman be/trail BOTH-ON (AllDefaults) vs BOTH-OFF (Struct+Time),
      structure-invalidation + time-stop always on, Risk Voice ON from base.yaml.
  P2 (EXP-002/009): tp_r_multiple sweep, Watchman OFF + Risk Voice OFF (exact
      EXP-002 rejection basis) — reuse exp009 harness `--watchman off` instead;
      this harness handles it too for one-file convenience.
  P3 (EXP-003): session gate all-24h [0,24) vs [14,18), under the LIVE Watchman
      config (be/trail OFF, struct+time ON), Risk Voice ON.

CRITICAL: unlike exp009's `_build_watchman_cfgs`, this builds WatchmanConfig with
ALL SEVEN fields INCLUDING breakeven_enabled/trail_enabled — the exp009 omission
silently defaults them to True/True (the TF-probe NOTE gotcha). Mirrors
scripts/run_backtest.py main() exactly. No production code modified.
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


def _slice(df, start, end):
    out = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]
    return out.reset_index(drop=True)


def _risk_voice(cfg, session_start, session_end):
    rv = dict(cfg["risk_voice"])
    return RiskVoiceConfig(
        max_spread_multiple=rv["max_spread_multiple"],
        max_spread_points_xauusd=rv["max_spread_points_xauusd"],
        news_blackout_before_min=rv["news_blackout_before_min"],
        news_blackout_after_min=rv["news_blackout_after_min"],
        max_stop_atr_multiple=rv["max_stop_atr_multiple"],
        session_start_hour=session_start if session_start is not None else rv["session_start_hour"],
        session_end_hour=session_end if session_end is not None else rv["session_end_hour"],
        friday_close_hour=rv["friday_close_hour"],
        max_atr_panic_multiple=rv["max_atr_panic_multiple"],
    )


def _watchman(cfg, breakeven_enabled, trail_enabled):
    w = cfg["watchman"]
    return WatchmanConfig(
        breakeven_at_r=w["breakeven_at_r"],
        trail_start_r=w["trail_start_r"],
        trail_distance_atr=w["trail_distance_atr"],
        time_stop_hours=w["time_stop_hours"],
        dead_trade_r_band=w["dead_trade_r_band"],
        breakeven_enabled=breakeven_enabled,
        trail_enabled=trail_enabled,
    )


def run_one(df, *, cfg, tp, pivot_bars, commission, start, end,
            watchman_on, breakeven_enabled, trail_enabled,
            risk_voice_on, session_start, session_end):
    sdf = _slice(df, start, end)
    rv = _risk_voice(cfg, session_start, session_end) if risk_voice_on else None
    wm = _watchman(cfg, breakeven_enabled, trail_enabled) if watchman_on else None
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
    p.add_argument("--commission", type=float, required=True)
    p.add_argument("--windows", default="y1,y2,y3,val")
    p.add_argument("--tp", type=float, default=2.0)
    p.add_argument("--pivot", type=int, default=None, help="default = base.yaml global.swing_pivot_bars")
    p.add_argument("--watchman", choices=["on", "off"], default="on")
    p.add_argument("--breakeven", choices=["on", "off"], default="off")
    p.add_argument("--trail", choices=["on", "off"], default="off")
    p.add_argument("--risk-voice", choices=["on", "off"], default="on")
    p.add_argument("--session-start", type=int, default=None)
    p.add_argument("--session-end", type=int, default=None)
    p.add_argument("--label", default="run")
    args = p.parse_args()

    cfg = load_yaml_config("base")
    pivot = args.pivot if args.pivot is not None else cfg["global"]["swing_pivot_bars"]
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]

    for w in windows:
        start, end = WINDOWS[w]
        res = run_one(
            df, cfg=cfg, tp=args.tp, pivot_bars=pivot, commission=args.commission,
            start=start, end=end,
            watchman_on=(args.watchman == "on"),
            breakeven_enabled=(args.breakeven == "on"),
            trail_enabled=(args.trail == "on"),
            risk_voice_on=(args.risk_voice == "on"),
            session_start=args.session_start, session_end=args.session_end,
        )
        print("RESULT " + json.dumps({
            "label": args.label, "window": w, "tp": args.tp, "pivot": pivot,
            "watchman": args.watchman, "be": args.breakeven, "trail": args.trail,
            "rv": args.risk_voice, "sess": [args.session_start, args.session_end],
            "comm": args.commission, **res,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
