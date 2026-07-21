#!/usr/bin/env python3
"""Run the backtest engine over data/historical/{symbol}_H1.csv and persist
a BacktestReport JSON envelope to data/db/backtest_reports/ -- what Appendix
A §5.2's Backtest -> Paper promotion gate reads (via
autotrade.auditor.backtest_results.load_backtest_report_envelope), not a raw
ClosedTrade list.

    python scripts/run_backtest.py XAUUSD --starting-equity 10000
        [--commission-per-lot 0.5] [--slippage-points 5.0]
        [--risk-per-trade-pct 0.5] [--out-of-sample]

Requires an active MT5 session only to resolve the symbol's `SymbolSpec`
(digits/point/tick_value/... -- same as scripts/download_historical.py); the
replay itself runs entirely offline against the historical CSV.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import BacktestReport, format_report, generate_report
from autotrade.common.config import REPO_ROOT, load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.common.symbols import get_symbol_spec
from autotrade.feed.historical import HISTORICAL_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "db" / "backtest_reports"


def build_envelope(
    symbol: str,
    df: pd.DataFrame,
    report: BacktestReport,
    cost_model: CostModelConfig,
    starting_equity: float,
    is_out_of_sample: bool,
) -> dict:
    """The JSON-serializable envelope written to disk -- see
    `auditor/backtest_results.py`'s `BacktestReportEnvelope` for the
    corresponding load-side shape/validation. `cost_model_complete` per
    Appendix A §5.2's "backtest ที่ไม่มี cost model = ไม่นับ": true only if
    commission is actually modeled (non-placeholder) AND slippage uses the
    minimum-1-spread convention (`slippage_points is None`, see
    `CostModelConfig`'s docstring) rather than a possibly-too-small override.
    """
    cost_model_complete = cost_model.commission_per_lot > 0 and cost_model.slippage_points is None
    return {
        "symbol": symbol,
        "bar_range": {"start": str(df["time"].iloc[0]), "end": str(df["time"].iloc[-1])},
        "starting_equity": starting_equity,
        "cost_model": asdict(cost_model),
        "cost_model_complete": cost_model_complete,
        "is_out_of_sample": is_out_of_sample,
        "report": asdict(report),
    }


def run_and_persist(
    symbol: str,
    df: pd.DataFrame,
    symbol_spec: SymbolSpec,
    starting_equity: float,
    risk_per_trade_pct: float,
    cost_model: CostModelConfig,
    is_out_of_sample: bool,
    output_dir: Path,
) -> Path:
    config = BacktestConfig(
        starting_equity=starting_equity, risk_per_trade_pct=risk_per_trade_pct, cost_model=cost_model,
    )
    trades = run_backtest(df, symbol, symbol_spec, config)
    report = generate_report(trades, starting_equity)
    envelope = build_envelope(symbol, df, report, cost_model, starting_equity, is_out_of_sample)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"{symbol}_{stamp}.json"
    out_path.write_text(json.dumps(envelope, indent=2, default=str), encoding="utf-8")

    logger.info("Backtest report written to %s", out_path)
    logger.info("%s", format_report(report))
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbol", help="Canonical symbol, e.g. XAUUSD")
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument(
        "--risk-per-trade-pct", type=float, default=None,
        help="Overrides config/base.yaml's cfo.risk_per_trade_pct",
    )
    parser.add_argument("--commission-per-lot", type=float, default=0.0)
    parser.add_argument(
        "--slippage-points", type=float, default=None,
        help="Omit to use the bar's own spread (minimum-1-spread convention)",
    )
    parser.add_argument(
        "--out-of-sample", action="store_true",
        help="Mark this run as out-of-sample in the envelope (human-set, not auto-detected)",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    csv_path = HISTORICAL_DIR / f"{args.symbol}_H1.csv"
    if not csv_path.exists():
        logger.error("No historical data at %s -- run scripts/download_historical.py first", csv_path)
        return 1
    df = pd.read_csv(csv_path, parse_dates=["time"])

    cfg = load_yaml_config("base")
    risk_per_trade_pct = (
        args.risk_per_trade_pct if args.risk_per_trade_pct is not None else cfg["cfo"]["risk_per_trade_pct"]
    )

    creds = load_mt5_credentials()
    with mt5_session(creds):
        symbol_spec = get_symbol_spec(args.symbol)

    cost_model = CostModelConfig(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points,
    )

    run_and_persist(
        args.symbol, df, symbol_spec, args.starting_equity, risk_per_trade_pct,
        cost_model, args.out_of_sample, args.output_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
