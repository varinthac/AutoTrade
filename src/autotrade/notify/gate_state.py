"""Small file-based cache of the last-known promotion/demotion gate results,
used by `scripts/run_auditor.py`'s `--notify` flag to detect state CHANGES
(only notify when something actually changed) rather than resending the same
gate result on every invocation. Same whole-file JSON read/write idiom as
`common/kill_switch_flag.py` / `risk/circuit_breaker.py`'s state persistence
-- no partial updates, no locking (single-operator CLI, not a service).

An unreadable/missing state file is treated as "no prior state", not an
error -- the first-ever evaluation of a given gate then always looks like a
change and notifies once, rather than raising or silently staying quiet
forever.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from autotrade.common.config import REPO_ROOT

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "notify_gate_state.json"

PromotionGate = Literal["backtest", "paper", "live"]
DemotionAction = Literal["none", "revert_to_paper", "halt_and_investigate"]


def _load(state_path: Path | None = None) -> dict:
    path = state_path or DEFAULT_STATE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "notify gate-state file %s is corrupt/unreadable (%s) -- treating as no prior state",
            path, exc,
        )
        return {}


def _save(state: dict, state_path: Path | None = None) -> None:
    path = state_path or DEFAULT_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _format_gate_status(value: bool | None) -> str:
    if value is None:
        return "PENDING"
    return "PASSED" if value else "FAILED"


def check_promotion_gate_changed(
    gate: PromotionGate, passed: bool, state_path: Path | None = None,
) -> tuple[bool, str]:
    """Compares `passed` against the last-known PERSISTED result for `gate`
    and returns `(changed, description)` -- read-only, does NOT persist
    anything itself (see `record_promotion_gate`). Split this way so a
    caller (`scripts/run_auditor.py`'s `--notify`) can persist the new state
    only after confirming `notify()` actually returned `True`: persisting
    unconditionally, before knowing whether the Telegram send succeeded,
    would mark a gate change "already notified" even when the message was
    lost to a transient outage -- silently and permanently dropping a
    promotion/demotion-relevant notification instead of retrying it on the
    next run. Always `changed = True` on the first-ever evaluation of a
    given `gate` (no prior state -> `None`, and `None != passed` for any
    bool)."""
    state = _load(state_path)
    previous = state.get("promotion", {}).get(gate)
    changed = previous != passed
    description = f"Gate ({gate}): {_format_gate_status(previous)} -> {_format_gate_status(passed)}"
    return changed, description


def record_promotion_gate(gate: PromotionGate, passed: bool, state_path: Path | None = None) -> None:
    """Persists `passed` as the new last-known result for `gate`. Call only
    after a `check_promotion_gate_changed`-detected change has actually been
    notified successfully (see that function's docstring)."""
    state = _load(state_path)
    promotion = state.get("promotion", {})
    promotion[gate] = passed
    state["promotion"] = promotion
    _save(state, state_path)


def check_demotion_changed(
    action: DemotionAction, state_path: Path | None = None,
) -> tuple[bool, str]:
    """Compares `action` against the last-known PERSISTED demotion action and
    returns `(changed, description)` -- read-only, does NOT persist anything
    itself (see `record_demotion`, and `check_promotion_gate_changed`'s
    docstring for why this split exists). Always `changed = True` on the
    first-ever evaluation (no prior state -> `None`, and `None != action`
    for any action string, including `"none"`)."""
    state = _load(state_path)
    previous = state.get("demotion")
    changed = previous != action
    description = f"Demotion: {previous or 'none'} -> {action}"
    return changed, description


def record_demotion(action: DemotionAction, state_path: Path | None = None) -> None:
    """Persists `action` as the new last-known demotion action. Call only
    after a `check_demotion_changed`-detected change has actually been
    notified successfully."""
    state = _load(state_path)
    state["demotion"] = action
    _save(state, state_path)
