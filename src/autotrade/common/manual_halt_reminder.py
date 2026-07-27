"""Periodic "the shadow loop has been manually stopped for N hours"
reminder.

**2026-07-28 code review finding.** `common/manual_halt_flag.py` was added
the same day specifically so `common/loop_watchdog.py`'s heartbeat-driven
auto-restart would stay quiet -- correctly -- while an operator's
deliberate `stop` is in effect (see that module's own docstring for the
incident: a deliberate stop otherwise got silently auto-resurrected).
But "stay quiet forever" reintroduces the exact same class of problem this
whole audit exists to fix: an operator who stops the loop for maintenance
and then forgets to `start` it again gets zero further signal, ever --
trading stays halted indefinitely with nothing distinguishing it from
"nobody's touched this in days, all quiet." `kill_switch_reminder.py`
already solves this for the kill switch; this is the same fix for
`manual_halt_flag`, same shape, same cooldown pattern.

Called once per heartbeat cycle from `scripts/run_health_check.py`.
Rate-limited to once per `reminder_interval_hours` (default 24) via a small
state file, same cooldown pattern as `kill_switch_reminder.py`. The FIRST
periodic reminder is timed from `manual_halt_flag`'s own `activated_at`
(not "now") -- `do_stop()` already sent its own confirmation via the
loop's own "AutoTrade stopped" Telegram message at that moment, so the
first follow-up shouldn't fire again until a full interval has actually
passed since then."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrade.common import manual_halt_flag
from autotrade.common.config import REPO_ROOT
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "manual_halt_reminder_state.json"


def check_and_remind(state_path: Path | None = None, reminder_interval_hours: float = 24.0) -> bool:
    """Returns True if a reminder was sent this cycle. Never raises -- a
    failure here must not prevent the other, already-working health checks
    from running."""
    try:
        state_path = state_path or DEFAULT_STATE_PATH
        status = manual_halt_flag.get_status()
        now = datetime.now(timezone.utc)

        if status is None:
            state_path.unlink(missing_ok=True)  # not active -- no stale reminder baseline to carry forward
            return False

        last_reminded = _load_last_reminded(state_path)
        if last_reminded is None:
            last_reminded = _parse_iso(status.get("activated_at"))
            if last_reminded is None:
                # Can't determine how long it's been active (a corrupt/
                # unreadable flag file) -- fail toward alerting once rather
                # than silently never reminding at all.
                last_reminded = datetime.fromtimestamp(0, tz=timezone.utc)

        if now - last_reminded < timedelta(hours=reminder_interval_hours):
            return False

        activated_at = _parse_iso(status.get("activated_at"))
        duration_desc = f"{(now - activated_at).total_seconds() / 3600:.1f} hour(s)" if activated_at else "an unknown duration"
        reason = status.get("reason") or "<unknown>"

        notify(
            f"[AutoTrade] ⏰ Reminder: the shadow loop has been manually STOPPED for {duration_desc} "
            f"(reason: {reason}). The heartbeat's auto-restart will NOT bring it back up on its own -- "
            "run 'python scripts/autotrade_control.py start' if you intend to resume trading."
        )
        _save_last_reminded(state_path, now)
        return True
    except Exception:
        logger.exception("manual_halt_reminder: check_and_remind raised -- leaving other health checks unaffected.")
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
            "manual_halt_reminder: state file %s is corrupt/unreadable -- treating as no prior reminder",
            state_path,
        )
        return None


def _save_last_reminded(state_path: Path, when: datetime) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_reminded_at": when.isoformat()}), encoding="utf-8")
