"""Watchman position metadata store -- per-position entry-time context that
must survive a process restart, since Watchman's decisions (breakeven/trail
R-multiples, structure invalidation) depend on facts fixed at entry time that
no other part of the system retains once `execution/demo_adapter.py`'s
`place_order()` returns a ticket -- the original `OrderPlan`/swing reference
used to build that order only ever existed transiently during that bar's
processing in `orchestrator/shadow_loop.py`.

Plain JSON-file-backed store, same read-whole-file/write-whole-file
convention as `common/kill_switch_flag.py` and `risk/circuit_breaker.py`'s
`_save_state`/`_load_state` -- one JSON file (default
`data/db/position_metadata.json`) holding a dict keyed by broker ticket (as a
string, since JSON object keys are always strings), same reasoning as
`risk/circuit_breaker.py`'s state persistence: an open position's original
context must not silently vanish if the shadow loop restarts.

`initial_stop_distance` is the R-multiple denominator for every Watchman
threshold (breakeven/trail/time-stop) -- it is recorded once, at entry, and
must NEVER be recomputed from a moved stop-loss later (Watchman itself may
move the live SL closer per `stop_logic.py`, which would silently shrink R if
this were derived from the current SL instead of stored as a fixed fact).

`entry_swing_index` is the bar index of the confirmed swing
(`features/swing.py`'s `latest_confirmed_swing_low`/`latest_confirmed_swing_high`)
that `council/order_construction.py` referenced when building this position's
stop-loss -- needed later by `exit_conditions.check_structure_invalidation`.
IMPORTANT: `check_structure_invalidation` re-uses this index as a positional
`.iloc` lookup into a `df` supplied at evaluation time -- that `df` MUST be
the same fixed-origin, contiguous-`RangeIndex` frame (or one only grown by
appending rows at the end, never trimmed from the front) that was in use
when this index was recorded. See `exit_conditions.check_structure_invalidation`'s
docstring for the full contract.

Phase 7b is responsible for actually calling `record_position_opened` from
the live pipeline (right after a successful `place_order()`) and
`remove_position_metadata` when a position closes -- this module only builds
the store itself.

Corrupt-file handling: unlike a genuinely-missing file (`{}` -- no positions
recorded yet, perfectly normal) or a "ticket not found" lookup (`None` --
that specific position was never opened by this system), a *present-but-
corrupt* metadata file during live operation means real open positions exist
but their entry-time context is unreadable. Silently falling back to `{}` in
that case -- the same pattern `risk/circuit_breaker.py`'s `_load_state` used
to have -- would make `get_position_metadata()` return `None` for every real
open position, indistinguishable from "never existed", so the 7b Watchman
loop would treat live positions as unmanaged with no soft protection at all.
`circuit_breaker.py` fixed its equivalent bug by failing toward its one
safety-critical boolean (`_drawdown_halted = True`). This module has no such
single flag to flip -- instead, `_load_all` raises `CorruptPositionMetadataError`
on a parse failure (after logging loudly), which propagates out of
`record_position_opened`/`get_position_metadata`/`remove_position_metadata`
uncaught. This forces a 7b caller to treat "the position store is
unreadable" as a distinct, loud failure it must explicitly handle (e.g. halt
new entries and alert a human), rather than a codepath that can silently
proceed as if no positions exist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import json

from autotrade.common.config import REPO_ROOT

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "position_metadata.json"


class CorruptPositionMetadataError(Exception):
    """Raised when the position metadata file exists but cannot be parsed.
    This must NOT be swallowed by a caller -- see module docstring's
    "Corrupt-file handling" section for why."""


@dataclass(frozen=True)
class PositionMetadata:
    ticket: int
    symbol: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    initial_stop_distance: float
    entry_swing_index: int
    opened_at: datetime
    # Phase 7b bugfix (news-protection dedup): `None` until news protection
    # first fires for this position; then set to roughly the end of the news
    # blackout window it fired for. `watchman/loop.py` skips re-evaluating
    # `check_news_protection` entirely while `now < news_protected_until`,
    # so the same still-active news window can't re-trigger the protective
    # action (e.g. CLOSE_HALF_AND_BREAKEVEN) every polling cycle and drain
    # the position -- see `watchman/loop.py`'s handling of this field for
    # the full mechanism. Once past this timestamp, a NEW high-impact event
    # can trigger protection again -- this is NOT a permanent one-shot flag.
    news_protected_until: datetime | None = None
    # Phase 8a (trading_system_summary_v2.md Appendix A §5.1's daily-report
    # fields): the entry-time spread (in points) and the fill-vs-intended
    # entry-price slippage, both already computed at order-send time by
    # `execution/demo_adapter.py`'s `_send_order`/`_reconcile_fill` but
    # otherwise lost once that call returns -- recorded here so
    # `watchman/loop.py`'s trade-journal writes can carry them through onto
    # `TradeRecord` at close time instead of always leaving them `None`.
    # `None` for callers that never computed them (e.g. NoOpBrokerAdapter
    # dry runs, or no `current_atr` supplied to `place_order`).
    entry_spread_points: float | None = None
    actual_slippage: float | None = None
    # 2026-07-29 frame-shift bugfix: the actual PRICE level of the entry
    # swing (the swing-low bar's low for a BUY, the swing-high bar's high
    # for a SELL) -- the ONLY thing structure invalidation genuinely needs.
    # `entry_swing_index` above is a POSITIONAL index into whatever
    # in-memory history frame the RECORDING process happened to hold; a
    # restart reseeds that frame from scratch, silently re-pointing the
    # same index at a DIFFERENT bar (observed live: trade #4 closed on a
    # FALSE structure invalidation post-restart; ticket 1826927585's index
    # resolved to high 4030.41 instead of the true swing high 4055.19).
    # `exit_conditions.check_structure_invalidation` prefers this level
    # whenever present; `entry_swing_index` remains only as a legacy
    # fallback for records written before this field existed. `None` for
    # such legacy records and for orphan-seeded metadata (true entry swing
    # unknown).
    entry_swing_level: float | None = None


def _load_all(state_path: Path) -> dict:
    """Fine for the file to simply not exist yet (no positions recorded
    ever) -- returns `{}` silently. A file that exists but fails to parse is
    a different, much louder problem: it means real open positions' entry
    context may be unrecoverable, so this logs an error and raises
    `CorruptPositionMetadataError` rather than returning `{}` (see module
    docstring's "Corrupt-file handling" section)."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "position metadata file %s is corrupt/unreadable (%s); real open "
            "positions' entry context may be unrecoverable -- refusing to "
            "silently treat this as an empty store",
            state_path,
            exc,
        )
        raise CorruptPositionMetadataError(
            f"position metadata file {state_path} is corrupt/unreadable: {exc}"
        ) from exc


def _save_all(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_position_opened(
    ticket: int,
    symbol: str,
    direction: Literal["BUY", "SELL"],
    entry_price: float,
    initial_stop_distance: float,
    entry_swing_index: int,
    opened_at: datetime,
    state_path: Path | None = None,
    entry_spread_points: float | None = None,
    actual_slippage: float | None = None,
    entry_swing_level: float | None = None,
) -> None:
    """Record entry-time context for a newly opened position, keyed by
    broker `ticket`. Overwrites any existing record for the same ticket."""
    path = state_path or DEFAULT_STATE_PATH
    all_positions = _load_all(path)
    all_positions[str(ticket)] = {
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "initial_stop_distance": initial_stop_distance,
        "entry_swing_index": entry_swing_index,
        "opened_at": opened_at.isoformat(),
        "news_protected_until": None,
        "entry_spread_points": entry_spread_points,
        "actual_slippage": actual_slippage,
        "entry_swing_level": entry_swing_level,
    }
    _save_all(path, all_positions)


def get_position_metadata(ticket: int, state_path: Path | None = None) -> PositionMetadata | None:
    """The recorded entry-time context for `ticket`, or None if no record
    exists (never recorded, or already removed after the position closed)."""
    path = state_path or DEFAULT_STATE_PATH
    all_positions = _load_all(path)
    record = all_positions.get(str(ticket))
    if record is None:
        return None
    news_protected_until_raw = record.get("news_protected_until")
    return PositionMetadata(
        ticket=ticket,
        symbol=record["symbol"],
        direction=record["direction"],
        entry_price=record["entry_price"],
        initial_stop_distance=record["initial_stop_distance"],
        entry_swing_index=record["entry_swing_index"],
        opened_at=datetime.fromisoformat(record["opened_at"]),
        news_protected_until=(
            datetime.fromisoformat(news_protected_until_raw) if news_protected_until_raw else None
        ),
        entry_spread_points=record.get("entry_spread_points"),
        actual_slippage=record.get("actual_slippage"),
        entry_swing_level=record.get("entry_swing_level"),
    )


def update_news_protected_until(
    ticket: int, news_protected_until: datetime | None, state_path: Path | None = None,
) -> None:
    """Update just `ticket`'s existing record's `news_protected_until`
    field, leaving every other field untouched -- called by
    `watchman/loop.py` right after news protection fires for a position, so
    the SAME still-active news window doesn't re-trigger the protective
    action every cycle (see `PositionMetadata.news_protected_until`'s
    docstring). A no-op if no record exists for this ticket (position
    already closed, or was never recorded)."""
    path = state_path or DEFAULT_STATE_PATH
    all_positions = _load_all(path)
    record = all_positions.get(str(ticket))
    if record is None:
        return
    record["news_protected_until"] = (
        news_protected_until.isoformat() if news_protected_until is not None else None
    )
    _save_all(path, all_positions)


def get_all_tracked_tickets(state_path: Path | None = None) -> list[int]:
    """Every ticket this system currently has recorded metadata for --
    `watchman/loop.py`'s reconciliation path uses this to find tickets that
    have DISAPPEARED from `BrokerAdapter.get_open_positions()` (closed,
    whether via this system's own `close_position()` call or a broker-side
    SL/TP hit) without going through the explicit-close path first."""
    path = state_path or DEFAULT_STATE_PATH
    all_positions = _load_all(path)
    return [int(t) for t in all_positions.keys()]


def remove_position_metadata(ticket: int, state_path: Path | None = None) -> None:
    """Delete the recorded metadata for `ticket` -- call this when a
    position closes, so metadata doesn't accumulate forever. A no-op if no
    record exists for this ticket."""
    path = state_path or DEFAULT_STATE_PATH
    all_positions = _load_all(path)
    if str(ticket) in all_positions:
        del all_positions[str(ticket)]
        _save_all(path, all_positions)
