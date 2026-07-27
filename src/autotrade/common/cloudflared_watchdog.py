"""Liveness watchdog + auto-restart for the `cloudflared` tunnel process
that exposes the dashboard at https://trade.kylerlink.com
(`ops/cloudflared_tunnel.ps1`).

**2026-07-28 incident.** The "AutoTrade Cloudflared" Scheduled Task
(`ops/cloudflared_tunnel.ps1`'s deployed form -- an "At log on" trigger, not
a Windows Service despite that script's own comments) ran once at logon on
2026-07-24 and then died at some point with nothing watching it -- exactly
the same "only ever (re)launches at logon, can't recover from its own
silent death mid-session" failure mode `common/loop_watchdog.py`'s module
docstring already documents for the shadow loop, just for a different
process. The dashboard itself (Flask, `scripts/run_dashboard.py`) was
running fine the whole time; only the tunnel connecting Cloudflare's edge to
it was down, surfacing to the user as Cloudflare error 1033.

Same transition-only-alert / always-retry-while-down shape as
`common/service_watchdog.py`, but adapted for a process with no PID file of
its own: liveness is checked by process image name (`tasklist`, matching
`common/pid_file.py`'s own subprocess-over-psutil convention) rather than a
PID file, and recovery re-triggers the existing Scheduled Task
(`schtasks /Run`) rather than a fresh `subprocess.Popen` -- reusing the
task's own already-configured run-as-user/working-directory/log-on-session
context rather than trying to replicate it here.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "cloudflared_watchdog_state.json"
_PROCESS_IMAGE_NAME = "cloudflared.exe"
_TASK_NAME = "AutoTrade Cloudflared"
_SUBPROCESS_TIMEOUT_SEC = 30  # 2026-07-28 code review finding, matches loop_watchdog's own restart timeout


def is_running() -> bool:
    """True if `tasklist` reports a running `cloudflared.exe` process."""
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {_PROCESS_IMAGE_NAME}", "/NH"],
        capture_output=True, text=True, check=False, timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    return _PROCESS_IMAGE_NAME.lower() in result.stdout.lower()


def check_and_restart(state_path: Path | None = None) -> bool:
    """Call this once per heartbeat cycle. Returns the currently-observed
    running state. Alerts via Telegram on a DOWN<->UP transition (quiet
    while it stays down); always attempts a restart while down, every
    cycle, regardless of transition -- `schtasks /Run` on an already-running
    task is a harmless no-op, so a racing retry cannot double-launch.

    The whole body is wrapped in a broad `except Exception` (2026-07-28
    audit finding) so a failure here can never abort whichever OTHER checks
    `run_health_check.py` runs after it in the same cycle -- fails toward
    "not confirmed running"."""
    try:
        state_path = state_path or DEFAULT_STATE_PATH
        currently_running = is_running()
        previous = _load_last_state(state_path)

        if previous is None:
            if not currently_running:
                _alert_down()
            else:
                logger.info("cloudflared_watchdog: confirmed running at watchdog startup.")
        elif currently_running != previous:
            _alert_up() if currently_running else _alert_down()

        if not currently_running:
            _attempt_restart()

        _save_last_state(state_path, currently_running)
        return currently_running
    except Exception:
        logger.exception("cloudflared_watchdog: check_and_restart raised -- leaving other health checks unaffected.")
        return False


def _load_last_state(state_path: Path) -> bool | None:
    if not state_path.exists():
        return None
    try:
        return bool(json.loads(state_path.read_text(encoding="utf-8"))["running"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        logger.warning(
            "cloudflared_watchdog: state file %s is corrupt/unreadable -- treating as no prior state", state_path,
        )
        return None


def _save_last_state(state_path: Path, running: bool) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"running": running}), encoding="utf-8")


def _alert_down() -> None:
    message = (
        f"[AutoTrade] \U0001F6A8 The cloudflared tunnel is DOWN -- the dashboard at "
        "trade.kylerlink.com is unreachable (Cloudflare error 1033) even though the dashboard "
        "itself may be fine. Attempting an automatic restart."
    )
    logger.critical(message)
    notify(message)


def _alert_up() -> None:
    message = "[AutoTrade] ✅ The cloudflared tunnel is back up -- trade.kylerlink.com should be reachable again."
    logger.warning(message)
    notify(message)


def _attempt_restart() -> None:
    try:
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", _TASK_NAME],
            capture_output=True, text=True, check=False, timeout=_SUBPROCESS_TIMEOUT_SEC,
        )
        if result.returncode == 0:
            logger.warning("cloudflared_watchdog: auto-restart triggered via Scheduled Task '%s'.", _TASK_NAME)
        else:
            logger.error(
                "cloudflared_watchdog: 'schtasks /Run /TN %s' exited %d: %s",
                _TASK_NAME, result.returncode, result.stderr.strip(),
            )
    except Exception:
        logger.exception("cloudflared_watchdog: auto-restart attempt raised an exception.")
