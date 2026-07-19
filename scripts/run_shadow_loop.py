#!/usr/bin/env python3
"""Phase 3d: continuous demo shadow-running loop -- the full
feed -> council(trivial_signal) -> risk(CFO sizing) -> execution pipeline,
run continuously against the MT5 demo account (spec.md §6 Phase 3d). This is
the Phase-3 stand-in for what will eventually become run_sandbox.py/run_live.py.

Defaults to --adapter noop (dry run, no MT5 orders ever sent). Passing
--adapter demo is what actually places real orders on the demo account and
must be requested explicitly every run, per spec.md §7 fail-safe defaults.

    python scripts/run_shadow_loop.py [--adapter noop|demo] [--seed-bars N]
"""
from __future__ import annotations

import argparse
import logging
import sys

import MetaTrader5 as mt5
import pandas as pd

from autotrade.common.clock import RealClock
from autotrade.common.config import MT5Credentials, load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import to_broker_name
from autotrade.execution.adapter import BrokerAdapter
from autotrade.execution.demo_adapter import ThrottledDemoAdapter
from autotrade.execution.noop_adapter import NoOpBrokerAdapter
from autotrade.feed.poller import TIMEFRAME_MAP
from autotrade.orchestrator.shadow_loop import ShadowLoop, ShadowLoopConfig
from autotrade.risk.circuit_breaker import CircuitBreaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SEED_BARS = 200


def seed_history(symbol: str, timeframe: str, bars: int, symbol_map: dict[str, str]) -> pd.DataFrame:
    """Fetch the last `bars` fully-closed bars to seed the rolling window
    build_trade_idea() needs for EMA20/50 and swing detection. Requires an
    active mt5_session(). Position 1 (not 0) skips the still-forming bar,
    same convention as feed/poller.fetch_last_closed_bar."""
    broker_symbol = to_broker_name(symbol, symbol_map)
    mt5_timeframe = TIMEFRAME_MAP[timeframe]
    rates = mt5.copy_rates_from_pos(broker_symbol, mt5_timeframe, 1, bars)
    if rates is None or len(rates) == 0:
        code, desc = mt5.last_error()
        raise RuntimeError(
            f"copy_rates_from_pos({broker_symbol!r}) returned nothing while seeding history: [{code}] {desc}"
        )

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def build_adapter(name: str, creds: MT5Credentials, clock: RealClock) -> BrokerAdapter:
    if name == "noop":
        return NoOpBrokerAdapter()
    if name == "demo":
        return ThrottledDemoAdapter(creds, clock)
    raise ValueError(f"unknown --adapter {name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--adapter", choices=["noop", "demo"], default="noop",
        help="noop (default, dry run, no orders sent) or demo (places real orders on the MT5 demo account)",
    )
    parser.add_argument(
        "--seed-bars", type=int, default=DEFAULT_SEED_BARS,
        help=f"Number of closed bars to seed the rolling history with (default {DEFAULT_SEED_BARS})",
    )
    parser.add_argument("--poll-interval-sec", type=float, default=5.0)
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after this many poll iterations (mainly for smoke-testing); default runs forever",
    )
    args = parser.parse_args()

    creds = load_mt5_credentials()
    cfg = load_yaml_config("base")
    symbols = list(cfg["symbols"].keys())
    symbol_map = cfg["symbols"]
    timeframe = cfg["global"]["timeframe"]

    clock = RealClock()
    adapter = build_adapter(args.adapter, creds, clock)
    if args.adapter == "noop":
        logger.info("Using NoOpBrokerAdapter -- dry run, no orders will be sent to any broker.")
    else:
        logger.warning("Using ThrottledDemoAdapter -- REAL orders will be sent to the MT5 demo account.")

    circuit_breaker = CircuitBreaker(
        daily_loss_limit_pct=cfg["cfo"]["daily_loss_limit_pct"],
        max_consecutive_losses=cfg["cfo"]["max_consecutive_losses"],
        max_drawdown_halt_pct=cfg["cfo"]["max_drawdown_halt_pct"],
    )
    loop_cfg = ShadowLoopConfig(
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        sl_buffer_atr=cfg["order"]["sl_buffer_atr"],
        sl_min_atr=cfg["order"]["sl_min_atr"],
        sl_max_atr=cfg["order"]["sl_max_atr"],
        tp_r_multiple=cfg["order"]["tp_r_multiple"],
    )

    with mt5_session(creds):
        initial_history = {
            symbol: seed_history(symbol, timeframe, args.seed_bars, symbol_map) for symbol in symbols
        }
        for symbol, df in initial_history.items():
            logger.info(
                "Seeded %s with %d bars (%s -> %s)", symbol, len(df), df["time"].iloc[0], df["time"].iloc[-1],
            )

        shadow_loop = ShadowLoop(
            adapter=adapter, circuit_breaker=circuit_breaker, cfg=loop_cfg,
            initial_history=initial_history, symbol_map=symbol_map, clock=clock,
        )
        logger.info(
            "Shadow loop starting: symbols=%s timeframe=%s adapter=%s -- waiting for next bar close",
            symbols, timeframe, args.adapter,
        )
        shadow_loop.run(
            symbols, timeframe, poll_interval_sec=args.poll_interval_sec, max_iterations=args.max_iterations,
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Shadow loop stopped (Ctrl+C).")
        sys.exit(0)
