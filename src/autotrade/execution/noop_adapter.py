"""NoOp broker adapter (Phase 3a) — never touches MT5. Exists purely so the
rest of the pipeline can be exercised with zero broker risk while it's being
wired up and debugged (see spec.md §6 Phase 3)."""
from __future__ import annotations

import logging

from autotrade.execution.adapter import BrokerAdapter, BrokerPosition, OrderResult, TradeRequest

logger = logging.getLogger(__name__)


class NoOpBrokerAdapter(BrokerAdapter):
    def __init__(self, fixed_equity: float = 10_000.0) -> None:
        self._fixed_equity = fixed_equity

    def place_order(self, request: TradeRequest, current_atr: float | None = None) -> OrderResult:
        logger.info(
            "NoOp dry-run order: %s %s lots=%s entry=%s sl=%s tp=%s",
            request.direction, request.symbol, request.lot_size,
            request.entry, request.stop_loss, request.take_profit,
        )
        return OrderResult(
            success=True,
            broker_ticket=None,
            filled_price=request.entry,
            filled_volume=request.lot_size,
            retcode=None,
            message="dry run: no order sent to any broker",
        )

    def modify_stop_loss(self, ticket: int, new_stop_loss: float) -> OrderResult:
        logger.info("NoOp dry-run modify SL: ticket=%s new_sl=%s", ticket, new_stop_loss)
        return OrderResult(
            success=True,
            broker_ticket=ticket,
            filled_price=new_stop_loss,
            filled_volume=None,
            retcode=None,
            message="dry run: no modify sent to any broker",
        )

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        logger.info("NoOp dry-run close: ticket=%s volume=%s", ticket, volume if volume is not None else "ALL")
        return OrderResult(
            success=True,
            broker_ticket=ticket,
            filled_price=None,
            filled_volume=volume,
            retcode=None,
            message="dry run: no close sent to any broker",
        )

    def get_equity(self) -> float:
        return self._fixed_equity

    def get_balance(self) -> float:
        # No floating P&L in a dry run -- same fixed value as get_equity().
        return self._fixed_equity

    def get_open_positions(self) -> list[BrokerPosition]:
        # A dry run never actually opens a position with any broker.
        return []
