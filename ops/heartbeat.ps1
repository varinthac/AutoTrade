# AutoTrade external heartbeat -- see docs/vps_deployment.md Section 6c.
#
# Pings healthchecks.io ONLY when the shadow loop is actually RUNNING -- so
# the external alarm fires both if the VPS/scheduler itself is dead (no ping
# arrives at all) and if Windows is up but the shadow loop process itself
# has crashed (ping is skipped because it isn't RUNNING).
#
# 2026-07-24: switched from `autotrade_control.py status` (read-only) to
# `run_health_check.py`, which also attempts an auto-restart when the loop
# is found down -- see that script's and common/loop_watchdog.py's own
# docstrings for why this repeating Task Scheduler trigger, not the
# console-window run_loop_watchdog.py, is now the thing responsible for
# both detecting AND recovering from the loop dying. Independent of any
# other AutoTrade python process being alive: Task Scheduler invokes this
# directly on its own schedule.
#
# Deployment ops script, not part of the `autotrade` package -- hardcoded
# to the VPS's real layout, matching ops/backup_db.py's own convention.

$status = & "C:\AutoTrade\.venv\Scripts\python.exe" `
    "C:\AutoTrade\scripts\run_health_check.py"

if ($status -match "RUNNING") {
    Invoke-WebRequest -Uri "https://hc-ping.com/01c530a2-317f-4e6c-9dd0-179a7cbdd266" -UseBasicParsing | Out-Null
}
