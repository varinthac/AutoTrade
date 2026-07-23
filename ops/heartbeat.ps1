# AutoTrade external heartbeat -- see docs/vps_deployment.md Section 6c.
#
# Pings healthchecks.io ONLY when autotrade_control.py status actually
# reports the shadow loop as RUNNING -- so the external alarm fires both if
# the VPS/scheduler itself is dead (no ping arrives at all) and if Windows
# is up but the shadow loop process itself has crashed (ping is skipped
# because status isn't RUNNING). Independent of loop_watchdog.py: this
# script doesn't rely on any AutoTrade python process being alive to send
# its own alert -- Task Scheduler invokes it directly.
#
# Deployment ops script, not part of the `autotrade` package -- hardcoded
# to the VPS's real layout, matching ops/backup_db.py's own convention.

$status = & "C:\AutoTrade\.venv\Scripts\python.exe" `
    "C:\AutoTrade\scripts\autotrade_control.py" status

if ($status -match "RUNNING") {
    Invoke-WebRequest -Uri "https://hc-ping.com/01c530a2-317f-4e6c-9dd0-179a7cbdd266" -UseBasicParsing | Out-Null
}
