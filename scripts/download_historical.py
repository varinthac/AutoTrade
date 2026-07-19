#!/usr/bin/env python3
"""Phase 1 verification: download historical bars for every symbol in
config/base.yaml and report gap/dedup validation results. Run:

    python scripts/download_historical.py [--days N]
"""
from __future__ import annotations

import argparse
import logging
import sys

from autotrade.common.config import load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.feed.historical import download_historical

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None, help="Overrides historical.default_days in config/base.yaml")
    args = parser.parse_args()

    creds = load_mt5_credentials()
    cfg = load_yaml_config("base")
    symbols = list(cfg["symbols"].keys())
    timeframe = cfg["global"]["timeframe"]
    days = args.days or cfg["historical"]["default_days"]

    had_unexplained_gaps = False

    with mt5_session(creds):
        for symbol in symbols:
            result = download_historical(symbol, timeframe, days)
            if result.unexplained_gaps:
                had_unexplained_gaps = True

    if had_unexplained_gaps:
        logger.warning("Some symbols had unexplained gaps — review before using this data for backtesting.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
