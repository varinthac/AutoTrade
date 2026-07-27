"""Periodic "kill switch has been active for N hours" reminder.

**2026-07-28 audit finding.** `scripts/kill_switch.py`'s `do_activate()`
sends exactly one Telegram alert when the kill switch is first activated,
and `kill_switch_flag.py` never auto-deactivates by design (see its own
module docstring). If an operator activates it for a legitimate reason and
then forgets -- or simply misses that one message -- trading halts
indefinitely with zero further signal. `autotrade_control.py status` shows
it, but nothing pushes that state proactively; a forgotten halt is
indistinguishable from "quiet market" to anyone not actively checking.

Called once per heartbeat cycle from `scripts/run_health_check.py`,
alongside the other watchdogs. Rate-limited to once per
`reminder_interval_hours` (default 24 -- a daily reminder while still
active, not a repeat of every ~10-minute heartbeat cycle) via a small state
file, same cooldown pattern as `common/calendar_export_watchdog.py`. The
FIRST periodic reminder is timed from the kill switch's own `activated_at`
(not from "now") -- `do_activate()` already sent one alert at that moment,
so the first follow-up shouldn't fire again until a full interval has
actually passed since then, not immediately on the next heartbeat cycle."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrade.common import kill_switch_flag
from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "kill_switch_reminder_state.json"


def check_and_remind(state_path: Path | None = None, reminder_interval_hours: float = 24.0) -> bool:
    """Returns True if a reminder was sent this cycle. Never raises -- a
    failure here must not prevent the other, already-working health checks
    from running."""
    try:
        state_path = state_path or DEFAULT_STATE_PATH
        status = kill_switch_flag.get_status()
        now = datetime.now(timezone.utc)

        if status is None:
            state_path.unlink(missing_ok=True)  # not active -- no stale reminder baseline to carry forward
            return False

        last_reminded = _load_last_reminded(state_path)
        if last_reminded is None:
            last_reminded = _parse_iso(status.get("activated_at"))
            if last_reminded is None:
                # Can't determine how long it's been active (a corrupt/
                # unreadable flag file -- see kill_switch_flag.get_status's
                # own fail-safe-toward-active docstring) -- fail toward
                # alerting once rather than silently never reminding at all.
                last_reminded = datetime.fromtimestamp(0, tz=timezone.utc)

        if now - last_reminded < timedelta(hours=reminder_interval_hours):
            return False

        activated_at = _parse_iso(status.get("activated_at"))
        # 2026-07-28 code review finding: check the PARSED value, not the
        # raw string's truthiness -- an unparseable-but-non-empty
        # activated_at (a corrupt flag file) used to compute duration as
        # now-minus-now (0.0 hours) instead of correctly falling through to
        # "an unknown duration".
        duration_desc = f"{(now - activated_at).total_seconds() / 3600:.1f} hour(s)" if activated_at else "an unknown duration"
        reason = status.get("reason") or "<unknown>"

        notify(
            f"[AutoTrade] ⏰ Reminder: the kill switch has been ACTIVE for {duration_desc} "
            f"(reason: {reason}). Trading remains halted. Run 'python scripts/kill_switch.py "
            "--deactivate --confirm' if you intend to resume."
        )
        _save_last_reminded(state_path, now)
        return True
    except Exception:
        logger.exception("kill_switch_reminder: check_and_remind raised -- leaving other health checks unaffected.")
        return False


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _load_last_reminded(state_path: Path) -> datetime | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_reminded_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        logger.warning(
            "kill_switch_reminder: state file %s is corrupt/unreadable -- treating as no prior reminder",
            state_path,
        )
        return None


def _save_last_reminded(state_path: Path, when: datetime) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_reminded_at": when.isoformat()}), encoding="utf-8")
