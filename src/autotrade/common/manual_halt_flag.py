"""Sticky "operator asked for this to stay stopped" marker.

Distinct from both existing flags: `stop_request_flag` is a cooperative
signal the running loop consumes and clears itself the moment it exits (so
by the time anything checks again, there is no trace it was ever set) --
`kill_switch_flag` is a destructive halt requiring an explicit
`--deactivate --confirm`. Neither one, by itself, lets anything outside the
loop's own process tell "the operator deliberately stopped this" apart from
"it crashed".

**2026-07-28 audit finding.** `scripts/autotrade_control.py stop` requests a
graceful stop; the loop exits cleanly (`stop_request_flag.clear()`) and its
PID file is removed. But `run_health_check.py` ->
`common/loop_watchdog.check_loop_alive(auto_restart=True)` (polled every
~10 minutes via `ops/heartbeat.ps1`) previously treated ANY PID-file-down
state the same way: fire a DOWN alert, then unconditionally relaunch. A
human pausing the loop for maintenance got it silently auto-resurrected
within one heartbeat cycle, with a DOWN->UP Telegram exchange that reads
exactly like "it crashed and recovered" -- inverting the operator's own
intent, with no way to tell from the alerts alone that this had happened.

This flag closes that gap: `do_stop()` sets it alongside the existing
`stop_request_flag`; `loop_watchdog.check_loop_alive()` checks it and, while
active, skips both the DOWN alert and the auto-restart attempt entirely
(the loop's own "AutoTrade stopped (graceful stop requested)" Telegram
message, sent from inside `_check_stop_request()`, is already the correct,
unambiguous signal for an intentional stop -- this flag's job is only to
stop the OTHER, misleading alert from also firing). `do_start()` -- the
explicit, human-invoked "resume" action -- clears it unconditionally before
doing anything else, so auto-restart resumes its normal behavior from that
point on, exactly like running `start` today already does in every other
respect.

Deliberately NOT set by internal/automated callers of `stop_request_flag`
(e.g. `common/calendar_export_watchdog.py`'s own self-recovery stop) --
only the human-facing `stop` command sets this. An automated recovery's own
stop-then-let-the-heartbeat-restart-it cycle must keep working exactly as
it does today; this flag only ever distinguishes a HUMAN's stop from
everything else.

Same simple JSON-file-backed pattern as `kill_switch_flag.py`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from autotrade.common.config import REPO_ROOT

DEFAULT_FLAG_PATH = REPO_ROOT / "data" / "db" / "manual_halt.flag"


def activate(reason: str, flag_path: Path | None = None) -> None:
    """Write the flag, recording when and why it was set."""
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string")

    path = flag_path or DEFAULT_FLAG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def is_active(flag_path: Path | None = None) -> bool:
    path = flag_path or DEFAULT_FLAG_PATH
    return path.exists()


def deactivate(flag_path: Path | None = None) -> None:
    path = flag_path or DEFAULT_FLAG_PATH
    path.unlink(missing_ok=True)


def get_status(flag_path: Path | None = None) -> dict | None:
    """Returns the recorded {"activated_at", "reason"} payload if active, or
    None if not. A present-but-corrupt flag is reported as active with an
    unknown reason rather than as inactive -- same fail-safe-toward-halted
    convention as kill_switch_flag.get_status: an unreadable flag must never
    look like "nothing to worry about" and silently let auto-restart resume."""
    path = flag_path or DEFAULT_FLAG_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"activated_at": None, "reason": f"<unreadable flag file at {path}>"}
