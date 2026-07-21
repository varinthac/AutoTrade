#!/usr/bin/env python3
"""General parameter-sweep harness for XAUUSD backtest tuning.

Constructs BacktestConfig with arbitrary strategy-param overrides (sl_buffer_atr,
sl_min_atr, sl_max_atr, tp_r_multiple, pivot_bars, bull/bear/conflict thresholds)
and runs the STOCK engine + STOCK signal fn over a date window. No production code
modified -- only public BacktestConfig fields are set.

Fidelity: with all defaults and no date filter this reproduces
scripts/run_backtest.py's output byte-for-byte (same engine, same defaults, same
cost model: commission $7/lot, slippage = bar's own spread). Risk Voice is left
None (risk_voice_cfg not passed), matching the CLI reports the baseline came from.

Usage:
  python experiments/param_sweep_harness.py --start 2021-07-22 --end 2024-07-21 \
      --tp 2.0 --sl-buffer 0.2 --sl-min 0.8
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import generate_report
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.feed.historical import HISTORICAL_DIR

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def run_one(df, *, sl_buffer, sl_min, sl_max, tp, pivot_bars,
            bull, bear, conflict, risk_pct, commission, starting_equity):
    cfg = BacktestConfig(
        starting_equity=starting_equity,
        risk_per_trade_pct=risk_pct,
        cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
        sl_buffer_atr=sl_buffer, sl_min_atr=sl_min, sl_max_atr=sl_max,
        tp_r_multiple=tp, pivot_bars=pivot_bars,
        bull_threshold=bull, bear_threshold=bear, conflict_threshold=conflict,
    )
    trades = run_backtest(df, "XAUUSD", _SPEC, cfg)
    rep = generate_report(trades, starting_equity)
    return {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4),
        "PF": round(rep.profit_factor, 4),
        "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4),
        "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--sl-buffer", type=float, default=0.2)
    p.add_argument("--sl-min", type=float, default=0.8)
    p.add_argument("--sl-max", type=float, default=2.5)
    p.add_argument("--tp", type=float, default=2.0)
    p.add_argument("--pivot-bars", type=int, default=3)
    p.add_argument("--bull", type=int, default=70)
    p.add_argument("--bear", type=int, default=70)
    p.add_argument("--conflict", type=int, default=55)
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--commission", type=float, default=7.0)
    p.add_argument("--starting-equity", type=float, default=10000.0)
    p.add_argument("--label", default="")
    args = p.parse_args()

    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    if args.start:
        df = df[df["time"] >= pd.Timestamp(args.start)]
    if args.end:
        df = df[df["time"] < pd.Timestamp(args.end)]
    df = df.reset_index(drop=True)

    res = run_one(
        df, sl_buffer=args.sl_buffer, sl_min=args.sl_min, sl_max=args.sl_max,
        tp=args.tp, pivot_bars=args.pivot_bars, bull=args.bull, bear=args.bear,
        conflict=args.conflict, risk_pct=args.risk_pct, commission=args.commission,
        starting_equity=args.starting_equity,
    )
    res["label"] = args.label
    res["window"] = {"start": str(df["time"].iloc[0]), "end": str(df["time"].iloc[-1])}
    res["params"] = {"sl_buffer": args.sl_buffer, "sl_min": args.sl_min,
                     "sl_max": args.sl_max, "tp": args.tp, "pivot_bars": args.pivot_bars,
                     "bull": args.bull, "bear": args.bear, "conflict": args.conflict}
    print("RESULT " + json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
