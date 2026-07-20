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

    def place_order(self, request: TradeRequest) -> OrderResult:
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

    def get_equity(self) -> float:
        return self._fixed_equity

    def get_balance(self) -> float:
        # No floating P&L in a dry run -- same fixed value as get_equity().
        return self._fixed_equity

    def get_open_positions(self) -> list[BrokerPosition]:
        # A dry run never actually opens a position with any broker.
        return []
