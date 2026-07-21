#!/usr/bin/env python3
"""Experiment harness for the session-window tuning experiment (Experiment #1:
coupled pair risk_voice.session_start_hour x session_end_hour).

WHY THIS EXISTS (methodology note): scripts/run_backtest.py's engine does NOT
wire in Risk Voice (see backtest/engine.py's "Known gap (Phase 6b)" docstring),
so its output is UNGATED by session -- editing config/base.yaml's
session_start_hour/session_end_hour has ZERO effect on that CLI. To evaluate the
session window we must reproduce, in the backtest, exactly what Risk Voice does
live: veto (drop) any entry whose bar hour is outside [start, end) server time
(risk_voice.py condition 4, half-open interval). We do that by wrapping the
stock _council_signal_fn with a session gate -- entries are gated, but ALL bars
are still fed to the engine so exit (SL/TP/gap) simulation stays correct. No
production/pipeline code is modified; this only composes existing public
functions (the engine's signal_fn is explicitly an injection point).

Fidelity: with `--start-hour none` this is byte-for-byte the same run as
run_backtest.py (no gate) -- used to validate the harness against the known
baseline XAUUSD_20260721T060416Z.json (199 trades, PF 1.2769).
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
from autotrade.feed.historical import HISTORICAL_DIR

# Real IC Markets XAUUSD SymbolSpec, resolved once via MT5 (get_symbol_spec) and
# cached here so parallel harness runs don't each call mt5.initialize (which is
# process-global and races across processes). Verified: with this spec + no gate,
# the harness reproduces XAUUSD_20260721T060416Z.json byte-for-byte.
_CACHED_SPEC = {
    "XAUUSD": SymbolSpec(
        canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
        tick_size=0.01, tick_value=1.0, contract_size=100.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, freeze_level=0,
    ),
}


def make_session_gated_signal_fn(start_hour, end_hour):
    """Return a signal_fn that returns None (no entry) when the bar's server
    hour is outside [start_hour, end_hour), else defers to the stock council
    signal fn. Mirrors risk_voice.py condition 4 exactly: half-open interval,
    server time == the CSV `time` column (config global.timezone: server)."""
    if start_hour is None:
        return _council_signal_fn

    def gated(df, as_of_index, **kwargs):
        hour = pd.Timestamp(df["time"].iloc[as_of_index]).hour
        if not (start_hour <= hour < end_hour):
            return None
        return _council_signal_fn(df, as_of_index, **kwargs)

    return gated


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbol")
    p.add_argument("--start-hour", default="none",
                   help="session_start_hour, or 'none' for no session gate")
    p.add_argument("--end-hour", type=int, default=None)
    p.add_argument("--start-date", default=None)
    p.add_argument("--end-date", default=None)
    p.add_argument("--starting-equity", type=float, default=10_000.0)
    p.add_argument("--commission-per-lot", type=float, default=7.0)
    p.add_argument("--label", default="")
    args = p.parse_args()

    start_hour = None if str(args.start_hour).lower() == "none" else int(args.start_hour)
    end_hour = args.end_hour

    df = pd.read_csv(HISTORICAL_DIR / f"{args.symbol}_H1.csv", parse_dates=["time"])
    if args.start_date:
        df = df[df["time"] >= pd.Timestamp(args.start_date)]
    if args.end_date:
        df = df[df["time"] < pd.Timestamp(args.end_date)]
    df = df.reset_index(drop=True)

    symbol_spec = _CACHED_SPEC[args.symbol]

    cfg = load_yaml_config("base")
    cost_model = CostModelConfig(commission_per_lot=args.commission_per_lot, slippage_points=None)
    bt = BacktestConfig(
        starting_equity=args.starting_equity,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=cost_model,
        signal_fn=make_session_gated_signal_fn(start_hour, end_hour),
    )
    trades = run_backtest(df, args.symbol, symbol_spec, bt)
    rep = generate_report(trades, args.starting_equity)

    out = {
        "label": args.label,
        "window": "none" if start_hour is None else f"[{start_hour},{end_hour})",
        "bar_range": {"start": str(df["time"].iloc[0]), "end": str(df["time"].iloc[-1])},
        "trade_count": rep.trade_count,
        "win_rate": rep.win_rate,
        "profit_factor": rep.profit_factor,
        "total_net_pnl": rep.total_net_pnl,
        "avg_r_multiple": rep.avg_r_multiple,
        "max_drawdown_pct": rep.max_drawdown_pct,
        "profit_factor_excluding_top_5": rep.profit_factor_excluding_top_5,
    }
    print("RESULT " + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
