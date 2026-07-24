#!/usr/bin/env python3
"""One-shot liveness check + auto-restart for every detached background
service (shadow loop, dashboard, Telegram control listener), meant to be
invoked on a short repeating interval by Task Scheduler ("AutoTrade
Heartbeat", ops/heartbeat.ps1) rather than run as its own long-lived
process.

2026-07-24 incident: the loop died silently and `scripts/run_loop_watchdog.py`
-- the console-window process meant to alert on exactly that -- was ALSO
found not running, so zero alert fired either time. A process that only
ever (re)launches "at logon" cannot recover from its own silent death
mid-session. Task Scheduler's own repeating trigger doesn't have that
problem: it dispatches a fresh one-shot process every cycle, so there is no
long-lived watchdog process here that can silently die. See
common/loop_watchdog.py's module docstring for the shadow loop's own fuller
history; dashboard/telegram control reuse the generic
common/service_watchdog.py added the same day once each gained its own PID
file (double-launch-guard commit) -- until then there was no way to detect
or recover either of them going down (the shadow loop's own /start command
can't help: it only ever launches the loop itself, and if the Telegram
listener is the thing that's down, there's no way to reach it via Telegram
in the first place).

Prints the same "AutoTrade loop: RUNNING (PID N)" / "AutoTrade loop: NOT
running" line `autotrade_control.py status` already prints, so
ops/heartbeat.ps1's existing `-match "RUNNING"` check (gating its
healthchecks.io ping) keeps working unchanged against this script instead.

    python scripts/run_health_check.py
"""
from __future__ import annotations

from autotrade.common import pid_file
from autotrade.common.config import REPO_ROOT
from autotrade.common.loop_watchdog import check_loop_alive
from autotrade.common.service_watchdog import check_and_restart

_DASHBOARD_SCRIPT = REPO_ROOT / "scripts" / "run_dashboard.py"
_DASHBOARD_PID_PATH = REPO_ROOT / "data" / "db" / "dashboard.pid"
_DASHBOARD_STATE_PATH = REPO_ROOT / "data" / "db" / "dashboard_watchdog_state.json"

_TELEGRAM_CONTROL_SCRIPT = REPO_ROOT / "scripts" / "run_telegram_control.py"
_TELEGRAM_CONTROL_PID_PATH = REPO_ROOT / "data" / "db" / "telegram_control.pid"
_TELEGRAM_CONTROL_STATE_PATH = REPO_ROOT / "data" / "db" / "telegram_control_watchdog_state.json"


def main() -> int:
    running = check_loop_alive(auto_restart=True)
    pid = pid_file.read() if running else None

    check_and_restart(
        "Dashboard", _DASHBOARD_PID_PATH, _DASHBOARD_STATE_PATH, _DASHBOARD_SCRIPT, REPO_ROOT,
    )
    check_and_restart(
        "Telegram control listener", _TELEGRAM_CONTROL_PID_PATH, _TELEGRAM_CONTROL_STATE_PATH,
        _TELEGRAM_CONTROL_SCRIPT, REPO_ROOT,
    )

    if running and pid is not None:
        print(f"AutoTrade loop: RUNNING (PID {pid})")
    else:
        print("AutoTrade loop: NOT running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
