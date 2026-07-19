#!/usr/bin/env python3
"""Phase 1 verification: poll configured symbols and log each new closed
H1 bar as it forms. Run:

    python scripts/watch_bar_closes.py
"""
from __future__ import annotations

import logging
import sys

from autotrade.common.config import load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.feed.poller import poll_new_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    creds = load_mt5_credentials()
    cfg = load_yaml_config("base")
    symbols = list(cfg["symbols"].keys())
    timeframe = cfg["global"]["timeframe"]

    logger.info("Watching %s on %s for new closed bars. Ctrl+C to stop.", symbols, timeframe)

    with mt5_session(creds):
        poll_new_bars(symbols, timeframe, on_new_bar=lambda snap: None, poll_interval_sec=15.0)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
