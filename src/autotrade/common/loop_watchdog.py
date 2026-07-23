"""External shadow-loop process-liveness watchdog.

**Real incident (2026-07-23):** the live shadow loop (`scripts/run_shadow_loop.py`)
died silently -- no traceback, no clean shutdown message, its last log line
just stops -- and sat dead for roughly two hours with zero alert. Nothing in
this codebase previously monitored the loop's own OS process from the
outside: `watchman/autotrading_watchdog.py` and `watchman/connectivity_watchdog.py`
both run *inside* `run_shadow_loop.py`'s own process, so neither can fire
once that process itself is gone -- a dead watcher cannot watch itself die.

This module is deliberately small and separate: it does nothing but read
`common/pid_file.py`'s PID-file liveness (the same check
`scripts/autotrade_control.py status` already uses) and alert on a
True<->False transition, mirroring `autotrading_watchdog.py`'s
transition-only alerting shape. It is meant to be polled from a SEPARATE,
minimal, long-running process (`scripts/run_loop_watchdog.py`) -- not
imported into `run_shadow_loop.py` itself, since the whole point is
detecting that process's absence.

Last-known state is persisted to a small JSON file (`gate_state.py`'s
whole-file read/write idiom), not kept in memory -- unlike
`AutoTradingWatchdog`, this watchdog cannot assume one long-lived Python
object survives across every check (a fresh CLI invocation is just as valid
a caller shape as a polling loop), so the "have we already alerted for this
outage" memory must survive a restart of the watchdog itself.

**Known, honest limitation, not hidden:** this only alerts on the shadow
loop's process disappearing -- it does not detect the loop being alive but
HUNG (process present, doing nothing). Detecting that would need a
heartbeat/log-freshness check, a reasonable future addition, not done here
under time pressure the night this was written. It also does not persist an
anomaly event to the trade journal (unlike `AutoTradingWatchdog`) -- doing
so would require assuming which `--mode` (paper/live) journal DB the loop
is using, an assumption this external, loop-independent watchdog has no
reliable way to make; the Telegram alert alone is the goal here. And,
unavoidably: nothing watches the watchdog itself -- it is deliberately as
small and dependency-free as possible (just a sleep loop, a `tasklist`
subprocess call, and a Telegram POST) specifically so it is far less likely
to crash than the MT5-connected trading loop it monitors, but this is a
mitigation, not a guarantee.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from autotrade.common import pid_file
from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "loop_watchdog_state.json"

_DOWN_MESSAGE = (
    "[AutoTrade] \U0001F6A8 The shadow loop process is DOWN -- it stopped running "
    "(no PID file / stale PID). No new signals are being evaluated and Watchman is not "
    "managing any open position until it's restarted. Existing open positions remain "
    "protected by their own broker-side stop-loss. Restart with "
    "'python scripts/autotrade_control.py start' or AutoTrade_Start.bat."
)
_UP_MESSAGE = "[AutoTrade] ✅ The shadow loop process is back up and running."


def _load_last_state(state_path: Path) -> bool | None:
    """The last-known running state, or `None` if this is the first check
    ever (no state file, or an unreadable one -- treated the same as "no
    prior state" so a corrupt file can't wedge this watchdog silent)."""
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return bool(data["running"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        logger.warning("loop_watchdog: state file %s is corrupt/unreadable -- treating as no prior state", state_path)
        return None


def _save_last_state(state_path: Path, running: bool) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"running": running}), encoding="utf-8")


def check_loop_alive(pid_path: Path | None = None, state_path: Path | None = None) -> bool:
    """Call this once per poll cycle. Returns the currently-observed running
    state. Notifies via Telegram on the first check if already down (mirrors
    `AutoTradingWatchdog.check`'s "alert on first check if already bad"
    convention -- there is no safe baseline to silently assume), and on
    every subsequent True<->False transition. Two consecutive "down" checks
    (the loop stays down) do NOT re-notify -- this is what the persisted
    last-known state exists to prevent."""
    state_path = state_path or DEFAULT_STATE_PATH
    currently_running = pid_file.is_running(pid_path)
    previous = _load_last_state(state_path)

    if previous is None:
        if not currently_running:
            _alert_down()
        else:
            logger.info("loop_watchdog: shadow loop confirmed running at watchdog startup.")
    elif currently_running != previous:
        if currently_running:
            _alert_up()
        else:
            _alert_down()

    _save_last_state(state_path, currently_running)
    return currently_running


def _alert_down() -> None:
    logger.critical(_DOWN_MESSAGE)
    notify(_DOWN_MESSAGE)


def _alert_up() -> None:
    logger.warning(_UP_MESSAGE)
    notify(_UP_MESSAGE)
