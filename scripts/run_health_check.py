#!/usr/bin/env python3
"""One-shot shadow-loop liveness check + auto-restart, meant to be invoked
on a short repeating interval by Task Scheduler ("AutoTrade Heartbeat",
ops/heartbeat.ps1) rather than run as its own long-lived process.

2026-07-24 incident: the loop died silently and `scripts/run_loop_watchdog.py`
-- the console-window process meant to alert on exactly that -- was ALSO
found not running, so zero alert fired either time. A process that only
ever (re)launches "at logon" cannot recover from its own silent death
mid-session. Task Scheduler's own repeating trigger doesn't have that
problem: it dispatches a fresh one-shot process every cycle, so there is no
long-lived watchdog process here that can silently die. See
common/loop_watchdog.py's module docstring for the full history.

Prints the same "AutoTrade loop: RUNNING (PID N)" / "AutoTrade loop: NOT
running" line `autotrade_control.py status` already prints, so
ops/heartbeat.ps1's existing `-match "RUNNING"` check (gating its
healthchecks.io ping) keeps working unchanged against this script instead.

    python scripts/run_health_check.py
"""
from __future__ import annotations

from autotrade.common import pid_file
from autotrade.common.loop_watchdog import check_loop_alive


def main() -> int:
    running = check_loop_alive(auto_restart=True)
    pid = pid_file.read() if running else None
    if running and pid is not None:
        print(f"AutoTrade loop: RUNNING (PID {pid})")
    else:
        print("AutoTrade loop: NOT running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
