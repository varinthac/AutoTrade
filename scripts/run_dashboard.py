#!/usr/bin/env python3
"""Launches the read-only trade dashboard (Flask, paper mode only) -- trade
history and daily reports over the same trade journal `run_auditor.py`
reads, served locally only. Binds strictly to 127.0.0.1 (never 0.0.0.0): no
auth, read-only reporting, must never be reachable off this machine.

    python scripts/run_dashboard.py [--port 8765] [--db-path PATH]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from autotrade.common import pid_file
from autotrade.common.config import REPO_ROOT
from autotrade.dashboard.app import create_app
from autotrade.store.models import DEFAULT_PAPER_DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 2026-07-24: an RDP reconnect re-fires every "At log on" Task Scheduler
# trigger for that logon event (Shadow Loop, Dashboard, Telegram Control
# alike -- see docs/vps_deployment.md Section 6a), not just the first time.
# A second Flask instance racing the first for the same port fails its
# bind and crashes -- refusing up front here, matching
# run_shadow_loop.py's own PID-file guard, turns that into a clean no-op.
PID_PATH = REPO_ROOT / "data" / "db" / "dashboard.pid"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_PAPER_DB_PATH)
    args = parser.parse_args()

    existing_pid = pid_file.read(PID_PATH)
    if existing_pid is not None and pid_file.is_pid_running(existing_pid):
        logger.error(
            "Dashboard already running (PID %d) -- refusing to start a second instance (it would "
            "just fail to bind the same port).", existing_pid,
        )
        return 1
    try:
        pid_file.write(os.getpid(), PID_PATH)
    except FileExistsError as exc:
        logger.error("Lost the race to claim the PID file: %s", exc)
        return 1

    try:
        app = create_app(db_path=args.db_path)
        app.run(host="127.0.0.1", port=args.port)
    finally:
        pid_file.remove(PID_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
