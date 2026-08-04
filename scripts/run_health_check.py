#!/usr/bin/env python3
"""One-shot liveness check + auto-restart for every detached background
service (shadow loop, Telegram control listener, the cloudflared tunnel)
plus the calendar-export self-heal check, meant to be invoked on a short
repeating interval by Task Scheduler ("AutoTrade Heartbeat",
ops/heartbeat.ps1) rather than run as its own long-lived process.

**2026-08-04 (lean-plan P1, docs/vps_lean_plan.md): the dashboard is
deliberately NOT checked here anymore, and must never be added back.** The
dashboard used to be always-on and auto-restarted like every other service
below; it is now on-demand (launched via Telegram's `/dashboard` command,
see `notify/telegram_control.py`, and self-terminating after an idle TTL,
see `scripts/run_dashboard.py`). If a `check_and_restart("Dashboard", ...)`
call is ever restored here, this repeating heartbeat will resurrect the
dashboard within one cycle (as fast as every few minutes) and silently
undo the entire point of making it on-demand -- the exact same trap the
`manual_halt_flag` work already hit once (see finding D-3/P1 in
docs/vps_lean_plan.md).

**2026-07-28: two silent-failure incidents in one day**, followed by a
broader audit, prompted every addition below the original three checks.
(1) `NewsCalendarExporter` (an MQL5 Service inside the MT5 terminal, not
one of this script's own PID-tracked processes) died silently and stayed
dead for days, fail-safe-vetoing every USD signal with nothing but an
unwatched log line + one Telegram alert that never triggered any actual
recovery -- see `common/calendar_export_watchdog.py`'s module docstring for
the full incident and recovery design. (2) The cloudflared tunnel
(Scheduled Task, "At log on" trigger only) died with nothing restarting it,
surfacing to the user as Cloudflare error 1033 even though the dashboard
behind it was fine the whole time -- see `common/cloudflared_watchdog.py`.
(3) The follow-up audit found: a deliberate `stop` getting silently
auto-resurrected by this very heartbeat within ~10 minutes (fixed at the
source in `common/loop_watchdog.py` + `common/manual_halt_flag.py`, not
here); the kill switch never reminding anyone it's still active
(`common/kill_switch_reminder.py`); and the daily report / DB backup
Scheduled Tasks having zero monitoring of their own
(`common/scheduled_task_watchdog.py`). (4) Same-day code review of that
audit's own fixes found `manual_halt_flag` had reintroduced the exact
"forgotten halt, zero further signal" problem it was built to prevent
(deliberately staying quiet forever, with no reminder counterpart the way
the kill switch got one) -- `common/manual_halt_reminder.py`.

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
from autotrade.common.calendar_export_watchdog import check_and_recover as check_calendar_export
from autotrade.common.calendar_export_watchdog import default_export_path
from autotrade.council.calendar_archive import archive_export_file
from autotrade.common.cloudflared_watchdog import check_and_restart as check_cloudflared
from autotrade.common.config import REPO_ROOT
from autotrade.common.kill_switch_reminder import check_and_remind as check_kill_switch_reminder
from autotrade.common.loop_watchdog import check_loop_alive
from autotrade.common.manual_halt_reminder import check_and_remind as check_manual_halt_reminder
from autotrade.common.scheduled_task_watchdog import check_all as check_scheduled_tasks
from autotrade.common.service_watchdog import check_and_restart

_TELEGRAM_CONTROL_SCRIPT = REPO_ROOT / "scripts" / "run_telegram_control.py"
_TELEGRAM_CONTROL_PID_PATH = REPO_ROOT / "data" / "db" / "telegram_control.pid"
_TELEGRAM_CONTROL_STATE_PATH = REPO_ROOT / "data" / "db" / "telegram_control_watchdog_state.json"


def main() -> int:
    # Runs BEFORE check_loop_alive: if the calendar export is stale enough to
    # force a stop this cycle, check_loop_alive's own auto_restart picks the
    # now-down loop straight back up in the same cycle -- see
    # calendar_export_watchdog's module docstring for why recovery is split
    # this way instead of duplicating the relaunch logic here.
    check_calendar_export()

    running = check_loop_alive(auto_restart=True)
    pid = pid_file.read() if running else None

    check_and_restart(
        "Telegram control listener", _TELEGRAM_CONTROL_PID_PATH, _TELEGRAM_CONTROL_STATE_PATH,
        _TELEGRAM_CONTROL_SCRIPT, REPO_ROOT,
    )
    check_cloudflared()
    check_kill_switch_reminder()
    check_manual_halt_reminder()
    check_scheduled_tasks()

    # Passive observation, never recovery: fold the current calendar-export
    # snapshot into the append-only history archive (EXP-023's prerequisite
    # for ever backtesting news protection -- see council/calendar_archive.py).
    # Missing/stale export is check_calendar_export()'s problem, not this
    # call's; it just archives whatever is readable and stays quiet otherwise.
    archive_export_file(default_export_path())

    if running and pid is not None:
        print(f"AutoTrade loop: RUNNING (PID {pid})")
    else:
        print("AutoTrade loop: NOT running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
