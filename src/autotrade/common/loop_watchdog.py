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

**2026-07-24 follow-up incident:** the loop died again (~13:00 server time)
and this time even `scripts/run_loop_watchdog.py` itself -- the one thing
meant to alert on exactly this -- was found not running, so zero alert
fired either time. A console-window process that only ever launches "at
logon" (Task Scheduler) cannot recover from its own silent death mid-session.
`scripts/run_health_check.py` exists to route this check through a
mechanism that already reliably survives that failure mode: a Task
Scheduler task with its own *repeating* trigger ("AutoTrade Heartbeat",
`ops/heartbeat.ps1`, every 10 minutes). Task Scheduler dispatches a fresh
one-shot process every cycle -- there is no long-lived watchdog process left
to silently die. `auto_restart=True`
(only `run_health_check.py` passes this; `run_loop_watchdog.py` still
doesn't, to keep its own behavior unchanged) additionally attempts to
relaunch the loop on every check while it's down -- safe to retry every
cycle unchanged because `run_shadow_loop.py`'s own PID-file double-launch
guard (see its `main()`) already refuses a second instance, so a retry
racing an in-progress startup just harmlessly no-ops rather than double
running. Deliberately NOT re-notified on every failed retry (same
transition-only philosophy as the alerts above) -- the down alert already
told the operator restart is being attempted/needed; if the underlying cause
(e.g. an active kill switch) prevents it from ever coming back, that's
surfaced by the *absence* of the "back up" message, not fresh spam.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from autotrade.common import manual_halt_flag, pid_file
from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "loop_watchdog_state.json"
_AUTOTRADE_CONTROL_PATH = REPO_ROOT / "scripts" / "autotrade_control.py"
_RESTART_TIMEOUT_SEC = 30

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


def check_loop_alive(
    pid_path: Path | None = None, state_path: Path | None = None, auto_restart: bool = False,
) -> bool:
    """Call this once per poll cycle. Returns the currently-observed running
    state. Notifies via Telegram on the first check if already down (mirrors
    `AutoTradingWatchdog.check`'s "alert on first check if already bad"
    convention -- there is no safe baseline to silently assume), and on
    every subsequent True<->False transition. Two consecutive "down" checks
    (the loop stays down) do NOT re-notify -- this is what the persisted
    last-known state exists to prevent.

    auto_restart=True additionally attempts to relaunch the loop (see
    _attempt_restart) every single time it's observed down, transition or
    not -- deliberately NOT gated on the same transition-only logic as the
    Telegram alerts above, so a restart that fails to actually bring MT5
    back up (e.g. broker/network hiccup) keeps getting retried on the next
    cycle rather than being attempted once and given up on.

    The whole body is wrapped in a broad `except Exception` (2026-07-28
    audit finding) so an unexpected failure here (e.g. `tasklist` itself
    misbehaving) can never abort every check that `run_health_check.py`
    runs AFTER this one in the same cycle -- fails toward "not confirmed
    running", same fail-safe-toward-assuming-trouble convention used
    elsewhere in this codebase (e.g. kill_switch_flag.get_status).

    **2026-07-28 audit finding: respects `manual_halt_flag`.** While it's
    active (set by `scripts/autotrade_control.py stop`, cleared by `start`
    -- see `manual_halt_flag.py`'s own docstring for the full incident),
    neither the DOWN alert nor the auto-restart attempt fire for a
    not-running loop -- an operator-requested stop already gets its own
    unambiguous Telegram confirmation from inside the loop itself as it
    exits, and this watchdog resurrecting it minutes later would silently
    invert that operator's intent."""
    try:
        state_path = state_path or DEFAULT_STATE_PATH
        currently_running = pid_file.is_running(pid_path)
        previous = _load_last_state(state_path)

        if not currently_running and manual_halt_flag.is_active():
            logger.info(
                "loop_watchdog: shadow loop is down, but manual_halt_flag is active (operator-requested "
                "stop) -- not alerting or auto-restarting. Run 'autotrade_control.py start' to resume."
            )
        else:
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

            if not currently_running and auto_restart:
                _attempt_restart()

        _save_last_state(state_path, currently_running)
        return currently_running
    except Exception:
        logger.exception("loop_watchdog: check_loop_alive raised -- leaving other health checks unaffected.")
        return False


def _alert_down() -> None:
    logger.critical(_DOWN_MESSAGE)
    notify(_DOWN_MESSAGE)


def _alert_up() -> None:
    logger.warning(_UP_MESSAGE)
    notify(_UP_MESSAGE)


def _attempt_restart() -> None:
    """Best-effort relaunch via the same `autotrade_control.py start` a
    human would run -- reused rather than duplicated so this stays subject
    to that command's own safety checks (refuses if the kill switch is
    active) unchanged. Deliberately not notified here on failure (see
    check_loop_alive's docstring) -- logged only; the operator already got
    the down alert, and the "back up" alert (or its absence) is the honest
    signal of whether this is working."""
    try:
        result = subprocess.run(
            [sys.executable, str(_AUTOTRADE_CONTROL_PATH), "start"],
            capture_output=True, text=True, timeout=_RESTART_TIMEOUT_SEC,
        )
        if result.returncode == 0:
            logger.warning("loop_watchdog: auto-restart launched (scripts/autotrade_control.py start).")
        else:
            logger.error(
                "loop_watchdog: auto-restart command exited %d: %s", result.returncode, result.stderr.strip(),
            )
    except Exception:
        logger.exception("loop_watchdog: auto-restart attempt raised an exception.")
