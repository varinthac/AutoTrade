"""Trade journal / structured event log -- write + read functions over
`store/models.py`'s SQLAlchemy ORM. Phase 8b's Auditor daily report is built
entirely from these read functions (`get_trades_for_day`,
`count_blocked_signals_for_day`, `get_anomaly_events_for_day`), written now so
that phase doesn't have to duplicate query logic (trading_system_summary_v2.md
Appendix A §5.1).

Every function opens its own short-lived `Session` (context-managed, closed
before returning) -- same "reopen the store each call" simplicity convention
`watchman/position_metadata.py` and `risk/circuit_breaker.py` already use for
their own plain-file state, applied here to SQLite instead. `db_path=None`
(every function's default) uses `store.models.DEFAULT_DB_PATH`; tests must
always pass an explicit `tmp_path`-based path.

**"Day" = MT5 server day, not UTC/local (Appendix A §0).** Every timestamp
column this module reads/writes is already server time (see
`store/models.py`'s module docstring) -- `get_trades_for_day`/
`count_blocked_signals_for_day`/`get_anomaly_events_for_day` all bucket by a
plain `datetime.date()` half-open range (`[day 00:00, day+1 00:00)`) over
that already-server-time column, the same boundary convention
`risk/circuit_breaker.py`'s `record_trade_close`/`check` use for the
daily-loss reset.

`get_trades_for_day`/`get_trades_in_range` bucket by `exit_time` (when a
trade actually CLOSED/realized), not `entry_time` -- Appendix A §5.1's daily
report is about what happened (closed) on a given day, mirroring
`circuit_breaker.record_trade_close`'s own `closed_at`-based day bucketing.

**Idempotency on `broker_ticket`.** `store/models.py`'s `TradeRecord.broker_ticket`
carries a `UNIQUE` constraint precisely because `watchman/loop.py`'s two close
paths are meant to be non-overlapping but aren't fully atomic across a crash
(e.g. `_record_explicit_close` writes successfully, then
`remove_position_metadata` throws before the ticket is untracked -- the next
cycle's reconciliation then re-observes the same already-closed ticket and
tries to record it again). `record_closed_trade` catches the resulting
`IntegrityError` for that one narrow race, logs a warning, and returns without
raising or duplicating -- this must never happen in normal operation, so if
it fires it's a signal the earlier write half-succeeded, not something to
paper over silently.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from autotrade.notify.telegram import notify
from autotrade.store.models import (
    AnomalyEventRecord,
    BlockedSignalRecord,
    TradeRecord,
    get_engine,
)

logger = logging.getLogger(__name__)

ExitReason = Literal[
    "stop_loss", "take_profit", "structure_invalidation", "time_stop",
    "news_protection", "abnormal_slippage", "manual", "reconciled_system_close", "unknown",
]
BlockSource = Literal[
    "risk_voice", "shield", "borderline_no_conviction", "borderline_conflicting",
    "borderline_near_threshold", "borderline_strong_not_negated",
]
AnomalyEventType = Literal[
    "reconnect", "order_reject", "circuit_breaker_trigger", "execution_failed",
    "abnormal_slippage", "autotrading_disabled", "autotrading_enabled",
    "orphan_position_found", "other",
]


def _day_bounds(server_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(server_date, datetime.min.time())
    return start, start + timedelta(days=1)


def record_closed_trade(
    symbol: str,
    direction: Literal["BUY", "SELL"],
    entry_time: datetime,
    entry_price: float,
    exit_time: datetime,
    exit_price: float,
    exit_reason: ExitReason,
    lot_size: float,
    gross_pnl: float,
    cost: float,
    net_pnl: float,
    r_multiple: float,
    recorded_at: datetime,
    entry_spread_points: float | None = None,
    actual_slippage: float | None = None,
    broker_ticket: int | None = None,
    db_path: Path | None = None,
) -> bool:
    """Persist one fully-closed trade. Called exactly once per broker ticket
    by whichever of `watchman/loop.py`'s two paths observed the close --
    a second attempt for the same `broker_ticket` (the narrow error-recovery
    race described in the module docstring's "Idempotency" section) hits the
    `UNIQUE` constraint and is swallowed here rather than raised.

    Returns `True` if this call genuinely inserted a new `TradeRecord`,
    `False` if it was a swallowed duplicate (nothing new was written) --
    callers that trigger a caller-visible side effect per close (e.g.
    `watchman/loop.py`'s trade-close `notify()`) must check this so a
    swallowed duplicate write doesn't ALSO duplicate that side effect."""
    engine = get_engine(db_path)
    record = TradeRecord(
        symbol=symbol, direction=direction, entry_time=entry_time, entry_price=entry_price,
        exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason, lot_size=lot_size,
        gross_pnl=gross_pnl, cost=cost, net_pnl=net_pnl, r_multiple=r_multiple,
        entry_spread_points=entry_spread_points, actual_slippage=actual_slippage,
        broker_ticket=broker_ticket, recorded_at=recorded_at,
    )
    with Session(engine) as session:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.warning(
                "record_closed_trade: broker_ticket=%s already has a TradeRecord -- this should "
                "only happen via the narrow error-recovery race described in this module's "
                "docstring (remove_position_metadata failing after an explicit close already "
                "recorded), never in normal operation. Skipping this duplicate write rather than "
                "crashing the watchman loop or double-counting the trade.", broker_ticket,
            )
            return False
    return True


def record_blocked_signal(
    timestamp: datetime,
    symbol: str,
    block_source: BlockSource,
    reason: str,
    direction: Literal["BUY", "SELL"] | None = None,
    db_path: Path | None = None,
) -> None:
    """Persist one blocked signal -- called alongside the existing
    `logger.warning(...)` calls at `orchestrator/shadow_loop.py`'s Risk
    Voice veto / Shield block / borderline-decision points, never replacing
    them."""
    engine = get_engine(db_path)
    record = BlockedSignalRecord(
        timestamp=timestamp, symbol=symbol, direction=direction,
        block_source=block_source, reason=reason,
    )
    with Session(engine) as session:
        session.add(record)
        session.commit()


def record_anomaly_event(
    timestamp: datetime,
    event_type: AnomalyEventType,
    details: str,
    db_path: Path | None = None,
) -> None:
    """Persist one anomaly event -- called alongside the existing log line
    at each anomaly's real detection point (connectivity watchdog alert,
    circuit-breaker gate trip, execution failure/abnormal slippage).

    notify() runs BEFORE the DB write, deliberately -- every one of this
    function's real callers (ConnectivityWatchdog, AutoTradingWatchdog,
    CircuitBreaker's gate trips, orphan-position reconciliation) exists
    specifically to alert a human about something going wrong, and a DB
    write can itself fail (locked file -- plausible overlap with
    ops/backup_db.py's nightly online backup, disk full, corruption) for
    reasons unrelated to whether the alert should fire. With the old
    write-then-notify order, a DB hiccup at exactly the moment something
    else is already wrong would silently swallow the ONE alert meant to
    surface it. The DB write is now best-effort after the alert already
    went out -- logged on failure, never raised, matching notify()'s own
    never-raise contract, so a persistence problem here can never look
    like "nothing happened" to the human on the other end of Telegram."""
    try:
        notify(f"[AutoTrade] Anomaly ({event_type}) at {timestamp.isoformat()}: {details}")
    except Exception:
        # notify()'s own docstring guarantees it never raises -- this is
        # pure defense-in-depth in case that contract is ever broken, so a
        # notify-side regression can never also take the DB write down with
        # it (matches tests/unit/store/test_journal.py's own
        # "still persists when notify raises" expectation).
        logger.exception("record_anomaly_event: notify() raised unexpectedly -- persisting the event anyway.")

    try:
        engine = get_engine(db_path)
        record = AnomalyEventRecord(timestamp=timestamp, event_type=event_type, details=details)
        with Session(engine) as session:
            session.add(record)
            session.commit()
    except Exception:
        logger.exception(
            "record_anomaly_event: failed to persist anomaly event to the DB (alert above still attempted) -- "
            "event_type=%s details=%s", event_type, details,
        )


def get_trades_in_range(
    start: datetime, end: datetime, db_path: Path | None = None,
) -> list[TradeRecord]:
    """Every `TradeRecord` whose `exit_time` falls in `[start, end)`,
    ordered by `exit_time`."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        stmt = (
            select(TradeRecord)
            .where(TradeRecord.exit_time >= start, TradeRecord.exit_time < end)
            .order_by(TradeRecord.exit_time)
        )
        return list(session.execute(stmt).scalars().all())


def get_trades_for_day(server_date: date, db_path: Path | None = None) -> list[TradeRecord]:
    """Every `TradeRecord` that closed on `server_date` (MT5 server day, see
    module docstring)."""
    start, end = _day_bounds(server_date)
    return get_trades_in_range(start, end, db_path=db_path)


def count_blocked_signals_for_day(
    server_date: date, db_path: Path | None = None,
) -> dict[str, int]:
    """Count of `BlockedSignalRecord`s on `server_date`, grouped by
    `block_source` -- Appendix A §5.1's "signal ที่ถูก block แยกตามเหตุผล"."""
    start, end = _day_bounds(server_date)
    engine = get_engine(db_path)
    with Session(engine) as session:
        stmt = select(BlockedSignalRecord).where(
            BlockedSignalRecord.timestamp >= start, BlockedSignalRecord.timestamp < end,
        )
        records = session.execute(stmt).scalars().all()

    counts: dict[str, int] = {}
    for record in records:
        counts[record.block_source] = counts.get(record.block_source, 0) + 1
    return counts


def get_anomaly_events_in_range(
    start: datetime, end: datetime, db_path: Path | None = None,
) -> list[AnomalyEventRecord]:
    """Every `AnomalyEventRecord` whose `timestamp` falls in `[start, end)`,
    ordered by `timestamp` -- added for Phase 8b's promotion-gate CLI
    (`scripts/run_auditor.py`'s Live ramp -> Full size gate needs to scan a
    whole live-ramp period, not just one day, for a `drawdown_halt` circuit
    breaker trigger)."""
    engine = get_engine(db_path)
    with Session(engine) as session:
        stmt = (
            select(AnomalyEventRecord)
            .where(AnomalyEventRecord.timestamp >= start, AnomalyEventRecord.timestamp < end)
            .order_by(AnomalyEventRecord.timestamp)
        )
        return list(session.execute(stmt).scalars().all())


def get_anomaly_events_for_day(
    server_date: date, db_path: Path | None = None,
) -> list[AnomalyEventRecord]:
    """Every `AnomalyEventRecord` on `server_date`, ordered by `timestamp`."""
    start, end = _day_bounds(server_date)
    return get_anomaly_events_in_range(start, end, db_path=db_path)
