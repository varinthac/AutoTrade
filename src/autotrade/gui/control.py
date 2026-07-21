"""Status/action wrappers for `scripts/autotrade_gui.py`'s Control tab --
pure logic, no tkinter import. Reads the same three flag/file primitives
`scripts/autotrade_control.py`'s `do_status()` reads (never shells out or
parses CLI stdout for status), and drives start/stop/emergency-stop by
invoking `autotrade_control.py` itself as a subprocess -- same pattern
`AutoTrade_Start.bat`/`AutoTrade_Stop.bat`/`AutoTrade_EmergencyStop.bat`
already use, just from Python instead of a .bat file.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from autotrade.common import kill_switch_flag, pid_file, stop_request_flag
from autotrade.common.config import REPO_ROOT

CONTROL_SCRIPT: Path = REPO_ROOT / "scripts" / "autotrade_control.py"


@dataclass(frozen=True)
class StatusReport:
    loop_running: bool
    loop_pid: int | None
    kill_switch_active: bool
    kill_switch_reason: str | None
    stop_pending: bool
    stop_pending_reason: str | None


def build_status() -> StatusReport:
    pid = pid_file.read()
    loop_running = pid is not None and pid_file.is_pid_running(pid)

    kill_status = kill_switch_flag.get_status()
    stop_status = stop_request_flag.get_status()

    return StatusReport(
        loop_running=loop_running,
        loop_pid=pid if loop_running else None,
        kill_switch_active=kill_status is not None,
        kill_switch_reason=kill_status.get("reason") if kill_status is not None else None,
        stop_pending=stop_status is not None,
        stop_pending_reason=stop_status.get("reason") if stop_status is not None else None,
    )


def format_status(report: StatusReport) -> str:
    lines = []

    if report.loop_running:
        lines.append(f"AutoTrade loop: RUNNING (PID {report.loop_pid})")
    else:
        lines.append("AutoTrade loop: NOT running")

    if report.kill_switch_active:
        lines.append(f"Kill switch: ACTIVE (reason={report.kill_switch_reason})")
    else:
        lines.append("Kill switch: not active")

    if report.stop_pending:
        lines.append(f"Stop request flag: PENDING (reason={report.stop_pending_reason})")
    else:
        lines.append("Stop request flag: not pending")

    return "\n".join(lines)


def can_start(report: StatusReport) -> bool:
    return not report.kill_switch_active and not report.loop_running


def start_bot() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONTROL_SCRIPT), "start"],
        capture_output=True, text=True, check=False,
    )


def stop_bot() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONTROL_SCRIPT), "stop"],
        capture_output=True, text=True, check=False,
    )


def emergency_stop_bot() -> subprocess.CompletedProcess:
    """Always passes --confirm -- the GUI's own confirm dialog (built in
    scripts/autotrade_gui.py) is the human-facing gate that must happen
    BEFORE this is ever called, same as AutoTrade_EmergencyStop.bat's
    `choice /C YN` dialog happening before it invokes --confirm on the CLI
    side."""
    return subprocess.run(
        [sys.executable, str(CONTROL_SCRIPT), "emergency-stop", "--confirm"],
        capture_output=True, text=True, check=False,
    )
