#!/usr/bin/env python3
"""Launches the read-only trade dashboard (Flask, paper mode only) -- trade
history and daily reports over the same trade journal `run_auditor.py`
reads, served locally only. Binds strictly to 127.0.0.1 (never 0.0.0.0): no
auth, read-only reporting, must never be reachable off this machine.

2026-08-04 (lean-plan P1, docs/vps_lean_plan.md): the dashboard is on-demand
now, launched via Telegram's `/dashboard` command (notify/telegram_control.py)
rather than an always-on logon task -- this script gained an idle-TTL
auto-shutdown so a forgotten instance doesn't sit around costing the ~98 MB
`--idle-ttl-minutes` was written to reclaim. `--idle-ttl-minutes 0` (or any
non-positive value) disables it for the dev PC / tests that want the old
always-on behaviour. **`scripts/run_health_check.py` deliberately no longer
auto-restarts this process** -- that trap is documented there; do not add it
back.

    python scripts/run_dashboard.py [--port 8765] [--db-path PATH] [--idle-ttl-minutes 30]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from autotrade.common import pid_file
from autotrade.common.config import REPO_ROOT
from autotrade.dashboard.app import create_app
from autotrade.store.models import DEFAULT_PAPER_DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_IDLE_TTL_MINUTES = 30
_IDLE_CHECK_INTERVAL_SEC = 30

# 2026-07-24: an RDP reconnect re-fires every "At log on" Task Scheduler
# trigger for that logon event (Shadow Loop, Dashboard, Telegram Control
# alike -- see docs/vps_deployment.md Section 6a), not just the first time.
# A second Flask instance racing the first for the same port fails its
# bind and crashes -- refusing up front here, matching
# run_shadow_loop.py's own PID-file guard, turns that into a clean no-op.
#
# The idle-TTL watchdog below leaves this file behind on its own
# self-terminating exit (os._exit() skips the try/finally in main()) rather
# than cleaning it up first -- deliberately: common/pid_file.py's own
# is_pid_running()/is_running() checks already treat a PID file naming a
# dead process as stale and safe to overwrite (see write()'s "stale/
# unreadable one is removed and the exclusive-create retried once" and
# is_running()'s "False for ... a stale (dead) PID"), so the next `/dashboard`
# launch or manual start cleans it up for free -- no extra machinery needed.
PID_PATH = REPO_ROOT / "data" / "db" / "dashboard.pid"


class _ActivityTracker:
    """Records the monotonic time of the most recent HTTP request (or
    process start, before any request has arrived yet) -- `dashboard/app.py`'s
    `create_app(on_request=...)` hook calls `touch()` on every inbound
    request; `_idle_watchdog()` below polls `idle_seconds()`. `time.monotonic()`
    rather than wall-clock time: immune to a system clock adjustment
    mid-run falsely triggering (or indefinitely delaying) a shutdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request_at = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last_request_at = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_request_at


def _should_shut_down(idle_seconds: float, ttl_minutes: float) -> bool:
    """Pure decision function, kept separate from `_idle_watchdog()`'s
    thread/sleep plumbing so it's unit-testable without a real 30-minute
    wait. `ttl_minutes <= 0` is this module's documented "never auto-shut-down"
    convention (module docstring) -- matches `run_dashboard.py`'s CLI help
    text, not an arbitrary internal choice."""
    if ttl_minutes <= 0:
        return False
    return idle_seconds >= ttl_minutes * 60


def _idle_watchdog(
    tracker: _ActivityTracker, ttl_minutes: float, stop_event: threading.Event, exit_fn=None,
) -> None:
    """Runs on a daemon thread (started only when `ttl_minutes > 0`) --
    polls `_should_shut_down()` every `_IDLE_CHECK_INTERVAL_SEC` and, once
    tripped, logs one line and exits the whole process via `exit_fn`
    (`os._exit()` by default, injectable for tests). `os._exit()` rather than
    `sys.exit()`: this runs on a background thread, where `sys.exit()` would
    only end the thread, leaving Flask's own thread serving forever -- see
    PID_PATH's own comment above for why skipping the normal
    try/finally PID-file cleanup here is fine. `stop_event.wait()` doubles as
    the sleep AND an immediate-wake early-exit signal (set when `main()`'s
    own `app.run()` returns normally, e.g. Ctrl+C during local dev) rather
    than a plain `time.sleep()` that would keep this thread alive for up to
    `_IDLE_CHECK_INTERVAL_SEC` seconds after the process should have already
    exited."""
    exit_fn = exit_fn or os._exit
    while not stop_event.wait(_IDLE_CHECK_INTERVAL_SEC):
        if _should_shut_down(tracker.idle_seconds(), ttl_minutes):
            logger.warning(
                "Dashboard idle for >= %.0f minute(s) (--idle-ttl-minutes) with no HTTP request -- "
                "auto-shutting down.", ttl_minutes,
            )
            exit_fn(0)
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_PAPER_DB_PATH)
    parser.add_argument(
        "--idle-ttl-minutes", type=float, default=_DEFAULT_IDLE_TTL_MINUTES,
        help=(
            "Auto-shut-down after this many minutes with no HTTP request (default %(default)s). "
            "0 or negative disables auto-shutdown entirely -- the old always-on behaviour, for "
            "local dev or tests."
        ),
    )
    args = parser.parse_args()

    existing_pid = pid_file.read(PID_PATH)
    if existing_pid is not None and pid_file.is_pid_running(existing_pid):
        logger.error(
            "Dashboard already running (PID %d) -- refusing to start a second instance (it would "
            "just fail to bind the same port).", existing_pid,
        )
        return 1
    try:
        pid_file.write(os.getpid(), PID_PATH)
    except FileExistsError as exc:
        logger.error("Lost the race to claim the PID file: %s", exc)
        return 1

    tracker = _ActivityTracker()
    stop_event = threading.Event()
    if args.idle_ttl_minutes > 0:
        threading.Thread(
            target=_idle_watchdog, args=(tracker, args.idle_ttl_minutes, stop_event), daemon=True,
        ).start()

    try:
        app = create_app(db_path=args.db_path, on_request=tracker.touch)
        app.run(host="127.0.0.1", port=args.port)
    finally:
        stop_event.set()
        pid_file.remove(PID_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
