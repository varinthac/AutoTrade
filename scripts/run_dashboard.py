#!/usr/bin/env python3
"""Launches the read-only trade dashboard (Flask, paper mode only) -- trade
history and daily reports over the same trade journal `run_auditor.py`
reads, served locally only. Binds strictly to 127.0.0.1 (never 0.0.0.0): no
auth, read-only reporting, must never be reachable off this machine.

    python scripts/run_dashboard.py [--port 8765] [--db-path PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autotrade.dashboard.app import create_app
from autotrade.store.models import DEFAULT_PAPER_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_PAPER_DB_PATH)
    args = parser.parse_args()

    app = create_app(db_path=args.db_path)
    app.run(host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
