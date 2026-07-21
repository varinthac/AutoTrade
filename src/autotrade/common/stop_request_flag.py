"""Graceful stop-request flag -- a small filesystem flag asking the running
`orchestrator/shadow_loop.py` loop to exit on its next poll cycle, per the
day-to-day start/stop operational workflow (see `scripts/autotrade_control.py`).

This is NOT the kill switch (`common/kill_switch_flag.py`): it carries no
position-closing semantics at all. Setting it just means "please stop
polling for new bars soon" -- any open positions are left exactly as they
are (broker-side SL/TP still active, but Watchman no longer trails/manages
them until the loop is restarted). Same simple JSON-file-backed pattern as
`kill_switch_flag.py`, just a second, independent flag with different,
non-destructive semantics.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from autotrade.common.config import REPO_ROOT

DEFAULT_FLAG_PATH = REPO_ROOT / "data" / "db" / "stop_request.flag"


def request(reason: str, flag_path: Path | None = None) -> None:
    """Write the stop-request flag, recording when and why it was requested."""
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string")

    path = flag_path or DEFAULT_FLAG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def is_requested(flag_path: Path | None = None) -> bool:
    path = flag_path or DEFAULT_FLAG_PATH
    return path.exists()


def clear(flag_path: Path | None = None) -> None:
    path = flag_path or DEFAULT_FLAG_PATH
    path.unlink(missing_ok=True)


def get_status(flag_path: Path | None = None) -> dict | None:
    """Returns the recorded {"requested_at", "reason"} payload if a stop is
    pending, or None if not. A present-but-corrupt flag is reported as
    pending with an unknown reason rather than as "not requested" -- same
    fail-safe-toward-stopping convention as `kill_switch_flag.get_status`."""
    path = flag_path or DEFAULT_FLAG_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"requested_at": None, "reason": f"<unreadable flag file at {path}>"}
