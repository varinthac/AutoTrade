"""Kill-switch halt flag — a small filesystem flag recording whether trading
is currently halted, per spec.md §7 "Safety Gates" and
trading_system_summary_v2.md Appendix B §B.4 "Kill switch".

No persistence layer (`store/`) exists yet, so a flag file is the honest MVP
for this stage: its mere presence means "trading is halted". This is what
scripts/kill_switch.py writes/clears, and what the (not-yet-built) shadow
-running loop will check before placing any new trade.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from autotrade.common.config import REPO_ROOT

DEFAULT_FLAG_PATH = REPO_ROOT / "data" / "db" / "kill_switch.flag"


def activate(reason: str, flag_path: Path | None = None) -> None:
    """Write the halt flag, recording when and why it was activated."""
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
    """Returns the recorded {"activated_at", "reason"} payload if the flag is
    active, or None if not active. A present-but-corrupt flag is reported as
    active with an unknown reason rather than as inactive -- per spec.md §7
    fail-safe defaults, an unreadable flag must never look like "not halted"."""
    path = flag_path or DEFAULT_FLAG_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"activated_at": None, "reason": f"<unreadable flag file at {path}>"}
