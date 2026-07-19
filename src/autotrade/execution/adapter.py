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
