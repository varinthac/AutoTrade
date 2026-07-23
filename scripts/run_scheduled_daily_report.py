#!/usr/bin/env python3
"""Thin wrapper around `scripts/run_auditor.py daily` for the Windows Task
Scheduler entry "AutoTrade Daily Report" (fires 09:00 local daily).

`run_auditor.py daily` defaults `--date` to the CURRENT MT5 broker server
date -- correct for an interactive/ad-hoc invocation, but wrong for this
scheduled one: 09:00 local is only ~4-5h past MT5 server-day rollover
(2026-07-23 DST measurement: server midnight lands at 05:00-06:00 local
depending on season -- see experiments/experiments_log.md's DST NOTE), so
"today" (server date) is only a few hours old and mostly empty at that
hour. The report a human actually wants at 09:00 is YESTERDAY's COMPLETE
day, not a half-formed snapshot of today.

This resolves "yesterday" from LOCAL wall-clock date, not a fresh MT5
server-date lookup -- safe specifically because the scheduled run always
fires at 09:00 local, comfortably after BOTH the local and server
midnights have already passed for the day (the DST NOTE's worst case has
server midnight at 06:00 local), so local-today and server-today are the
same calendar date at that fixed run time, and local-yesterday ==
server-yesterday follows. This also skips `run_auditor.py`'s own MT5
round-trip entirely (an extra benefit, not just simpler code) -- one fewer
MT5 session competing with the live shadow loop's own connection each
morning. If the Task Scheduler trigger time is ever changed to something
close to the server-day boundary, this local-date shortcut would need
re-examining.

    python scripts/run_scheduled_daily_report.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

RUN_AUDITOR_PATH = Path(__file__).resolve().parent / "run_auditor.py"


def main() -> int:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return subprocess.call([
        sys.executable, str(RUN_AUDITOR_PATH), "daily",
        "--date", yesterday, "--mode", "paper", "--notify",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
