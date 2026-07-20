"""SQLAlchemy ORM models for the live trade journal / structured event log
(spec.md §4 "Persistence": SQLite (WAL mode) via SQLAlchemy -- trade journal,
config versions, and audit logs). This is what Phase 8b's Auditor reads to
build the daily trade-autopsy report (trading_system_summary_v2.md Appendix A
§5.1) -- see that section's list ("จำนวนไม้/win/loss/net P&L/ค่าเฉลี่ย R,
signal ที่ถูก block แยกตามเหตุผล, slippage จริง vs ที่คาด/spread เฉลี่ย,
เหตุการณ์ผิดปกติ") for exactly why these three tables have the fields they do.

**Timestamps are MT5 broker SERVER time, not UTC/local.** Same convention as
`risk/circuit_breaker.py`/`common/mt5_time.py`: every timestamp column here
(`entry_time`, `exit_time`, `timestamp`, ...) is a naive `datetime` that
already IS server time -- never converted, never mixed with UTC/local. Day-
boundary queries (`store/journal.py`'s `get_trades_for_day` etc.) rely on
this being consistent everywhere a timestamp is written.

**Live trade journal, not the backtest one.** `TradeRecord` is shaped
similarly to `backtest/engine.py`'s `ClosedTrade` (same
symbol/direction/entry/exit/exit_reason/lot_size/gross_pnl/cost/net_pnl/
r_multiple core) but is a distinct concept with live-only fields
(`entry_spread_points`, `actual_slippage`, `broker_ticket`) -- they are not
interchangeable and this module does not import from `backtest/`.

**Cost-model convention (mirrors backtest/engine.py's `ClosedTrade`
docstring):** `gross_pnl` is the pure price P&L (entry to exit, before
commission/swap); `cost` is commission + swap combined, as a POSITIVE number
to subtract; `net_pnl = gross_pnl - cost`. See `watchman/loop.py`'s two-path
reconciliation design for exactly how each path populates these -- the
EXPLICIT-close path (this system's own `close_position()` call) computes
`gross_pnl` from price only and cannot see live commission/swap figures
without an extra MT5 history query, so `cost` is `0.0` there; the
RECONCILIATION path (broker-side SL/TP hits, detected via
`BrokerAdapter.get_closed_trade_info()`) DOES query MT5's own deal history
and so gets real commission+swap in `cost`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from autotrade.common.config import REPO_ROOT

DEFAULT_DB_PATH = REPO_ROOT / "data" / "db" / "trade_journal.sqlite"


class Base(DeclarativeBase):
    pass


class TradeRecord(Base):
    """One fully-closed live position -- written exactly once per broker
    ticket, by whichever of `watchman/loop.py`'s two non-overlapping paths
    (explicit close vs. reconciliation) actually observed the close, never
    both (see that module's docstring)."""

    __tablename__ = "trade_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str]
    direction: Mapped[str]
    entry_time: Mapped[datetime]
    entry_price: Mapped[float]
    exit_time: Mapped[datetime]
    exit_price: Mapped[float]
    exit_reason: Mapped[str]
    """One of: stop_loss / take_profit / structure_invalidation / time_stop /
    news_protection / abnormal_slippage / manual / unknown."""
    lot_size: Mapped[float]
    gross_pnl: Mapped[float]
    cost: Mapped[float]
    net_pnl: Mapped[float]
    r_multiple: Mapped[float]
    entry_spread_points: Mapped[float | None] = mapped_column(default=None)
    actual_slippage: Mapped[float | None] = mapped_column(default=None)
    broker_ticket: Mapped[int | None] = mapped_column(default=None, unique=True, index=True)
    """SQLite treats each `NULL` as distinct for a `UNIQUE` constraint, so
    multiple no-ticket records are still allowed -- this only rejects a
    second row for the SAME real ticket, the double-write scenario
    `store/journal.py`'s `record_closed_trade` guards against via
    `IntegrityError`."""
    recorded_at: Mapped[datetime]
    """When THIS system wrote the record (server time) -- distinct from
    `exit_time` (when the position actually closed), useful to tell a
    same-cycle write apart from a reconciliation write that lagged behind
    the real close."""


class BlockedSignalRecord(Base):
    """One signal that was NOT traded because Risk Voice vetoed it, Shield
    blocked it, or the Council decision itself was borderline/no-conviction
    -- `orchestrator/shadow_loop.py`'s wiring at each of those three
    existing decision points (Appendix A §5.1's "signal ที่ถูก block แยกตาม
    เหตุผล: Risk veto, Shield block, borderline no-trade")."""

    __tablename__ = "blocked_signal_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime]
    symbol: Mapped[str]
    direction: Mapped[str | None] = mapped_column(default=None)
    """The hypothetical direction that was blocked -- `None` when no
    hypothetical direction was ever computed (a genuine "no conviction" case
    where bull/bear scores were both too low to even guess a side, see
    `council/decision_matrix.py`'s borderline-reason branches)."""
    block_source: Mapped[str] = mapped_column(index=True)
    """One of: risk_voice / shield / borderline_no_conviction /
    borderline_conflicting / borderline_near_threshold /
    borderline_strong_not_negated -- fine-grained enough to distinguish
    Appendix A §5.1's three top-level categories (Risk veto, Shield block,
    borderline no-trade) while still keeping the four distinct
    `borderline_reason` values `council/decision_matrix.py` can produce."""
    reason: Mapped[str]
    """Free text -- the existing `ShieldDecision`/`RiskVoiceDecision`/
    `CouncilDecision` reason string(s) already produced at the call site."""


class AnomalyEventRecord(Base):
    """One anomaly worth surfacing in the daily report (Appendix A §5.1:
    "เหตุการณ์ผิดปกติ: reconnect, order reject, circuit breaker triggers")."""

    __tablename__ = "anomaly_event_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime]
    event_type: Mapped[str] = mapped_column(index=True)
    """One of: reconnect / order_reject / circuit_breaker_trigger /
    execution_failed / abnormal_slippage / other."""
    details: Mapped[str]


_ENGINE_CACHE: dict[str, Engine] = {}
"""One `Engine`/connection pool per resolved `db_path`, keyed by the resolved
absolute path string -- `get_engine()` is called fresh on every single
read/write throughout `store/journal.py`, and creating (and never disposing)
a brand-new `Engine` on every one of those calls is a slow connection-pool
leak in the long-running orchestrator. Keying by the RESOLVED path (rather
than the raw `db_path` argument) means a `db_path=None` call and an explicit
call using that same resolved default path share one cached engine instead
of two. No locking beyond what's already implicit in this codebase's
single-threaded MT5-access design (`common/mt5_connection.py`)."""


def get_engine(db_path: Path | str | None = None) -> Engine:
    """One SQLite engine, WAL mode enabled via a `PRAGMA` fired on every new
    DBAPI connection (SQLite has no native "enable WAL for this engine" call
    in SQLAlchemy -- this is the documented way to do it). Also ensures every
    table above exists (`CREATE TABLE IF NOT EXISTS`, idempotent) before
    returning, so callers never need a separate migration step for this
    Phase 8a schema. Cached per resolved `db_path` (see `_ENGINE_CACHE`) --
    repeated calls for the same path return the SAME `Engine` instead of
    opening a new connection pool every time.

    `db_path=None` uses `DEFAULT_DB_PATH` (`data/db/trade_journal.sqlite`,
    git-ignored per `.gitignore`'s `data/db/*` rule, same as this codebase's
    other `data/db/` state files) -- tests must always pass an explicit
    `tmp_path`-based path instead."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cache_key = str(path.resolve())
    cached_engine = _ENGINE_CACHE.get(cache_key)
    if cached_engine is not None:
        return cached_engine

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enable_wal(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(engine)
    _ENGINE_CACHE[cache_key] = engine
    return engine
