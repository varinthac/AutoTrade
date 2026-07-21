#!/usr/bin/env python3
"""Day-to-day start/stop workflow for the live shadow loop -- the thin CLI
`AutoTrade_Start.bat`/`AutoTrade_Stop.bat`/`AutoTrade_EmergencyStop.bat`
(repo root) each call.

Two independent stop mechanisms, deliberately not one (per the operational
design this implements):

  - `stop` -- a graceful, non-destructive stop. Sets
    `common/stop_request_flag.py`'s cooperative flag; the running
    `scripts/run_shadow_loop.py` loop (if any) notices it on its next poll
    cycle, clears it, and exits on its own. Any open positions are left
    exactly as they are (broker-side SL/TP still active, but Watchman no
    longer manages them until restarted).
  - `emergency-stop` -- destructive. Reuses `scripts/kill_switch.py`
    unchanged (halts AND closes every open position at market) rather than
    duplicating its logic; requires `--confirm`, same safety pattern
    `kill_switch.py` itself uses for its own destructive actions.

    python scripts/autotrade_control.py start
    python scripts/autotrade_control.py stop
    python scripts/autotrade_control.py emergency-stop --confirm
    python scripts/autotrade_control.py status
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from autotrade.common import kill_switch_flag, pid_file, stop_request_flag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent
RUN_SHADOW_LOOP_PATH = SCRIPTS_DIR / "run_shadow_loop.py"
KILL_SWITCH_PATH = SCRIPTS_DIR / "kill_switch.py"


def do_start() -> int:
    """Refuses if the kill switch is active -- never auto-deactivates it
    (that's always an explicit, separate `kill_switch.py --deactivate
    --confirm` action, per that script's own "never lift a halt
    accidentally" philosophy). Otherwise launches run_shadow_loop.py
    --adapter demo detached, in its own new console window, and returns
    immediately -- the actual "started successfully" confirmation comes via
    Telegram from inside run_shadow_loop.py itself once MT5 actually
    connects, not from this command."""
    status = kill_switch_flag.get_status()
    if status is not None:
        logger.error(
            "Refusing to start: kill switch is ACTIVE (reason=%s). Run "
            "'python scripts/kill_switch.py --deactivate --confirm' first if you intend to resume.",
            status.get("reason"),
        )
        return 1

    logger.info("Launching scripts/run_shadow_loop.py --adapter demo in a new console window...")
    subprocess.Popen(
        [sys.executable, str(RUN_SHADOW_LOOP_PATH), "--adapter", "demo"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    logger.info(
        "Launched. Watch the new console window for live output -- a Telegram confirmation will "
        "follow once MT5 actually connects."
    )
    return 0


def do_stop() -> int:
    """Non-blocking -- requests a graceful stop and returns immediately,
    matching the file-flag pattern (no waiting/polling to confirm the loop
    actually exited). Warns (but still requests -- harmless either way,
    see stop_request_flag's own stale-flag-at-startup handling) if the PID
    file shows no loop is currently running, so a stray double-click doesn't
    silently look like it did something."""
    if not pid_file.is_running():
        logger.warning(
            "Stop requested, but no AutoTrade loop appears to be running right now -- requesting "
            "anyway (harmless: a stale flag is cleared automatically the next time a loop starts)."
        )

    stop_request_flag.request("manual stop button")
    logger.info(
        "Graceful stop requested. The running shadow loop (if any) will exit within roughly one "
        "poll interval and send its own Telegram confirmation. Open positions, if any, are left "
        "untouched -- use emergency-stop if you need them closed."
    )
    return 0


def do_emergency_stop(confirm: bool) -> int:
    """Requires --confirm (mirrors kill_switch.py's own safety pattern for
    destructive actions) since this closes every open position at market.
    Reuses kill_switch.py's own --activate logic via subprocess rather than
    duplicating it."""
    if not confirm:
        logger.error(
            "--confirm is required for emergency-stop -- this halts trading AND closes every open "
            "position at market."
        )
        return 1

    logger.warning("Invoking scripts/kill_switch.py --activate for the manual emergency stop button...")
    result = subprocess.run([sys.executable, str(KILL_SWITCH_PATH), "--activate", "manual emergency stop button"])
    return result.returncode


def do_status() -> int:
    pid = pid_file.read()
    if pid is not None and pid_file.is_pid_running(pid):
        print(f"AutoTrade loop: RUNNING (PID {pid})")
    else:
        print("AutoTrade loop: NOT running")

    status = kill_switch_flag.get_status()
    if status is not None:
        print(f"Kill switch: ACTIVE (reason={status.get('reason')})")
    else:
        print("Kill switch: not active")

    stop_status = stop_request_flag.get_status()
    if stop_status is not None:
        print(
            f"Stop request flag: PENDING (reason={stop_status.get('reason')}) -- normally this "
            "should be absent; a lingering flag may mean the loop isn't actually running to "
            "consume it."
        )
    else:
        print("Stop request flag: not pending")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start", help="Launch the live shadow loop (--adapter demo) in a new console window")
    subparsers.add_parser("stop", help="Request a graceful stop of the running shadow loop (non-blocking)")

    emergency_parser = subparsers.add_parser(
        "emergency-stop", help="Halt trading AND close every open position at market (via kill_switch.py)",
    )
    emergency_parser.add_argument("--confirm", action="store_true", help="Required to actually execute")

    subparsers.add_parser("status", help="Report loop-running / kill-switch / stop-flag state")

    args = parser.parse_args()

    if args.command == "start":
        return do_start()
    if args.command == "stop":
        return do_stop()
    if args.command == "emergency-stop":
        return do_emergency_stop(args.confirm)
    return do_status()


if __name__ == "__main__":
    sys.exit(main())
