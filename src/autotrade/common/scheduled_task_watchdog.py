"""Liveness/success watchdog for Scheduled Tasks that run detached from
`run_health_check.py`'s own PID-file-based checks: the daily Auditor report
and the nightly DB backup (`docs/vps_deployment.md` sections 6b/8).

**2026-07-28 audit finding.** Both tasks report success via `LastTaskResult`
in Task Scheduler alone -- nothing in this codebase ever polled that. A
silent failure (permissions change, disk full, an unhandled exception in
either script) would look identical to "everything's fine" from the
operator's side: the daily report simply not arriving one day is easy to
mistake for "no news today", and `ops/backup_db.py` itself has zero
`notify()` of its own on failure -- it just prints and returns a nonzero
exit code that nothing reads.

**Alert-only, no auto-restart attempt** -- unlike this package's other
watchdogs. Blindly re-running a partially-failed daily report or DB backup
isn't obviously safe (e.g. a backup that failed mid-write), and both tasks
already get another shot at their next scheduled occurrence; the operator
needs to know it failed and investigate/re-run manually, not have this
silently paper over a real failure by retriggering it.

Checked via `schtasks /Query /TN "<name>" /FO LIST /V` (plain-text, parsed
here) rather than PowerShell's `Get-ScheduledTaskInfo` cmdlet -- matches
this package's existing subprocess-over-invoking-a-shell convention
(`common/pid_file.py`'s own `tasklist` usage, `cloudflared_watchdog.py`'s
own `schtasks /Run`)."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "scheduled_task_watchdog_state.json"

# task name -> max age of "Last Run Time" before considered stale. Both are
# daily tasks -- a generous multi-hour buffer past 24h absorbs clock
# drift/slow start without letting a genuinely-stopped task go unnoticed
# for days.
_MONITORED_TASKS: dict[str, timedelta] = {
    "AutoTrade Daily Report": timedelta(hours=27),
    "AutoTrade DB Backup": timedelta(hours=27),
}

_DATETIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S")
_SCHTASKS_QUERY_TIMEOUT_SEC = 30
# 0x41301 -- Task Scheduler's "the task is currently running" STATUS code,
# surfaced through the Last Result field while an instance is mid-run.
_SCHED_S_TASK_RUNNING = 267009


def check_all(state_path: Path | None = None) -> dict[str, bool]:
    """Call this once per heartbeat cycle. Returns {task_name: healthy} for
    every monitored task. Never raises -- a failure checking ONE task must
    never prevent checking (or alerting on) the others."""
    resolved_state_path = state_path or DEFAULT_STATE_PATH
    return {name: _check_one(name, max_age, resolved_state_path) for name, max_age in _MONITORED_TASKS.items()}


def _check_one(task_name: str, max_age: timedelta, state_path: Path) -> bool:
    try:
        healthy, detail = _query_and_evaluate(task_name, max_age)
        previous = _load_last_state(state_path, task_name)

        if previous is None:
            if not healthy:
                _alert_unhealthy(task_name, detail)
        elif healthy != previous:
            _alert_healthy(task_name) if healthy else _alert_unhealthy(task_name, detail)

        _save_last_state(state_path, task_name, healthy)
        return healthy
    except Exception:
        logger.exception(
            "scheduled_task_watchdog: check for %r raised -- leaving other checks unaffected.", task_name,
        )
        return False


def _query_and_evaluate(task_name: str, max_age: timedelta) -> tuple[bool, str]:
    # 2026-07-28 code review finding: a timeout, matching
    # loop_watchdog._attempt_restart's own subprocess.run(timeout=...) --
    # without one, a hung schtasks query would hang the whole
    # run_health_check.py cycle (the caller's own try/except in
    # _check_one still catches a resulting TimeoutExpired and reports this
    # task unhealthy, same as any other query failure).
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
        capture_output=True, text=True, check=False, timeout=_SCHTASKS_QUERY_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        return False, f"schtasks query failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()}"

    fields = _parse_fields(result.stdout)
    last_result_raw = fields.get("Last Result")
    last_run_raw = fields.get("Last Run Time")

    if last_result_raw is None or last_run_raw is None:
        return False, "could not find 'Last Result' / 'Last Run Time' in schtasks output"

    try:
        last_result = int(last_result_raw)
    except ValueError:
        return False, f"non-numeric Last Result {last_result_raw!r}"

    if last_result == _SCHED_S_TASK_RUNNING:
        # 2026-08-05 false-alarm fix: while a task instance is EXECUTING,
        # Windows reports Last Result = 0x41301 (267009, SCHED_S_TASK_RUNNING)
        # -- a status code, not a failure. The heartbeat fired at 09:00:0x,
        # squarely inside the Daily Report's own run, and alerted on it;
        # seconds later the task finished with 0. In-progress is healthy:
        # the task demonstrably fired (that is what this watchdog exists to
        # verify), and a HUNG run is still covered elsewhere -- every
        # watched task now carries its own ExecutionTimeLimit (runbook 6a),
        # after which Task Scheduler kills it and Last Result becomes a
        # genuine non-zero code this check flags on the next cycle.
        return True, "ok (task currently running)"
    if last_result != 0:
        return False, f"Last Result={last_result} (non-zero -- the task's own last run failed)"

    last_run = _parse_datetime(last_run_raw)
    if last_run is None:
        return False, f"could not parse Last Run Time {last_run_raw!r} (unexpected date format/locale on this VPS)"

    age = datetime.now() - last_run
    if age > max_age:
        return False, f"Last Run Time was {age} ago (> {max_age} threshold) -- task may have stopped firing"

    return True, "ok"


def _parse_fields(schtasks_output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in schtasks_output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_datetime(raw: str) -> datetime | None:
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _load_last_state(state_path: Path, task_name: str) -> bool | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data.get(task_name)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "scheduled_task_watchdog: state file %s is corrupt/unreadable -- treating as no prior state",
            state_path,
        )
        return None


def _save_last_state(state_path: Path, task_name: str, healthy: bool) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[task_name] = healthy
    state_path.write_text(json.dumps(data), encoding="utf-8")


def _alert_unhealthy(task_name: str, detail: str) -> None:
    message = (
        f"[AutoTrade] \U0001F6A8 Scheduled Task '{task_name}' looks unhealthy: {detail}. This task does "
        f"NOT auto-restart -- check it manually (schtasks /Query /TN \"{task_name}\" /V) and re-run it "
        "if needed."
    )
    logger.critical(message)
    notify(message)


def _alert_healthy(task_name: str) -> None:
    message = f"[AutoTrade] ✅ Scheduled Task '{task_name}' is healthy again."
    logger.warning(message)
    notify(message)
