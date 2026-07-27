"""Generic detached-background-service liveness watchdog + auto-restart --
same transition-only-alert / always-retry-while-down shape as
`common/loop_watchdog.py` (see its own module docstring for the full
2026-07-24 incident history), generalized for `scripts/run_health_check.py`
to reuse against the dashboard and Telegram control listener now that each
has its own PID file (see the same day's double-launch-guard commit).
`loop_watchdog.py` itself stays shadow-loop-specific (bespoke,
Watchman-aware alert text) -- this module is for any OTHER detached
service governed by a plain `common/pid_file.py` PID file, launched via a
`sys.executable <script> [args...]` command.

Job Object note: restart launches with `CREATE_BREAKAWAY_FROM_JOB` (falls
back without it on OSError), same reasoning as
`scripts/autotrade_control.py do_start()` -- a restart triggered from
Task Scheduler must survive regardless of what job the caller happens to
be in.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from autotrade.common import pid_file
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)


def check_and_restart(
    name: str,
    pid_path: Path,
    state_path: Path,
    script_path: Path,
    cwd: Path,
    extra_args: list[str] | None = None,
) -> bool:
    """Call this once per poll cycle for one service. Returns the
    currently-observed running state. Alerts via Telegram on a DOWN<->UP
    transition (quiet while it stays down, same as loop_watchdog.py, to
    avoid spamming every cycle a genuine problem persists). Always attempts
    a restart while down, every cycle, regardless of transition -- safe to
    retry unconditionally because the launched script's own PID-file
    double-launch guard makes a racing retry a harmless no-op.

    The whole body is wrapped in a broad `except Exception` (2026-07-28
    audit finding) so a failure checking/restarting ONE service can never
    abort whichever OTHER services `run_health_check.py` checks after it in
    the same cycle -- fails toward "not confirmed running"."""
    try:
        currently_running = pid_file.is_running(pid_path)
        previous = _load_last_state(state_path)

        if previous is None:
            if not currently_running:
                _alert_down(name)
            else:
                logger.info("%s: confirmed running at watchdog startup.", name)
        elif currently_running != previous:
            _alert_up(name) if currently_running else _alert_down(name)

        if not currently_running:
            _attempt_restart(name, script_path, cwd, extra_args or [])

        _save_last_state(state_path, currently_running)
        return currently_running
    except Exception:
        logger.exception("%s: check_and_restart raised -- leaving other health checks unaffected.", name)
        return False


def _load_last_state(state_path: Path) -> bool | None:
    if not state_path.exists():
        return None
    try:
        return bool(json.loads(state_path.read_text(encoding="utf-8"))["running"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        logger.warning("service_watchdog: state file %s is corrupt/unreadable -- treating as no prior state", state_path)
        return None


def _save_last_state(state_path: Path, running: bool) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"running": running}), encoding="utf-8")


def _alert_down(name: str) -> None:
    message = f"[AutoTrade] \U0001F6A8 {name} is DOWN -- attempting an automatic restart."
    logger.critical(message)
    notify(message)


def _alert_up(name: str) -> None:
    message = f"[AutoTrade] ✅ {name} is back up and running."
    logger.warning(message)
    notify(message)


def _attempt_restart(name: str, script_path: Path, cwd: Path, extra_args: list[str]) -> None:
    """Fire-and-forget, like autotrade_control.py's own do_start() -- the
    launched process (Flask app.run() / a `while True` poll loop) runs
    forever by design, so this must never block waiting for it to exit
    (subprocess.run() would hang until the NEXT restart-triggering crash)."""
    args = [sys.executable, str(script_path), *extra_args]
    try:
        subprocess.Popen(
            args, cwd=str(cwd),
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_BREAKAWAY_FROM_JOB,
        )
        logger.warning("%s: auto-restart launched.", name)
    except OSError:
        logger.warning("%s: restart with CREATE_BREAKAWAY_FROM_JOB failed -- retrying without it.", name)
        try:
            subprocess.Popen(args, cwd=str(cwd), creationflags=subprocess.CREATE_NEW_CONSOLE)
            logger.warning("%s: auto-restart launched (without job breakaway).", name)
        except Exception:
            logger.exception("%s: auto-restart attempt raised an exception.", name)
