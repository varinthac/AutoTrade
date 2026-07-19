#!/usr/bin/env python3
"""Phase 0 verification: log into the MT5 demo account and print live
XAUUSD ticks for a short window. Run:

    python scripts/verify_mt5_connection.py
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime

import MetaTrader5 as mt5

from autotrade.common.config import load_mt5_credentials
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import get_symbol_spec

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TICK_WINDOW_SECONDS = 30
POLL_INTERVAL_SECONDS = 1.0
# A single tick that never advances just means we sampled the last cached
# quote once — that happens identically whether the market is live or
# closed. Only a *second* distinct tick proves ticks are actually streaming.
MIN_DISTINCT_TICKS_FOR_LIVE = 2


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
        last_tick = None
        deadline = time.monotonic() + TICK_WINDOW_SECONDS
        tick_count = 0

        while time.monotonic() < deadline:
            tick = mt5.symbol_info_tick(spec.broker_name)
            # A just-selected symbol can report a zero-filled placeholder
            # tick (time=0, bid=ask=0.0) before the first real tick arrives.
            if tick is not None and tick.time != 0 and tick.time_msc != last_time_msc:
                last_time_msc = tick.time_msc
                last_tick = tick
                tick_count += 1
                logger.info("tick #%d: bid=%s ask=%s time=%s", tick_count, tick.bid, tick.ask, tick.time)
            time.sleep(POLL_INTERVAL_SECONDS)

        if tick_count == 0:
            logger.error("No ticks received in %ds — market may be closed, or symbol/server misconfigured", TICK_WINDOW_SECONDS)
            return 1

        if tick_count < MIN_DISTINCT_TICKS_FOR_LIVE:
            # tick.time is broker server time, not true UTC (see
            # common/mt5_time.py) — this staleness estimate is approximate
            # (off by the server's UTC offset), but still clearly separates
            # "just now" from "days-old cached quote from before a weekend close".
            age = datetime.utcnow() - datetime.utcfromtimestamp(last_tick.time)
            logger.warning(
                "Only %d distinct tick observed in %ds (unchanged the rest of the window) — "
                "this looks like a STALE cached quote (~%s old), not a live feed. "
                "Market is likely closed. Re-run during active trading hours to confirm live streaming.",
                tick_count, TICK_WINDOW_SECONDS, age,
            )
            return 1

        logger.info("OK: received %d distinct ticks, confirmed advancing. MT5 live connectivity verified.", tick_count)
        return 0


if __name__ == "__main__":
    sys.exit(main())
