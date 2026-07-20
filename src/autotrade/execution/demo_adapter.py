"""Throttled real-demo broker adapter (Phase 3b, extended in Phase 7b) —
places real orders against the MT5 demo account via mt5_session() +
mt5.order_send(), with a deliberate cooldown throttle, fill reconciliation,
and (Phase 7b, trading_system_summary_v2.md Appendix A §4.8) execution-error
handling: reject/requote retry, broker-stop-level SL clamping, partial-fill
signalling, and an abnormal-slippage close.

Opens its own `mt5_session()` per call (the session helper is reentrant per
common/mt5_connection.py, so this composes safely with any outer session an
orchestrator may hold open later) rather than expecting to be used inside an
already-open one.

**Retry design (Appendix A §4.8, item 1):** every `mt5.order_send()` call in
this module (place/modify/close) goes through `_send_with_retry`, which
retries up to `max_retries` times, `retry_delay_sec` apart -- and NEVER
varies the request between attempts (no price-chasing: the exact same
price/sl/tp/volume is resent every time). The delay is issued via an injected
`sleep_fn` (defaults to `time.sleep`) rather than a bare `time.sleep()` call,
so tests can supply a no-op and assert on call counts/timing without actually
blocking -- same "injectable seam instead of a direct OS call" convention as
`common/clock.py` applies to wall-clock reads.

**Retry eligibility is NOT uniform across every outcome.** A rejected/
requoted retcode (`TRADE_RETCODE_REJECT`/`TRADE_RETCODE_REQUOTE`) is always
safe to retry -- the broker explicitly told us the order did NOT execute.
A structurally-terminal retcode (`NO_MONEY`/`MARKET_CLOSED`/`TRADE_DISABLED`/
`INVALID_VOLUME`/`INVALID_STOPS`) is NEVER retried, for any action -- retrying
cannot possibly fix it, so retrying would only burn the full retry-delay
window for nothing. An AMBIGUOUS outcome (`order_send()` returning `None`, or
`TRADE_RETCODE_TIMEOUT`/`TRADE_RETCODE_CONNECTION`) means we genuinely don't
know whether the broker executed the order -- for a non-idempotent
`TRADE_ACTION_DEAL` (`place_order`'s new-position open, `close_position`'s
close), blindly resending risks a DOUBLE FILL/double-close, so these are
treated as immediately terminal instead of retried. `modify_stop_loss`'s
`TRADE_ACTION_SLTP` is idempotent (resending the same SL is harmless even if
the first attempt secretly succeeded), so it's the one call site that opts
into retrying ambiguous outcomes too, via `_send_with_retry`'s
`is_idempotent` parameter.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Literal

import MetaTrader5 as mt5

from autotrade.common.clock import Clock
from autotrade.common.config import MT5Credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import get_symbol_spec
from autotrade.execution.adapter import BrokerAdapter, BrokerPosition, OrderResult, TradeRequest

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

_DONE_RETCODES = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)

# Genuinely transient: the broker explicitly told us the order did NOT
# execute -- always safe to retry, for any action.
_ALWAYS_RETRYABLE_RETCODES = frozenset({mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_REJECT})

# Structurally terminal: retrying the byte-identical request can NEVER
# succeed -- never retried, for any action (see module docstring).
_TERMINAL_RETCODES = frozenset({
    mt5.TRADE_RETCODE_NO_MONEY,
    mt5.TRADE_RETCODE_MARKET_CLOSED,
    mt5.TRADE_RETCODE_TRADE_DISABLED,
    mt5.TRADE_RETCODE_INVALID_VOLUME,
    mt5.TRADE_RETCODE_INVALID_STOPS,
})

# Ambiguous: we don't know whether the broker actually executed the order --
# only safe to retry for an idempotent action (see module docstring).
_AMBIGUOUS_RETCODES = frozenset({mt5.TRADE_RETCODE_TIMEOUT, mt5.TRADE_RETCODE_CONNECTION})


def _is_retryable_outcome(send_result, is_idempotent: bool) -> bool:
    """Whether `_send_with_retry` should attempt another `order_send()` for
    this outcome -- `send_result` is `None` (order_send() itself returned
    `None`, the most ambiguous possible outcome) or a raw MT5 result with a
    non-DONE retcode. `is_idempotent` distinguishes a `TRADE_ACTION_SLTP`
    modify (safe to retry even on an ambiguous outcome) from a
    `TRADE_ACTION_DEAL` place/close (NOT safe -- double-fill/double-close
    risk, see module docstring)."""
    if send_result is None:
        return is_idempotent
    if send_result.retcode in _ALWAYS_RETRYABLE_RETCODES:
        return True
    if send_result.retcode in _AMBIGUOUS_RETCODES:
        return is_idempotent
    return False


def _clamp_to_stops_level(
    direction: Literal["BUY", "SELL"], current_price: float, requested_sl: float,
    stops_level: int, point: float,
) -> float:
    """Clamp `requested_sl` to the closest distance-from-price the broker
    will actually accept (`SYMBOL_TRADE_STOPS_LEVEL`, in points) -- Appendix
    A §4.8's broker-stop-level handling. A BUY's SL must sit at least
    `stops_level` points below `current_price`; a SELL's SL must sit at
    least `stops_level` points above it. Only ever moves the SL FURTHER from
    price to satisfy the minimum distance -- never closer than requested."""
    if stops_level <= 0 or point <= 0:
        return requested_sl
    min_distance = stops_level * point
    if direction == "BUY":
        max_allowed_sl = current_price - min_distance
        return min(requested_sl, max_allowed_sl)
    else:
        min_allowed_sl = current_price + min_distance
        return max(requested_sl, min_allowed_sl)


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
        max_retries: int = 2,
        retry_delay_sec: float = 3.0,
        sleep_fn: Callable[[float], None] | None = None,
        max_entry_slippage_atr: float = 0.3,
        min_rr_after_slippage: float = 1.3,
    ) -> None:
        self._creds = creds
        self._clock = clock
        self._min_seconds_between_trades = min_seconds_between_trades
        self._deviation_points = deviation_points
        self._magic = magic
        self._fill_price_tolerance_points = fill_price_tolerance_points
        self._symbol_map = symbol_map
        self._max_retries = max_retries
        self._retry_delay_sec = retry_delay_sec
        self._sleep_fn = sleep_fn or time.sleep
        self._max_entry_slippage_atr = max_entry_slippage_atr
        self._min_rr_after_slippage = min_rr_after_slippage
        self._last_placed_at: datetime | None = None

    def place_order(self, request: TradeRequest, current_atr: float | None = None) -> OrderResult:
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

        result = self._send_order(request, current_atr)
        if result.success or result.closed_due_to_slippage:
            # An abnormal-slippage close still consumed the throttle window
            # -- a real order was sent and filled, just then immediately
            # unwound -- so it must not be possible to place() again within
            # the same cooldown.
            self._last_placed_at = now
        return result

    def _send_order(self, request: TradeRequest, current_atr: float | None) -> OrderResult:
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

            send_result = self._send_with_retry(mt5_request, f"place_order({request.symbol} {request.direction})")

            if send_result is None:
                code, desc = mt5.last_error()
                message = f"order_send() returned None after {self._max_retries} retries: [{code}] {desc}"
                logger.error("execution_failed: %s", message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            if send_result.retcode not in _DONE_RETCODES:
                reason = _KNOWN_REJECT_REASONS.get(send_result.retcode, "order rejected")
                message = (
                    f"order_send rejected ({reason}) after {self._max_retries} retries: "
                    f"retcode={send_result.retcode} comment={send_result.comment!r}"
                )
                logger.error("execution_failed: %s", message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=send_result.retcode, message=message,
                )

            partial_fill = abs(send_result.volume - request.lot_size) > 1e-9
            abnormal_slippage = self._reconcile_fill(request, send_result, spec.point, current_atr)

            if abnormal_slippage:
                price_diff = abs(send_result.price - request.entry)
                logger.warning(
                    "abnormal_slippage: ticket=%s %s filled at %.5f, intended entry %.5f -- "
                    "drift %.5f > %.2fx ATR (%.5f)",
                    send_result.order, request.symbol, send_result.price, request.entry,
                    price_diff, self._max_entry_slippage_atr, current_atr,
                )
                realized_rr = _realized_rr(request, send_result.price)
                if realized_rr is not None and realized_rr < self._min_rr_after_slippage:
                    logger.error(
                        "abnormal_slippage: ticket=%s %s realized R:R %.2f < floor %.2f after slippage -- "
                        "closing position immediately",
                        send_result.order, request.symbol, realized_rr, self._min_rr_after_slippage,
                    )
                    close_result = self.close_position(send_result.order)
                    if not close_result.success:
                        logger.warning(
                            "abnormal_slippage: ticket=%s %s immediate close attempt failed (%s) -- "
                            "retrying the close once more",
                            send_result.order, request.symbol, close_result.message,
                        )
                        close_result = self.close_position(send_result.order)

                    if close_result.success:
                        message = (
                            f"abnormal_slippage: filled at {send_result.price} (intended entry "
                            f"{request.entry}), realized R:R {realized_rr:.2f} < floor "
                            f"{self._min_rr_after_slippage} -- position closed immediately "
                            f"({close_result.message})"
                        )
                        logger.error(message)
                        return OrderResult(
                            success=False, broker_ticket=send_result.order, filled_price=send_result.price,
                            filled_volume=send_result.volume, retcode=send_result.retcode, message=message,
                            partial_fill=partial_fill, closed_due_to_slippage=True,
                        )

                    message = (
                        f"abnormal_slippage: filled at {send_result.price} (intended entry {request.entry}), "
                        f"realized R:R {realized_rr:.2f} < floor {self._min_rr_after_slippage} -- "
                        f"CLOSE FAILED even after a retry ({close_result.message}) -- ticket="
                        f"{send_result.order} {request.symbol} is a REAL OPEN position with bad "
                        f"post-slippage R:R that this immediate-close attempt could NOT unwind"
                    )
                    logger.critical(
                        "abnormal_slippage_close_failed: ticket=%s %s realized R:R %.2f < floor %.2f, "
                        "and the immediate protective close FAILED after a retry (%s) -- a REAL, OPEN "
                        "position now exists on the broker with bad post-slippage R:R and is NOT "
                        "currently being managed. It will be picked up and re-evaluated by the next "
                        "Watchman cycle via get_open_positions() -- verify manually if that is not "
                        "acceptable to wait for.",
                        send_result.order, request.symbol, realized_rr, self._min_rr_after_slippage,
                        close_result.message,
                    )
                    return OrderResult(
                        success=False, broker_ticket=send_result.order, filled_price=send_result.price,
                        filled_volume=send_result.volume, retcode=send_result.retcode, message=message,
                        partial_fill=partial_fill, closed_due_to_slippage=False, position_still_open=True,
                    )

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
                partial_fill=partial_fill,
            )

    def _send_with_retry(self, mt5_request: dict, context: str, is_idempotent: bool = False):
        """Send `mt5_request` via `mt5.order_send()`, retrying up to
        `self._max_retries` times (`self._retry_delay_sec` apart, via
        `self._sleep_fn`) -- but ONLY when `_is_retryable_outcome` says the
        outcome is actually safe/worth retrying (Appendix A §4.8's
        execution-error handling, tightened per the module docstring's
        retry-eligibility note). NEVER varies the request between attempts
        (no price-chasing with repeated market orders): every retry resends
        the exact same `mt5_request` dict built once by the caller. Returns
        the LAST attempt's raw result (a rejected/ambiguous result, `None`,
        or a genuine success) -- the caller applies its usual success/
        failure interpretation to whatever comes back, same as an
        un-retried call. `is_idempotent=True` (only `modify_stop_loss`'s
        `TRADE_ACTION_SLTP`) additionally allows retrying an ambiguous
        outcome (`None`/`TIMEOUT`/`CONNECTION`) -- unsafe for `place_order`/
        `close_position`'s non-idempotent `TRADE_ACTION_DEAL`, so those stay
        at the default `is_idempotent=False`."""
        attempts = self._max_retries + 1
        send_result = None
        for attempt in range(1, attempts + 1):
            send_result = mt5.order_send(mt5_request)
            if send_result is not None and send_result.retcode in _DONE_RETCODES:
                return send_result

            retryable = _is_retryable_outcome(send_result, is_idempotent)

            if send_result is None:
                code, desc = mt5.last_error()
                logger.warning(
                    "%s: order_send() returned None on attempt %d/%d: [%d] %s%s",
                    context, attempt, attempts, code, desc,
                    "" if retryable else " -- ambiguous outcome on a non-idempotent action, "
                    "NOT retrying (double-fill/double-close risk)",
                )
            else:
                reason = _KNOWN_REJECT_REASONS.get(send_result.retcode, "order rejected")
                logger.warning(
                    "%s: order_send rejected (%s) on attempt %d/%d: retcode=%s comment=%r%s",
                    context, reason, attempt, attempts, send_result.retcode, send_result.comment,
                    "" if retryable else " -- not retrying (structurally terminal, or an ambiguous "
                    "outcome unsafe to retry on this action)",
                )

            if not retryable:
                return send_result

            if attempt < attempts:
                self._sleep_fn(self._retry_delay_sec)

        return send_result

    def _reconcile_fill(
        self, request: TradeRequest, send_result, point: float, current_atr: float | None,
    ) -> bool:
        """Compare the actual fill against what was requested and surface a
        WARNING on drift beyond tolerance — never silently trust the request
        matched intent (spec.md §7 "paired with fill reconciliation").
        Returns True if the fill also counts as Appendix A §4.8's
        `abnormal_slippage` (entry slippage beyond `max_entry_slippage_atr`
        x ATR from the intended entry) -- the caller decides whether that
        also breaches the post-slippage R:R floor and needs an immediate
        close. Always False when `current_atr` is not supplied (no ATR, no
        way to evaluate the threshold)."""
        price_diff = abs(send_result.price - request.entry)

        if point > 0:
            price_diff_points = price_diff / point
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

        if current_atr is None or current_atr <= 0:
            return False
        return price_diff > self._max_entry_slippage_atr * current_atr

    def modify_stop_loss(self, ticket: int, new_stop_loss: float) -> OrderResult:
        with mt5_session(self._creds):
            position = self._get_position(ticket)
            if position is None:
                message = f"modify_stop_loss: no open position found for ticket={ticket}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            info = mt5.symbol_info(position.symbol)
            if info is None:
                code, desc = mt5.last_error()
                message = f"symbol_info({position.symbol!r}) failed while modifying SL: [{code}] {desc}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            direction: Literal["BUY", "SELL"] = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
            clamped_sl = _clamp_to_stops_level(
                direction=direction, current_price=position.price_current, requested_sl=new_stop_loss,
                stops_level=info.trade_stops_level, point=info.point,
            )
            if abs(clamped_sl - new_stop_loss) > 1e-9:
                logger.warning(
                    "modify_stop_loss: ticket=%s requested SL %.5f violates broker stops level "
                    "(%d points @ %.5f) -- clamped to %.5f (deviated %.5f from the intended plan)",
                    ticket, new_stop_loss, info.trade_stops_level, info.point, clamped_sl,
                    abs(clamped_sl - new_stop_loss),
                )

            mt5_request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": position.symbol,
                "sl": clamped_sl,
                "tp": position.tp,
                "magic": self._magic,
            }

            send_result = self._send_with_retry(
                mt5_request, f"modify_stop_loss(ticket={ticket})", is_idempotent=True,
            )
            if send_result is None or send_result.retcode not in _DONE_RETCODES:
                if send_result is None:
                    code, desc = mt5.last_error()
                    message = (
                        f"modify_stop_loss(ticket={ticket}) failed after {self._max_retries} retries: "
                        f"order_send() returned None: [{code}] {desc}"
                    )
                    retcode = None
                else:
                    reason = _KNOWN_REJECT_REASONS.get(send_result.retcode, "order rejected")
                    message = (
                        f"modify_stop_loss(ticket={ticket}) failed after {self._max_retries} retries "
                        f"({reason}): retcode={send_result.retcode} comment={send_result.comment!r}"
                    )
                    retcode = send_result.retcode
                logger.error("execution_failed: %s", message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=retcode, message=message,
                )

            message = f"SL modified: ticket={ticket} new_sl={clamped_sl}"
            logger.info(message)
            return OrderResult(
                success=True, broker_ticket=ticket, filled_price=clamped_sl,
                filled_volume=None, retcode=send_result.retcode, message=message,
            )

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        with mt5_session(self._creds):
            position = self._get_position(ticket)
            if position is None:
                message = f"close_position: no open position found for ticket={ticket}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            close_volume = position.volume if volume is None else volume
            if close_volume <= 0 or close_volume > position.volume + 1e-9:
                message = (
                    f"close_position: requested volume {close_volume} invalid for ticket={ticket} "
                    f"(open volume={position.volume})"
                )
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )

            order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY

            tick = mt5.symbol_info_tick(position.symbol)
            if tick is None or tick.time == 0:
                code, desc = mt5.last_error()
                message = f"symbol_info_tick({position.symbol!r}) failed while closing: [{code}] {desc}"
                logger.error(message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=None, message=message,
                )
            price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

            mt5_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": ticket,
                "symbol": position.symbol,
                "volume": close_volume,
                "type": order_type,
                "price": price,
                "deviation": self._deviation_points,
                "magic": self._magic,
                "comment": "autotrade watchman close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            send_result = self._send_with_retry(mt5_request, f"close_position(ticket={ticket})")
            if send_result is None or send_result.retcode not in _DONE_RETCODES:
                if send_result is None:
                    code, desc = mt5.last_error()
                    message = (
                        f"close_position(ticket={ticket}) failed after {self._max_retries} retries: "
                        f"order_send() returned None: [{code}] {desc}"
                    )
                    retcode = None
                else:
                    reason = _KNOWN_REJECT_REASONS.get(send_result.retcode, "order rejected")
                    message = (
                        f"close_position(ticket={ticket}) failed after {self._max_retries} retries "
                        f"({reason}): retcode={send_result.retcode} comment={send_result.comment!r}"
                    )
                    retcode = send_result.retcode
                logger.error("execution_failed: %s", message)
                return OrderResult(
                    success=False, broker_ticket=ticket, filled_price=None,
                    filled_volume=None, retcode=retcode, message=message,
                )

            message = f"closed volume={send_result.volume} at price={send_result.price}"
            logger.info("CLOSED ticket=%s %s", ticket, message)
            return OrderResult(
                success=True, broker_ticket=ticket, filled_price=send_result.price,
                filled_volume=send_result.volume, retcode=send_result.retcode, message=message,
            )

    def _get_position(self, ticket: int):
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return None
        return positions[0]

    def get_equity(self) -> float:
        with mt5_session(self._creds):
            account = mt5.account_info()
            if account is None:
                code, desc = mt5.last_error()
                raise RuntimeError(f"account_info() failed: [{code}] {desc}")
            return account.equity

    def get_balance(self) -> float:
        with mt5_session(self._creds):
            account = mt5.account_info()
            if account is None:
                code, desc = mt5.last_error()
                raise RuntimeError(f"account_info() failed: [{code}] {desc}")
            return account.balance

    def get_open_positions(self) -> list[BrokerPosition]:
        """Every open position on the account (not filtered to this
        adapter's own `magic` -- Shield's portfolio checks need the full
        picture of what's actually exposed, including anything opened
        manually or by another process).

        `risk_pct` is a pragmatic APPROXIMATION, not the position's
        originally-intended risk at open time: MT5 doesn't retain that, so
        this instead uses the position's CURRENT distance-to-stop and
        CURRENT equity --
            risk_pct = |current_price - sl| * point_value * volume / equity * 100
        which drifts from the true "at risk" fraction as price moves toward
        or away from the stop, or as equity changes. Good enough for
        Shield's rule 5 ceiling check; not a substitute for a real
        per-position risk ledger.
        """
        with mt5_session(self._creds):
            positions = mt5.positions_get()
            if positions is None:
                code, desc = mt5.last_error()
                raise RuntimeError(f"positions_get() failed: [{code}] {desc}")

            account = mt5.account_info()
            if account is None:
                code, desc = mt5.last_error()
                raise RuntimeError(f"account_info() failed: [{code}] {desc}")
            equity = account.equity

            symbol_map = self._symbol_map or load_yaml_config("base")["symbols"]
            broker_to_canonical = {broker: canonical for canonical, broker in symbol_map.items()}

            result: list[BrokerPosition] = []
            for pos in positions:
                canonical = broker_to_canonical.get(pos.symbol)
                if canonical is None:
                    logger.warning(
                        "get_open_positions(): broker symbol %r has no canonical mapping in "
                        "config/base.yaml symbols -- skipping (Shield needs canonical names)",
                        pos.symbol,
                    )
                    continue

                info = mt5.symbol_info(pos.symbol)
                if info is None or info.trade_tick_size <= 0:
                    logger.warning(
                        "get_open_positions(): symbol_info(%r) unavailable/invalid -- "
                        "skipping this position", pos.symbol,
                    )
                    continue
                point_value = info.trade_tick_value / info.trade_tick_size

                if pos.sl == 0:
                    logger.warning(
                        "get_open_positions(): position %s on %s has no stop-loss set -- "
                        "treating as unbounded risk, will force Shield's risk ceiling to "
                        "block new entries",
                        pos.ticket, pos.symbol,
                    )
                    risk_pct = float("inf")
                elif equity <= 0:
                    logger.warning(
                        "get_open_positions(): position %s on %s -- equity is non-positive, "
                        "risk_pct cannot be computed, treating as 0.0",
                        pos.ticket, pos.symbol,
                    )
                    risk_pct = 0.0
                else:
                    distance = abs(pos.price_current - pos.sl)
                    risk_pct = distance * point_value * pos.volume / equity * 100

                direction: Literal["BUY", "SELL"] = (
                    "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                )
                result.append(BrokerPosition(
                    ticket=pos.ticket, symbol=canonical, direction=direction, risk_pct=risk_pct,
                    current_sl=pos.sl, current_price=pos.price_current, volume=pos.volume,
                ))

            return result


def _realized_rr(request: TradeRequest, fill_price: float) -> float | None:
    """R:R measured from the ACTUAL fill price against the unchanged
    (already-sent) stop-loss/take-profit -- Appendix A §4.8's post-slippage
    R:R check. `None` if risk is zero/undefined (can't divide)."""
    risk = abs(fill_price - request.stop_loss)
    if risk <= 0:
        return None
    reward = abs(request.take_profit - fill_price)
    return reward / risk
