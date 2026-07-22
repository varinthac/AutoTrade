"""BrokerAdapter interface — the seam between the (upstream) decision
pipeline and the (downstream) broker, per spec.md §2.1 / §2.3.

Per the dependency-direction invariant, this module defines its own plain
dataclasses rather than importing `council.order_construction.OrderPlan` or
anything risk/-specific — a caller combines an `OrderPlan` + CFO lot size
into a `TradeRequest` itself, keeping execution/ reusable/testable
independent of upstream modules.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    direction: Literal["BUY", "SELL"]
    lot_size: float
    entry: float
    stop_loss: float
    take_profit: float


@dataclass(frozen=True)
class OrderResult:
    success: bool
    broker_ticket: int | None
    filled_price: float | None
    filled_volume: float | None
    retcode: int | None
    message: str
    # Phase 7b (Appendix A §4.8) additions -- both default so every
    # pre-existing OrderResult(...) construction (place_order's own success/
    # failure paths, NoOpBrokerAdapter, every test fixture) keeps working
    # unmodified; only place_order()'s partial-fill/abnormal-slippage paths
    # and the new modify_stop_loss()/close_position() methods set these
    # explicitly.
    partial_fill: bool = False
    closed_due_to_slippage: bool = False
    # Phase 7b bugfix: set True only on the specific compounding-failure path
    # where an abnormal-slippage close was attempted (and retried) but never
    # actually succeeded -- the entry fill DID go through, so `broker_ticket`
    # is a REAL open position on the broker, even though `success` is False
    # and `closed_due_to_slippage` is False (it was never actually closed).
    # Callers (orchestrator/shadow_loop.py) must still record this position's
    # Watchman metadata when this is True, same as a normal success, or it
    # becomes permanently invisible to Watchman.
    position_still_open: bool = False


@dataclass(frozen=True)
class BrokerPosition:
    """An open position, as seen from execution/'s side of the fence.
    Started out (Phase 3) shaped the same as `shield.checkpoint.OpenPositionInfo`
    (symbol/direction/risk_pct); Phase 7b adds `ticket`/`current_sl`/
    `current_price`/`volume` -- Watchman's per-position loop
    (`watchman/loop.py`) needs the broker ticket to look up
    `PositionMetadata`/call `modify_stop_loss`/`close_position`, and the
    position's live SL/price/volume to feed `evaluate_watchman`/
    `news_protection` without a second MT5 round-trip. Defined independently
    here rather than imported from shield/ -- same "define our own plain
    dataclass instead of importing an upstream module's" pattern this file
    already uses for `TradeRequest` vs. `council.order_construction.OrderPlan`.
    The caller (orchestrator/) converts this into whatever shape each
    downstream module (Shield, Watchman) actually needs."""

    ticket: int
    symbol: str
    direction: Literal["BUY", "SELL"]
    risk_pct: float
    current_sl: float
    current_price: float
    volume: float


@dataclass(frozen=True)
class ClosedTradeInfo:
    """Ground-truth close details for a position that is no longer open,
    read from MT5's own trade history -- `watchman/loop.py`'s
    reconciliation path uses this to build a trade-journal record for a
    position that closed WITHOUT this system ever calling `close_position()`
    itself (overwhelmingly a broker-side hard SL/TP hit).

    `close_price`/`close_time`/`closed_volume` come from the LAST exit deal
    chronologically (the one that finished closing the position).
    `gross_pnl`/`cost` are summed across EVERY deal belonging to this
    position (the entry deal and every partial/full exit deal), so a
    position that had an earlier partial close (e.g. Watchman's
    news-protection half-close) still gets its FULL lifetime P&L captured
    in one record here. `cost` is commission + swap combined, as a POSITIVE
    number to subtract (mirrors `store/models.py`'s `TradeRecord.cost`
    convention) -- MT5 reports both as negative-when-charged, so `cost` is
    `-(sum of commission + swap)`.

    `exit_reason` is derived from the closing deal's own MT5 `reason` field
    (`DEAL_REASON_SL`/`DEAL_REASON_TP` for a genuine broker-side stop/target
    hit; client/mobile/web-initiated maps to `"manual"`; expert-initiated
    (script/EA/API) maps to `"reconciled_system_close"` if it carries THIS
    adapter's own `magic` number -- i.e. this system's own close, most likely
    one whose acknowledgment was lost -- or to `"unknown"` (with a loud
    warning) if it doesn't, since that means some OTHER script/EA touched the
    account; a stop-out margin call or anything else unrecognized also maps
    to `"unknown"` -- see `execution/demo_adapter.py`'s `get_closed_trade_info`
    for the exact mapping)."""

    close_price: float
    close_time: datetime
    closed_volume: float
    gross_pnl: float
    cost: float
    exit_reason: Literal["stop_loss", "take_profit", "manual", "reconciled_system_close", "unknown"]


class BrokerAdapter(ABC):
    """Minimal interface every execution backend implements."""

    @abstractmethod
    def place_order(self, request: TradeRequest, current_atr: float | None = None) -> OrderResult:
        """Open a new position. `current_atr` is optional (Phase 7b,
        Appendix A §4.8's abnormal-slippage check) -- when given, a fill
        that lands beyond `max_entry_slippage_atr` x `current_atr` from
        `request.entry` is logged as `abnormal_slippage`; if the resulting
        realized R:R (measured from the ACTUAL fill price against the
        unchanged sl/tp) then drops below the configured floor, the position
        is closed immediately and this returns `success=False`,
        `closed_due_to_slippage=True` rather than a normal success. If that
        immediate close itself fails (retried once, still fails), this
        instead returns `success=False`, `closed_due_to_slippage=False`,
        `position_still_open=True` -- the entry fill genuinely went through
        and `broker_ticket` is a REAL open position the caller must still
        record for Watchman, even though the intended slippage-close did
        not succeed. Passing `None` (the default) skips this check entirely
        -- callers with no ATR handy (e.g. NoOpBrokerAdapter) simply never
        trigger it.
        `OrderResult.partial_fill` is True whenever the actual filled volume
        differs from `request.lot_size` -- callers must use
        `OrderResult.filled_volume` (the ACTUAL filled amount) for any
        downstream risk/position-sizing bookkeeping, never `request.lot_size`,
        and must NEVER place a second order to top up the difference (Appendix
        A §4.8's explicit "ห้ามยิงเพิ่ม")."""
        ...

    @abstractmethod
    def modify_stop_loss(self, ticket: int, new_stop_loss: float) -> OrderResult:
        """Move `ticket`'s stop-loss to `new_stop_loss`. If the broker
        rejects it for being closer to the current price than
        `SYMBOL_TRADE_STOPS_LEVEL` allows, the SL is instead set at the
        closest distance the broker will actually accept (Appendix A §4.8)
        -- `OrderResult.filled_price` always carries the ACTUAL stop-loss
        applied (which may differ from `new_stop_loss` for exactly this
        reason); `filled_volume` is unused (`None`)."""
        ...

    @abstractmethod
    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        """Close `ticket` at market -- the full position if `volume` is
        `None`, otherwise a partial close of exactly `volume` lots (used by
        Watchman's news protection to close half). `OrderResult.filled_price`
        is the actual close price, `filled_volume` the actual volume closed."""
        ...

    @abstractmethod
    def get_equity(self) -> float:
        ...

    @abstractmethod
    def get_balance(self) -> float:
        """Account balance, excluding any unrealized P&L of open positions
        (unlike get_equity()) -- callers use `equity - balance` as a floating
        P&L proxy (risk/circuit_breaker.py's daily-loss gate)."""
        ...

    @abstractmethod
    def get_open_positions(self) -> list[BrokerPosition]:
        """Every currently-open position on the account, for Shield's
        portfolio-level checks (shield/checkpoint.py rules 2-5) and
        Watchman's per-position loop (watchman/loop.py)."""
        ...

    @abstractmethod
    def get_closed_trade_info(self, ticket: int) -> ClosedTradeInfo | None:
        """Ground-truth close details for `ticket` (a position ticket, same
        numbering as `BrokerPosition.ticket`) that is no longer open -- used
        by `watchman/loop.py`'s reconciliation path (see `ClosedTradeInfo`'s
        docstring). Returns `None` if no closing deal is found yet (e.g. a
        brief lag between the position disappearing from
        `get_open_positions()` and its closing deal landing in history) --
        callers must treat this as "retry next cycle", never as "this
        position never closed"."""
        ...
