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

**`close_position`'s ambiguity is resolved by checking ground truth, not by
blindly retrying.** Not retrying an ambiguous outcome avoids a double-close,
but naively reporting it as a failure is also wrong if the broker actually
executed the close and only the acknowledgment was lost -- a real incident
this exists to fix: a TIMEOUT on a genuinely-successful close was reported as
`execution_failed`, and the position sat mislabeled until periodic
reconciliation caught up minutes later. So before `close_position` declares
failure on a non-DONE outcome, `_resolve_ambiguous_close` re-queries the
position directly: if it's now gone (full close) or reduced by exactly the
requested amount (partial close), the close is treated as a genuine success
whose acknowledgment was lost (logged at INFO, not as an anomaly/ERROR) --
otherwise (still fully open, or the re-query itself fails) it falls back to
the existing conservative failure reporting.

**`place_order`'s equivalent ambiguity is resolved similarly, but is a
strictly harder disambiguation problem.** Unlike `close_position` (which has
a known ticket to re-query directly -- its target position's absence/
reduction is a direct signal), `place_order` has no ticket yet: a brand-new
position merely *existing* is much weaker proof it came from THIS specific
request. `_resolve_ambiguous_place` only runs when `_is_ambiguous_outcome`
says the outcome is genuinely ambiguous (TIMEOUT/CONNECTION/`order_send()`
returning `None`) -- never for a clean structural rejection (e.g.
`TRADE_RETCODE_TRADE_DISABLED`, `INVALID_STOPS`) where the broker has already
told us definitively that no order was placed. It then re-queries every open
position on the request's symbol and keeps only those matching this
adapter's own `magic`, the requested direction, a volume no larger than
requested (a genuine partial fill counts too), and opened within a small
clock-skew buffer either side of "now". Exactly one match is treated as a
genuine success whose acknowledgment was lost -- routed back through the
SAME fill-reconciliation/abnormal-slippage logic a normal `order_send()`
success goes through, not returned early -- same protection as the close-side
fix. Zero matches means the order genuinely failed (unchanged behavior). More
than one match means there is no ticket to anchor on and no safe way to guess
which position is "ours" (e.g. a stray manual trade or a genuine concurrent
duplicate signal sharing the same symbol/magic/direction/volume) -- this
deliberately prefers a false failure (a real fill reported as REJECTED,
caught by the next Watchman reconciliation cycle) over a false success
(crediting the wrong ticket with this request's entry-time risk context). A
ticket already attributed by an earlier `_resolve_ambiguous_place` call on
this same adapter instance is never matched again, so two ambiguous timeouts
close together in time (e.g. a retried signal) cannot double-book the same
real position as two logical fills.

**Distinguishing this system's own closes from genuine manual ones.** MT5
tags a human closing via terminal/mobile/web as `DEAL_REASON_CLIENT`/
`MOBILE`/`WEB`, but tags any script/EA/API call -- including this adapter's
own `order_send()` -- as `DEAL_REASON_EXPERT`, the same as a human never
touched it. `get_closed_trade_info` disambiguates an EXPERT-reasoned close
using the closing deal's own `magic` number against `self._magic` (every
request this adapter sends is tagged with it): a match means this system's
own close (most likely the ack-loss scenario above reaching reconciliation
instead of the normal explicit-close path), classified as
`"reconciled_system_close"`; a mismatch means some OTHER script/EA touched
the account, classified as `"unknown"` with a loud warning. Genuine
`DEAL_REASON_CLIENT`/`MOBILE`/`WEB` deals stay `"manual"`.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

import MetaTrader5 as mt5

from autotrade.common.clock import Clock
from autotrade.common.config import MT5Credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbols import SymbolSpec, get_symbol_spec
from autotrade.execution.adapter import (
    BrokerAdapter,
    BrokerPosition,
    ClosedTradeInfo,
    OrderResult,
    TradeRequest,
)
from autotrade.notify.telegram import notify
from autotrade.risk.circuit_breaker import CircuitBreaker
from autotrade.store import journal

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

# `_resolve_ambiguous_place`'s re-query window tolerance: this adapter's own
# `self._clock.now()` read (captured right before `_send_with_retry`) and the
# broker's own recorded position-open time are two independent clock samples
# -- a few seconds apart even when there is no real ambiguity at all (network
# latency, MT5's per-second time resolution). Too large a buffer would risk
# matching a genuinely unrelated position opened shortly before this request.
_AMBIGUOUS_PLACE_TIME_BUFFER_SEC = 5.0

# get_closed_trade_info() -- MT5's own DEAL_REASON for a closing deal,
# mapped to execution.adapter.ClosedTradeInfo's exit_reason vocabulary.
# DEAL_REASON_SL/DEAL_REASON_TP are the genuine broker-side hard-stop hits
# this reconciliation path exists to catch. DEAL_REASON_CLIENT/MOBILE/WEB are
# genuine human actions (terminal/mobile app/web terminal) -- bucketed as
# "manual". DEAL_REASON_EXPERT means an API/EA/script closed it -- which is
# EXACTLY what THIS adapter's own order_send() calls produce too (MT5 tags
# script-driven closes the same as EA activity), so it is NOT bucketed with
# "manual" -- see _classify_expert_closed_reason. Anything else (e.g.
# DEAL_REASON_SO, a stop-out/margin-call liquidation) is deliberately NOT
# guessed into "stop_loss" -- it is a materially different, rarer situation
# worth surfacing as "unknown" for manual investigation rather than silently
# conflated with a normal SL hit.
_SL_TP_DEAL_REASONS: dict[int, Literal["stop_loss", "take_profit"]] = {
    mt5.DEAL_REASON_SL: "stop_loss",
    mt5.DEAL_REASON_TP: "take_profit",
}
_MANUAL_DEAL_REASONS = frozenset({
    mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_MOBILE, mt5.DEAL_REASON_WEB,
})


def _classify_expert_closed_reason(
    ticket: int, deal_magic: int, own_magic: int,
) -> Literal["reconciled_system_close", "unknown"]:
    """A closing deal with `DEAL_REASON_EXPERT` means an API/EA/script closed
    the position -- MT5 does not distinguish "this adapter's own
    `close_position()`" from any other script/EA on the account, so the
    deal's own `magic` number is the only way to tell them apart (this
    adapter tags every `order_send()` request it makes with `self._magic`,
    see `place_order`/`modify_stop_loss`/`close_position`). A matching magic
    means this system's own script closed it -- most likely a close whose
    acknowledgment was lost (see `close_position`'s ambiguous-outcome
    ground-truth check) reaching reconciliation instead of the normal
    explicit-close path. A non-matching magic means some OTHER script/EA
    touched this single-purpose trading account -- genuinely unexpected,
    logged loudly rather than silently folded into "manual"."""
    if deal_magic == own_magic:
        return "reconciled_system_close"
    logger.warning(
        "get_closed_trade_info: position=%s closed by DEAL_REASON_EXPERT with magic=%s, "
        "which does not match this adapter's own magic=%s -- some OTHER script/EA closed "
        "this position, not this system. Classified as 'unknown', worth manual investigation.",
        ticket, deal_magic, own_magic,
    )
    return "unknown"


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


def _is_ambiguous_outcome(send_result) -> bool:
    """Same ambiguous category `_is_retryable_outcome` uses for its
    idempotent-only retry decision (`send_result is None`, or a retcode in
    `_AMBIGUOUS_RETCODES`) -- reused here, standalone, so `place_order`'s
    ground-truth re-query (`_resolve_ambiguous_place`) is gated on the exact
    same categorization rather than a separately-invented one. Deliberately
    excludes `_ALWAYS_RETRYABLE_RETCODES` (REQUOTE/REJECT) and every
    structurally-terminal retcode: those mean the broker has already told us
    definitively that no order was placed, so re-querying for a stray
    matching position could only add false-success risk for zero benefit."""
    if send_result is None:
        return True
    return send_result.retcode in _AMBIGUOUS_RETCODES


def _describe_ambiguous_outcome(send_result) -> str:
    """Human-readable description of whichever specific condition
    `_is_ambiguous_outcome` actually matched (`order_send()` returning
    `None`, or a TIMEOUT/CONNECTION retcode) -- used in
    `_confirmed_place_send_result`'s log message so it accurately reflects
    the real trigger instead of a hardcoded guess."""
    if send_result is None:
        return "order_send() returned None"
    return _KNOWN_REJECT_REASONS.get(send_result.retcode, f"retcode={send_result.retcode}")


def _as_naive_utc(value: datetime) -> datetime:
    """Normalizes a clock read to the naive-datetime convention
    `common/mt5_time.py` establishes for every MT5-derived timestamp this
    module compares against (`opened_at` in `_resolve_ambiguous_place`,
    `get_closed_trade_info`'s `close_time`). `RealClock.now()`
    (common/clock.py) returns a TZ-AWARE UTC datetime -- comparing it
    directly against a naive MT5-derived datetime raises `TypeError: can't
    compare offset-naive and offset-aware datetimes`. An already-naive input
    (e.g. a test's naive FakeClock) passes through unchanged."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class _ConfirmedFillResult:
    """Synthesizes the shape of a raw MT5 `order_send()` result from a
    position `_resolve_ambiguous_place` confirmed via ground-truth re-query,
    so `_send_order`'s normal success-path logic (fill reconciliation,
    abnormal-slippage check, partial-fill detection) runs on it unmodified
    instead of being bypassed by an early return."""

    def __init__(self, order: int, price: float, volume: float, retcode: int) -> None:
        self.order = order
        self.price = price
        self.volume = volume
        self.retcode = retcode
        self.comment = ""


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
        journal_db_path: str | None = None,
        circuit_breaker: CircuitBreaker | None = None,
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
        self._journal_db_path = journal_db_path
        # `None` (the default) means this adapter's rare abnormal-slippage
        # self-close path does not feed the circuit breaker's consecutive-
        # loss/daily-P&L gates -- explicit placeholder, not silently-
        # equivalent-to-tracking (see watchman/loop.py's 2026-07-23 module
        # docstring note for the incident this convention comes from).
        # scripts/run_shadow_loop.py always passes a real one; tests that
        # don't need this path's gate feed can omit it.
        self._circuit_breaker = circuit_breaker
        self._last_placed_at: datetime | None = None
        # Tickets already attributed to a place_order() call via
        # `_resolve_ambiguous_place`'s ground-truth re-query -- once a ticket
        # is confirmed here, a LATER ambiguous-place call on this same
        # adapter instance must never attribute it again (e.g. a retried/
        # duplicate signal whose own ambiguous timeout would otherwise match
        # the same already-attributed position a second time and double-book
        # one real fill as two). Self-contained to this adapter instance --
        # deliberately not shared with watchman/position_metadata.py.
        self._confirmed_place_tickets: set[int] = set()

    def _record_anomaly(self, event_type: str, details: str) -> None:
        journal.record_anomaly_event(
            timestamp=self._clock.now(), event_type=event_type, details=details,
            db_path=self._journal_db_path,
        )

    def _record_slippage_close_trade(
        self, request: TradeRequest, ticket: int, entry_price: float, entry_time: datetime,
        lot_size: float, spec: SymbolSpec,
        entry_spread_points: float | None = None, actual_slippage: float | None = None,
    ) -> None:
        """Writes the trade-journal record for a self-close triggered by
        abnormal slippage (Appendix A §4.8 items 3-4) -- called right
        alongside the `abnormal_slippage` anomaly event, in the same code
        path, so one can never happen without the other. This is a real,
        P&L-bearing trade that would otherwise never appear in the trade
        journal: `orchestrator/shadow_loop.py` only records Watchman
        metadata (not a TradeRecord) when `result.success or
        result.position_still_open`, and this path returns
        `success=False, position_still_open=False`. Queries
        `get_closed_trade_info` for the ACTUAL close price/time/commission/
        swap -- same ground-truth convention `watchman/loop.py`'s
        reconciliation path uses -- rather than approximating from the
        close `OrderResult`."""
        info = self.get_closed_trade_info(ticket)
        if info is None:
            logger.warning(
                "abnormal_slippage: ticket=%s %s closed but MT5 history has no closing deal for "
                "it yet -- trade-journal record not written this cycle (the anomaly event above "
                "was still recorded regardless).", ticket, request.symbol,
            )
            return

        point_value = spec.tick_value / spec.tick_size if spec.tick_size else 0.0
        initial_stop_distance = abs(request.entry - request.stop_loss)
        risk_amount = initial_stop_distance * point_value * lot_size
        net_pnl = info.gross_pnl - info.cost
        r_multiple = net_pnl / risk_amount if risk_amount else 0.0

        inserted = journal.record_closed_trade(
            symbol=request.symbol, direction=request.direction, entry_time=entry_time,
            entry_price=entry_price, exit_time=info.close_time, exit_price=info.close_price,
            exit_reason="abnormal_slippage", lot_size=info.closed_volume,
            gross_pnl=info.gross_pnl, cost=info.cost, net_pnl=net_pnl, r_multiple=r_multiple,
            entry_spread_points=entry_spread_points, actual_slippage=actual_slippage,
            recorded_at=self._clock.now(), broker_ticket=ticket, db_path=self._journal_db_path,
        )
        if not inserted:
            return  # swallowed duplicate write (see record_closed_trade docstring) -- do not double-notify
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_trade_close(pnl=net_pnl, closed_at=info.close_time)
        notify(
            f"[AutoTrade] Trade CLOSED {request.symbol} {request.direction} entry={entry_price:.5f} "
            f"exit={info.close_price:.5f} reason=abnormal_slippage net_pnl={net_pnl:.2f} R={r_multiple:.2f}"
        )

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

            request_sent_at = _as_naive_utc(self._clock.now())
            send_result = self._send_with_retry(mt5_request, f"place_order({request.symbol} {request.direction})")

            if _is_ambiguous_outcome(send_result):
                confirmed = self._resolve_ambiguous_place(request, spec, order_type, request_sent_at, send_result)
                if confirmed is not None:
                    # Rejoin the normal success flow below (fill
                    # reconciliation, abnormal-slippage check) instead of
                    # short-circuiting past it -- an ambiguous-timeout
                    # confirmation is exactly the scenario (high latency/
                    # volatility) where that protection is most needed.
                    send_result = confirmed

            if send_result is None:
                code, desc = mt5.last_error()
                message = f"order_send() returned None after {self._max_retries} retries: [{code}] {desc}"
                logger.error("execution_failed: %s", message)
                self._record_anomaly("execution_failed", message)
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
                self._record_anomaly("order_reject", message)
                return OrderResult(
                    success=False, broker_ticket=None, filled_price=None,
                    filled_volume=None, retcode=send_result.retcode, message=message,
                )

            partial_fill = abs(send_result.volume - request.lot_size) > 1e-9
            entry_time = self._clock.now()
            entry_spread_points = (tick.ask - tick.bid) / spec.point if spec.point else None
            abnormal_slippage = self._reconcile_fill(request, send_result, spec.point, current_atr)

            if abnormal_slippage:
                price_diff = abs(send_result.price - request.entry)
                slippage_message = (
                    f"abnormal_slippage: ticket={send_result.order} {request.symbol} filled at "
                    f"{send_result.price:.5f}, intended entry {request.entry:.5f} -- drift "
                    f"{price_diff:.5f} > {self._max_entry_slippage_atr:.2f}x ATR ({current_atr:.5f})"
                )
                logger.warning(slippage_message)
                self._record_anomaly("abnormal_slippage", slippage_message)
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
                        self._record_slippage_close_trade(
                            request=request, ticket=send_result.order, entry_price=send_result.price,
                            entry_time=entry_time, lot_size=send_result.volume, spec=spec,
                            entry_spread_points=entry_spread_points, actual_slippage=price_diff,
                        )
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
                    self._record_anomaly("execution_failed", message)
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

    def _resolve_ambiguous_place(
        self, request: TradeRequest, spec: SymbolSpec, order_type: int, request_sent_at: datetime,
        send_result,
    ) -> _ConfirmedFillResult | None:
        """Ground-truth disambiguation for `place_order`'s ambiguous outcome
        (`_is_ambiguous_outcome` -- TIMEOUT/CONNECTION/`order_send()`
        returning `None`), called BEFORE `_send_order` declares failure.

        Unlike `close_position`'s `_resolve_ambiguous_close` (which re-queries
        a KNOWN ticket -- its target position's disappearance/reduction is
        direct proof), `place_order` has no ticket to check yet: a brand-new
        position merely *existing* is much weaker evidence that it came from
        THIS specific request. This re-queries every open position on
        `request.symbol` and keeps only those that match this adapter's own
        `magic`, the requested direction, a volume no larger than requested
        (a genuine partial fill still counts -- see `_send_order`'s own
        `partial_fill` handling on the normal path), were opened within
        `_AMBIGUOUS_PLACE_TIME_BUFFER_SEC`'s clock-skew tolerance of
        `request_sent_at` and this re-query's own `self._clock.now()`, and
        have not already been attributed to an earlier ambiguous-place call
        on this adapter instance (`self._confirmed_place_tickets` -- without
        this, two ambiguous timeouts close together in time, e.g. a retried
        signal, could both match the SAME one real position and double-book
        it as two logical fills).

        Returns a synthesized `_ConfirmedFillResult` (the same shape as a raw
        `mt5.order_send()` result) ONLY if exactly one position matches -- an
        unambiguous "this is ours" -- so the caller (`_send_order`) can rejoin
        its normal success flow (fill reconciliation, abnormal-slippage check)
        on it, rather than short-circuiting past that protection. Returns
        `None` (caller proceeds to its normal failure handling) if zero
        positions match (the order genuinely did not go through), the
        re-query itself fails (cannot tell either way, same fail-toward-
        failure discipline as `_resolve_ambiguous_close`), or MORE THAN ONE
        position matches: with no ticket to anchor on, two or more equally-
        plausible candidates (e.g. a stray manual trade, or a genuine
        concurrent duplicate signal, sharing this request's symbol/magic/
        direction/volume within the window) make it impossible to safely
        attribute any ONE of them to this request -- guessing wrongly here
        would misattribute a real position's entry-time risk context
        (Watchman metadata, throttle state) to the wrong ticket, so this
        deliberately prefers a false failure (a real fill reported as
        REJECTED, caught by the next Watchman reconciliation cycle) over a
        false success (the wrong ticket credited as this request's fill)."""
        positions = mt5.positions_get(symbol=spec.broker_name)
        if positions is None:
            code, desc = mt5.last_error()
            logger.warning(
                "place_order(%s %s): outcome was ambiguous, and the ground-truth re-query "
                "(positions_get) also failed ([%d] %s) -- cannot confirm whether the order "
                "actually went through, falling back to reporting failure.",
                request.symbol, request.direction, code, desc,
            )
            return None

        requery_time = _as_naive_utc(self._clock.now())
        earliest_valid_time = request_sent_at - timedelta(seconds=_AMBIGUOUS_PLACE_TIME_BUFFER_SEC)
        latest_valid_time = requery_time + timedelta(seconds=_AMBIGUOUS_PLACE_TIME_BUFFER_SEC)
        expected_position_type = (
            mt5.POSITION_TYPE_BUY if order_type == mt5.ORDER_TYPE_BUY else mt5.POSITION_TYPE_SELL
        )
        matches = []
        for pos in positions:
            if pos.ticket in self._confirmed_place_tickets:
                continue
            if pos.magic != self._magic or pos.type != expected_position_type:
                continue
            if pos.volume > request.lot_size + 1e-9:
                continue
            opened_at = datetime.fromtimestamp(pos.time, tz=timezone.utc).replace(tzinfo=None)
            if opened_at < earliest_valid_time or opened_at > latest_valid_time:
                continue
            matches.append(pos)

        if not matches:
            logger.info(
                "place_order(%s %s): outcome was ambiguous but no matching new position was "
                "found -- the order genuinely did not go through.", request.symbol, request.direction,
            )
            return None

        if len(matches) > 1:
            logger.error(
                "place_order(%s %s): outcome was ambiguous, and %d positions match "
                "magic=%s/direction/volume<=%.2f opened at/after %s -- cannot safely attribute "
                "any single one to this request, not guessing, reporting failure for "
                "investigation. candidate tickets=%s",
                request.symbol, request.direction, len(matches), self._magic, request.lot_size,
                request_sent_at, [pos.ticket for pos in matches],
            )
            return None

        matched_position = matches[0]
        self._confirmed_place_tickets.add(matched_position.ticket)
        return self._confirmed_place_send_result(matched_position, request, send_result)

    def _confirmed_place_send_result(
        self, position, request: TradeRequest, send_result,
    ) -> _ConfirmedFillResult:
        """Builds the synthesized `_ConfirmedFillResult` for a place that
        `_resolve_ambiguous_place` confirmed actually happened despite a
        lost/ambiguous acknowledgment -- mirrors `_confirmed_close_result`'s
        structure. Uses the broker's own recorded `price_open`/`volume`
        (ground truth) as the real fill, not the originally-requested entry
        price/lot_size -- `retcode` is set to `DONE`/`DONE_PARTIAL` (matching
        whether the matched volume is the full requested lot_size or a
        partial fill) so `_send_order`'s normal success-path logic treats it
        exactly as it would a genuine `order_send()` success."""
        retcode = (
            mt5.TRADE_RETCODE_DONE if abs(position.volume - request.lot_size) <= 1e-9
            else mt5.TRADE_RETCODE_DONE_PARTIAL
        )
        message = (
            f"place_order({request.symbol} {request.direction}) outcome was ambiguous "
            f"({_describe_ambiguous_outcome(send_result)}) but a matching new position "
            f"(ticket={position.ticket}) is confirmed open on the broker -- treating as a "
            f"successful fill whose acknowledgment was lost"
        )
        logger.info(message)
        return _ConfirmedFillResult(
            order=position.ticket, price=position.price_open, volume=position.volume, retcode=retcode,
        )

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
                self._record_anomaly("execution_failed" if send_result is None else "order_reject", message)
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
                confirmed = self._resolve_ambiguous_close(ticket, close_volume, position.volume)
                if confirmed is not None:
                    return confirmed

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
                self._record_anomaly("execution_failed" if send_result is None else "order_reject", message)
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

    def _resolve_ambiguous_close(
        self, ticket: int, close_volume: float, volume_at_request: float,
    ) -> OrderResult | None:
        """Ground-truth disambiguation for `close_position()`'s non-DONE
        outcome (`_send_with_retry` returned `None`, a rejected retcode, or
        an ambiguous TIMEOUT/CONNECTION) -- called BEFORE declaring failure.
        A lost acknowledgment does not mean the close didn't happen: this
        re-queries the position directly (NOT via `_get_position`, which
        collapses a genuine query failure and a confirmed "position gone"
        into the same `None` -- here the two must be told apart) to check
        whether it actually went through anyway.

        Returns a success `OrderResult` if ground truth confirms the
        requested close (full or partial) actually happened. Returns `None`
        (caller proceeds to its normal failure handling) in every other
        case: the position is confirmed still open at its original volume
        (the close genuinely did not happen), the re-query itself failed
        (MT5 unreachable -- cannot tell, fail toward the conservative
        report-failure behavior rather than guess), or the observed state
        doesn't cleanly match what was requested (e.g. a partial close
        requested but the whole position is gone -- don't guess at fill
        numbers, surface it as a failure worth investigating instead)."""
        positions = mt5.positions_get(ticket=ticket)
        if positions is None:
            code, desc = mt5.last_error()
            logger.warning(
                "close_position(ticket=%s): outcome was ambiguous/failed, and the ground-truth "
                "re-query (positions_get) also failed ([%d] %s) -- cannot confirm whether the "
                "close actually succeeded, falling back to reporting failure.",
                ticket, code, desc,
            )
            return None

        full_close_requested = abs(close_volume - volume_at_request) < 1e-9

        if not positions:
            if not full_close_requested:
                logger.warning(
                    "close_position(ticket=%s): a PARTIAL close (%.2f of %.2f lots) was "
                    "requested but the position is now entirely gone -- more than intended "
                    "may have closed (e.g. a broker-side SL/TP also fired); not guessing at "
                    "fill numbers here, reporting the ambiguous outcome as a failure for "
                    "investigation.", ticket, close_volume, volume_at_request,
                )
                return None
            return self._confirmed_close_result(ticket, volume_at_request)

        remaining_volume = positions[0].volume
        if not full_close_requested and remaining_volume < volume_at_request - 1e-9:
            actually_closed = volume_at_request - remaining_volume
            if abs(actually_closed - close_volume) < 1e-9:
                return self._confirmed_close_result(ticket, close_volume)
            logger.warning(
                "close_position(ticket=%s): outcome was ambiguous/failed, and the remaining "
                "volume (%.2f) doesn't cleanly match the requested partial close (%.2f of "
                "%.2f) -- not guessing, reporting failure for investigation.",
                ticket, remaining_volume, close_volume, volume_at_request,
            )
            return None

        if abs(remaining_volume - volume_at_request) < 1e-9:
            logger.info(
                "close_position(ticket=%s): outcome was ambiguous/failed but the position is "
                "confirmed still open at its original volume -- the close genuinely did not "
                "happen.", ticket,
            )
        else:
            # full_close_requested=True but remaining_volume is neither
            # volume_at_request (untouched) nor 0 (fully closed, handled by
            # the `not positions` branch above) -- e.g. an IOC full-close
            # order partially filled from thin liquidity. Not guessing this
            # is success (the requested FULL close did not happen), but the
            # log must not claim "original volume" when the volume actually
            # changed -- that would mislead an incident investigation.
            logger.warning(
                "close_position(ticket=%s): a FULL close was requested but the position's "
                "remaining volume (%.2f) is neither the original %.2f nor 0 -- a partial fill "
                "occurred on a full-close request; not guessing, reporting failure for "
                "investigation.", ticket, remaining_volume, volume_at_request,
            )
        return None

    def _confirmed_close_result(self, ticket: int, closed_volume: float) -> OrderResult:
        """Builds the success `OrderResult` for a close that
        `_resolve_ambiguous_close` confirmed actually happened despite a
        lost/ambiguous acknowledgment -- queries `get_closed_trade_info` for
        the real fill price (ground truth from MT5's own deal history)
        rather than guessing; falls back to `filled_price=None` if that
        history hasn't caught up yet (still a genuine success -- the
        position is confirmed gone/reduced either way)."""
        info = self.get_closed_trade_info(ticket)
        filled_price = info.close_price if info is not None else None
        message = (
            f"close_position(ticket={ticket}) outcome was ambiguous (TIMEOUT/rejected) but the "
            f"position is confirmed no longer open at the requested volume -- treating as a "
            f"successful close whose acknowledgment was lost"
        )
        logger.info(message)
        return OrderResult(
            success=True, broker_ticket=ticket, filled_price=filled_price,
            filled_volume=closed_volume, retcode=None, message=message,
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

    def get_closed_trade_info(self, ticket: int) -> ClosedTradeInfo | None:
        """`mt5.history_deals_get(position=ticket)` -- confirmed against the
        real installed `MetaTrader5` package (its `position=` filter needs
        no `date_from`/`date_to` range, unlike its other overloads) that
        this returns every deal belonging to position `ticket` (both the
        entry deal and every partial/full exit deal), or an empty tuple
        `()` if `ticket` has no history at all yet -- `None` only on a
        genuine query failure. See `ClosedTradeInfo`'s docstring for the
        aggregation/sign convention this builds."""
        with mt5_session(self._creds):
            deals = mt5.history_deals_get(position=ticket)
            if deals is None:
                code, desc = mt5.last_error()
                logger.warning(
                    "get_closed_trade_info: history_deals_get(position=%s) failed: [%d] %s",
                    ticket, code, desc,
                )
                return None
            if not deals:
                logger.warning(
                    "get_closed_trade_info: no deals found in MT5 history for position=%s yet -- "
                    "it may have closed too recently for the deal to be visible; caller should "
                    "retry next cycle rather than treat this as never having closed.",
                    ticket,
                )
                return None

            exit_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
            if not exit_deals:
                logger.warning(
                    "get_closed_trade_info: position=%s has %d deal(s) in history but none is an "
                    "exit deal (DEAL_ENTRY_OUT/OUT_BY) -- it may not actually be fully closed yet; "
                    "caller should retry next cycle.",
                    ticket, len(deals),
                )
                return None

            # MT5 reports commission/swap as negative-when-charged (a debit)
            # -- `cost` here is a POSITIVE number to subtract, matching
            # store/models.py's TradeRecord.cost convention, so negate the
            # sum. `gross_pnl` is deal.profit only (pure price P&L, already
            # excludes commission/swap). Summed across EVERY deal for this
            # position (entry + every partial/full exit), not just the
            # final exit, so a position with an earlier partial close still
            # gets its full lifetime P&L in one record.
            gross_pnl = sum(d.profit for d in deals)
            cost = -sum(d.commission + d.swap for d in deals)
            closed_volume = sum(d.volume for d in exit_deals)

            last_exit = max(exit_deals, key=lambda d: d.time)
            exit_reason = _SL_TP_DEAL_REASONS.get(last_exit.reason)
            if exit_reason is None:
                if last_exit.reason in _MANUAL_DEAL_REASONS:
                    exit_reason = "manual"
                elif last_exit.reason == mt5.DEAL_REASON_EXPERT:
                    exit_reason = _classify_expert_closed_reason(ticket, last_exit.magic, self._magic)
                else:
                    exit_reason = "unknown"
                    logger.warning(
                        "get_closed_trade_info: position=%s closed with DEAL_REASON=%s, not "
                        "recognized as SL/TP/manual/expert (e.g. a stop-out/margin-call "
                        "liquidation) -- classified as 'unknown', worth manual investigation.",
                        ticket, last_exit.reason,
                    )

            return ClosedTradeInfo(
                close_price=last_exit.price,
                close_time=datetime.fromtimestamp(last_exit.time, tz=timezone.utc).replace(tzinfo=None),
                closed_volume=closed_volume,
                gross_pnl=gross_pnl,
                cost=cost,
                exit_reason=exit_reason,
            )


def _realized_rr(request: TradeRequest, fill_price: float) -> float | None:
    """R:R measured from the ACTUAL fill price against the unchanged
    (already-sent) stop-loss/take-profit -- Appendix A §4.8's post-slippage
    R:R check. `None` if risk is zero/undefined (can't divide)."""
    risk = abs(fill_price - request.stop_loss)
    if risk <= 0:
        return None
    reward = abs(request.take_profit - fill_price)
    return reward / risk
