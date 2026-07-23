#!/usr/bin/env python3
"""External shadow-loop process-liveness watchdog -- polls
`common/loop_watchdog.py`'s `check_loop_alive()` on an interval and sends a
Telegram alert the moment `scripts/run_shadow_loop.py`'s process disappears
(2026-07-23 incident: it died silently and sat down for ~2 hours with zero
alert). See `AutoTrade_Watchdog_Start.bat` (repo root) for how this is
normally launched -- same "the console window IS the running process, no
separate PID/stop mechanism, closing the window stops it" convention as
`scripts/run_telegram_control.py`/`scripts/run_dashboard.py`. Deliberately
kept this small/dependency-free (see `loop_watchdog.py`'s module docstring)
so it is far less likely to crash than the trading loop it watches.

    python scripts/run_loop_watchdog.py [--interval-sec 300]
"""
from __future__ import annotations

import argparse
import logging
import time

from autotrade.common.loop_watchdog import check_loop_alive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 300.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--interval-sec", type=float, default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between liveness checks (default {DEFAULT_INTERVAL_SEC:.0f})",
    )
    args = parser.parse_args()

    logger.info("Loop watchdog starting -- checking every %.0fs whether the shadow loop is alive.", args.interval_sec)
    while True:
        running = check_loop_alive()
        logger.info("shadow loop running=%s", running)
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
