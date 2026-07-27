"""Self-healing recovery for a silently-dead `NewsCalendarExporter` MQL5
Service (`mql5/NewsCalendarExporter.mq5`), meant to be called once per cycle
from `scripts/run_health_check.py` alongside the existing shadow-loop/
dashboard/Telegram watchdogs.

**2026-07-25/28 incident, and why this exists.** `council/mql5_calendar_provider.py`
already detects and alerts on a stale export file (fail-safe-vetoing every
news-blackout check in the meantime -- see that module's own docstring), but
only ever *logs and alerts*; nothing previously attempted to actually fix
it. The Service died silently around 2026-07-25 03:xx and the terminal was
never restarted, so it simply never came back -- vetoing every USD signal
for many hours across 2026-07-26/27, then the export finally recovered on
2026-07-28 only via a manual RDP/SSH intervention (kill `terminal64.exe`,
let it relaunch -- MQL5 Services configured `enabled=1` in the terminal's
own `config/services.ini`, confirmed by this incident, auto-start again on
terminal launch; the earlier assumption in mql5_calendar_provider.py's own
docstring that this flag was never set turned out to be stale/wrong by the
time of this incident). There is no command-line API to restart an MQL5
Service directly (Services only start/stop via the terminal's Navigator
panel, or automatically at terminal launch) -- a full terminal relaunch is
the only scriptable recovery path found.

**Recovery sequence**, mirroring the exact manual steps that resolved the
2026-07-28 incident:
1. Request a graceful shadow-loop stop (`stop_request_flag`) and wait
   briefly for it to actually exit -- killing `terminal64.exe` out from
   under a live `mt5_session()` is not a tested/graceful path (the running
   loop's next MT5 call would just fail), whereas stop-then-restart-fresh
   is the exact sequence already proven safe.
2. Kill `terminal64.exe` (`taskkill`, matching `common/pid_file.py`'s own
   subprocess-over-psutil convention). Safe regardless of open positions --
   those live on the broker server, not the local terminal; the same
   accepted risk window as any other loop-down period (`common/loop_watchdog.py`'s
   `_DOWN_MESSAGE`: broker-side SL/TP still protects an open position while
   the loop is down).
3. Leave the actual relaunch to `run_health_check.py`'s existing
   `check_loop_alive(auto_restart=True)` call, invoked right after this one
   in the same heartbeat cycle -- it already handles "loop is down -> launch
   scripts/autotrade_control.py start", which auto-launches a fresh
   `terminal64.exe` via `mt5.initialize()`, and the Service comes back up on
   its own via `enabled=1`. No need to duplicate that relaunch logic here.

**Threshold and cooldown, deliberately distinct from
`MQL5CalendarProvider`'s own 10-minute alert threshold**: this watchdog's
own trigger (`staleness_threshold_minutes`, default 20 -- roughly 4 missed
5-minute export cycles) is set higher, so a single missed cycle or the
provider's own already-firing alert doesn't also immediately trigger a full
terminal/loop restart; the exporter gets a couple of chances to recover on
its own first. `cooldown_minutes` (default 30) rate-limits repeat restart
attempts if the underlying cause isn't something a restart can fix (e.g.
MT5 login itself broken) -- without it, every 10-minute heartbeat cycle
would otherwise keep bouncing the whole shadow loop forever.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrade.common import manual_halt_flag, pid_file, stop_request_flag
from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "calendar_export_watchdog_state.json"
_EXPORT_FILENAME = "AutoTradeNewsCalendar.csv"
_TERMINAL_PROCESS_NAME = "terminal64.exe"
_STOP_WAIT_TIMEOUT_SEC = 90
_STOP_WAIT_POLL_SEC = 5
_TASKKILL_TIMEOUT_SEC = 30


def default_export_path() -> Path:
    """`%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files\\...` -- the standard,
    non-portable-mode MT5 install location for the shared Common Files
    folder, confirmed against the actual VPS during the 2026-07-28 incident
    (`mt5.terminal_info().commondata_path` resolved to exactly this).
    Deliberately does NOT go through `mql5_calendar_provider.resolve_commondata_path()`
    (which requires an active `mt5_session()`) -- this watchdog must keep
    working even when the terminal itself is down entirely, not just the
    Service inside it, so it cannot depend on MT5 already being reachable to
    find the very file that tells it whether MT5 needs recovering."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("calendar_export_watchdog: %APPDATA% is not set -- cannot locate the MT5 Common Files folder")
    return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files" / _EXPORT_FILENAME


def _load_last_restart(state_path: Path) -> datetime | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_restart_attempt"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        logger.warning(
            "calendar_export_watchdog: state file %s is corrupt/unreadable -- treating as no prior attempt",
            state_path,
        )
        return None


def _save_last_restart(state_path: Path, when: datetime) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_restart_attempt": when.isoformat()}), encoding="utf-8")


def check_and_recover(
    export_path: Path | None = None,
    state_path: Path | None = None,
    staleness_threshold_minutes: float = 20.0,
    cooldown_minutes: float = 30.0,
) -> bool:
    """Call this once per heartbeat cycle, before `loop_watchdog.check_loop_alive`.
    Returns True if a recovery attempt (stop + kill terminal) was made this
    cycle, False if the export is fresh, a recovery was skipped (within
    cooldown), or `manual_halt_flag` is active. Never raises -- a failure in
    this watchdog must not prevent the other, already-working health checks
    from running.

    **2026-07-28 code review finding: respects `manual_halt_flag`.** While
    an operator has deliberately stopped the loop (`scripts/autotrade_control.py
    stop` -- see `manual_halt_flag.py`'s own docstring), this watchdog would
    otherwise keep "recovering" every cooldown period for as long as the
    export stays stale, e.g. because the operator also closed the MT5
    terminal for the same maintenance window -- repeatedly re-alerting and
    `taskkill`-ing `terminal64.exe` out from under them if they reopen it to
    check on something mid-maintenance. `check_loop_alive` already refuses
    to auto-restart the loop while this flag is active; skipping the
    recovery attempt here too keeps this watchdog quiet for the same
    reason, for the same duration."""
    try:
        if manual_halt_flag.is_active():
            logger.info(
                "calendar_export_watchdog: export may be stale, but manual_halt_flag is active "
                "(operator-requested stop) -- not attempting recovery."
            )
            return False

        path = export_path or default_export_path()
        state_path = state_path or DEFAULT_STATE_PATH
        now = datetime.now(timezone.utc)

        if path.exists():
            age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        else:
            age = None  # never exported at all is just as much a problem as stale

        threshold = timedelta(minutes=staleness_threshold_minutes)
        if age is not None and age <= threshold:
            return False

        last_restart = _load_last_restart(state_path)
        if last_restart is not None and now - last_restart < timedelta(minutes=cooldown_minutes):
            logger.warning(
                "calendar_export_watchdog: export still stale (age=%s) but last recovery attempt was "
                "%s ago (< %s cooldown) -- skipping this cycle.",
                age, now - last_restart, timedelta(minutes=cooldown_minutes),
            )
            return False

        age_desc = str(age) if age is not None else "file does not exist"
        logger.critical(
            "calendar_export_watchdog: export file stale (%s, threshold %s) -- attempting automatic "
            "recovery (stop loop, restart MT5 terminal).",
            age_desc, threshold,
        )
        notify(
            f"[AutoTrade] \U0001F527 Economic calendar export has been stale for {age_desc} -- "
            "attempting automatic recovery: stopping the shadow loop and restarting the MT5 terminal "
            "so the NewsCalendarExporter Service restarts. The loop will relaunch automatically once "
            "the terminal is back."
        )

        stop_request_flag.request("calendar export watchdog auto-recovery")
        _wait_for_loop_stop()
        _kill_terminal()

        _save_last_restart(state_path, now)
        return True
    except Exception:
        logger.exception("calendar_export_watchdog: check_and_recover raised -- leaving other health checks unaffected.")
        return False


def _wait_for_loop_stop(timeout_sec: float = _STOP_WAIT_TIMEOUT_SEC, poll_sec: float = _STOP_WAIT_POLL_SEC) -> None:
    """Best-effort wait for the shadow loop to exit after a graceful stop
    request -- proceeds to kill the terminal regardless once the timeout
    elapses (staying stale forever is worse than a slightly-less-clean
    terminal kill)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not pid_file.is_running():
            return
        time.sleep(poll_sec)
    logger.warning(
        "calendar_export_watchdog: shadow loop did not confirm stopped within %ss -- proceeding to "
        "restart the terminal anyway.",
        timeout_sec,
    )


def _kill_terminal() -> None:
    # 2026-07-28 code review finding: a timeout, matching
    # loop_watchdog._attempt_restart's own subprocess.run(timeout=...) --
    # without one, a hung taskkill would hang this whole health-check
    # cycle indefinitely (mitigated but not fully covered by
    # ops/heartbeat.ps1's external healthchecks.io ping, which only
    # detects the miss after the fact).
    result = subprocess.run(
        ["taskkill", "/IM", _TERMINAL_PROCESS_NAME, "/F"],
        capture_output=True, text=True, check=False, timeout=_TASKKILL_TIMEOUT_SEC,
    )
    if result.returncode == 0:
        logger.warning("calendar_export_watchdog: killed %s.", _TERMINAL_PROCESS_NAME)
    else:
        logger.warning(
            "calendar_export_watchdog: taskkill %s exited %d (may simply mean it wasn't running): %s",
            _TERMINAL_PROCESS_NAME, result.returncode, result.stderr.strip(),
        )
