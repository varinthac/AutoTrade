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


@dataclass(frozen=True)
class BrokerPosition:
    """An open position, as seen from execution/'s side of the fence.
    Deliberately shaped the same as `shield.checkpoint.OpenPositionInfo`
    (symbol/direction/risk_pct) but defined independently here rather than
    imported from shield/ -- same "define our own plain dataclass instead of
    importing an upstream module's" pattern this file already uses for
    `TradeRequest` vs. `council.order_construction.OrderPlan`. The caller
    (orchestrator/) converts one into the other."""

    symbol: str
    direction: Literal["BUY", "SELL"]
    risk_pct: float


class BrokerAdapter(ABC):
    """Minimal interface every execution backend implements. Kept deliberately
    small — close_position/modify_stop_loss belong to the Watchman phase,
    added only when actually needed."""

    @abstractmethod
    def place_order(self, request: TradeRequest) -> OrderResult:
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
        portfolio-level checks (shield/checkpoint.py rules 2-5)."""
        ...
