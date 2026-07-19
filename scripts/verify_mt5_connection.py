#!/usr/bin/env python3
"""Phase 0 verification: log into the MT5 demo account and print live
XAUUSD ticks for a short window. Run:

    python scripts/verify_mt5_connection.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import MetaTrader5 as mt5

from autotrade.common.config import load_mt5_credentials
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import get_symbol_spec

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TICK_WINDOW_SECONDS = 30
POLL_INTERVAL_SECONDS = 1.0


def main() -> int:
    creds = load_mt5_credentials()

    with mt5_session(creds):
        spec = get_symbol_spec("XAUUSD")
        logger.info(
            "XAUUSD resolved to broker symbol %r (digits=%d, tick_size=%s, contract_size=%s)",
            spec.broker_name, spec.digits, spec.tick_size, spec.contract_size,
        )

        logger.info("Streaming live ticks for %ds ...", TICK_WINDOW_SECONDS)
        last_time_msc = None
        deadline = time.monotonic() + TICK_WINDOW_SECONDS
        tick_count = 0

        while time.monotonic() < deadline:
            tick = mt5.symbol_info_tick(spec.broker_name)
            if tick is not None and tick.time_msc != last_time_msc:
                last_time_msc = tick.time_msc
                tick_count += 1
                logger.info("tick #%d: bid=%s ask=%s time=%s", tick_count, tick.bid, tick.ask, tick.time)
            time.sleep(POLL_INTERVAL_SECONDS)

        if tick_count == 0:
            logger.error("No ticks received in %ds — market may be closed, or symbol/server misconfigured", TICK_WINDOW_SECONDS)
            return 1

        logger.info("OK: received %d ticks. MT5 connectivity verified.", tick_count)
        return 0


if __name__ == "__main__":
    sys.exit(main())
