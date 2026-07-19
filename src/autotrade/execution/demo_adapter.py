"""Throttled real-demo broker adapter (Phase 3b) — places real orders
against the MT5 demo account via mt5_session() + mt5.order_send(), with a
deliberate cooldown throttle and fill reconciliation.

Opens its own `mt5_session()` per call (the session helper is reentrant per
common/mt5_connection.py, so this composes safely with any outer session an
orchestrator may hold open later) rather than expecting to be used inside an
already-open one.
"""
from __future__ import annotations

import logging
from datetime import datetime

import MetaTrader5 as mt5

from autotrade.common.clock import Clock
from autotrade.common.config import MT5Credentials
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import get_symbol_spec
from autotrade.execution.adapter import BrokerAdapter, OrderResult, TradeRequest

logger = logging.getLogger(__name__)

# Explicit, human-readable reasons for the retcodes most likely to show up
# during manual verification (spec.md §7 "Execution retcode handling").
# Anything not in this map still gets a clear message with the raw retcode.
_KNOWN_REJECT_REASONS: dict[int, str] = {
    mt5.TRADE_RETCODE_REQUOTE: "requote",
    mt5.TRADE_RETCODE_REJECT: "rejected by broker",
    mt5.TRADE_RETCODE_INVALID_STOPS: "invalid stops (too close to price, or violates TRADE_STOPS_LEVEL/FREEZE_LEVEL)",
    mt5.TRADE_RETCODE_INVALID_PRICE: "invalid price",
    mt5.TRADE_RETCODE_INVALID_VOLUME: "invalid volume",
    mt5.TRADE_RETCODE_NO_MONEY: "insufficient margin",
    mt5.TRADE_RETCODE_MARKET_CLOSED: "market closed",
    mt5.TRADE_RETCODE_TRADE_DISABLED: "trading disabled for this symbol/account",
    mt5.TRADE_RETCODE_TIMEOUT: "request timed out",
    mt5.TRADE_RETCODE_CONNECTION: "no connection to trade server",
}


class ThrottledDemoAdapter(BrokerAdapter):
    def __init__(
        self,
        creds: MT5Credentials,
        clock: Clock,
        min_seconds_between_trades: float = 300.0,
        deviation_points: int = 20,
        magic: int = 234_000,
        fill_price_tolerance_points: float = 5.0,
        symbol_map: dict[str, str] | None = None,
    ) -> None:
        self._creds = creds
        self._clock = clock
        self._min_seconds_between_trades = min_seconds_between_trades
        self._deviation_points = deviation_points
        self._magic = magic
        self._fill_price_tolerance_points = fill_price_tolerance_points
        self._symbol_map = symbol_map
        self._last_placed_at: datetime | None = None

    def place_order(self, request: TradeRequest) -> OrderResult:
        now = self._clock.now()
        if self._last_placed_at is not None:
            elapsed = (now - self._last_placed_at).total_seconds()
            if elapsed < self._min_seconds_between_trades:
                message = (
                    f"throttled: only {elapsed:.1f}s since last placement, "
                    f"minimum {self._min_seconds_between_trades}s required"
                )
                logger.warning(message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

        if request.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got {request.direction!r}")

        result = self._send_order(request)
        if result.success:
            self._last_placed_at = now
        return result

    def _send_order(self, request: TradeRequest) -> OrderResult:
        with mt5_session(self._creds):
            spec = get_symbol_spec(request.symbol, self._symbol_map)

            tick = mt5.symbol_info_tick(spec.broker_name)
            if tick is None or tick.time == 0:
                code, desc = mt5.last_error()
                message = f"symbol_info_tick({spec.broker_name!r}) failed: [{code}] {desc}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            if request.direction == "BUY":
                order_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            else:
                order_type = mt5.ORDER_TYPE_SELL
                price = tick.bid

            mt5_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": spec.broker_name,
                "volume": request.lot_size,
                "type": order_type,
                "price": price,
                "sl": request.stop_loss,
                "tp": request.take_profit,
                "deviation": self._deviation_points,
                "magic": self._magic,
                "comment": "autotrade phase3 demo",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            send_result = mt5.order_send(mt5_request)
            if send_result is None:
                code, desc = mt5.last_error()
                message = f"order_send() returned None: [{code}] {desc}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            if send_result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
                reason = _KNOWN_REJECT_REASONS.get(send_result.retcode, "order rejected")
                message = (
                    f"order_send rejected ({reason}): retcode={send_result.retcode} "
                    f"comment={send_result.comment!r}"
                )
                logger.warning(message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=send_result.retcode, message=message,
                )

            self._reconcile_fill(request, send_result, spec.point)

            fill_note = "filled" if send_result.retcode == mt5.TRADE_RETCODE_DONE else "PARTIALLY filled"
            message = (
                f"order {fill_note}: ticket={send_result.order} price={send_result.price} "
                f"volume={send_result.volume}"
            )
            logger.info(message)
            return OrderResult(
                success=True,
                broker_ticket=send_result.order,
                filled_price=send_result.price,
                filled_volume=send_result.volume,
                retcode=send_result.retcode,
                message=message,
            )

    def _reconcile_fill(self, request: TradeRequest, send_result, point: float) -> None:
        """Compare the actual fill against what was requested and surface a
        WARNING on drift beyond tolerance — never silently trust the request
        matched intent (spec.md §7 "paired with fill reconciliation")."""
        if point > 0:
            price_diff_points = abs(send_result.price - request.entry) / point
            if price_diff_points > self._fill_price_tolerance_points:
                logger.warning(
                    "Fill price %.5f differs from requested entry %.5f by %.1f points "
                    "(tolerance %.1f) for %s",
                    send_result.price, request.entry, price_diff_points,
                    self._fill_price_tolerance_points, request.symbol,
                )

        if abs(send_result.volume - request.lot_size) > 1e-9:
            logger.warning(
                "Filled volume %.2f differs from requested lot_size %.2f for %s",
                send_result.volume, request.lot_size, request.symbol,
            )

    def get_equity(self) -> float:
        with mt5_session(self._creds):
            account = mt5.account_info()
            if account is None:
                code, desc = mt5.last_error()
                raise RuntimeError(f"account_info() failed: [{code}] {desc}")
            return account.equity
