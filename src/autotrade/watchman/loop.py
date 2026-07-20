"""Watchman's continuous position-management loop (Phase 7b) -- wires the
pure decision logic (`evaluate.evaluate_watchman`, `news_protection.check_news_protection`)
into real MT5 modify/close calls via `execution.adapter.BrokerAdapter`, plus
the connectivity watchdog (item 7).

**Known, deliberate simplification -- same polling cadence as the entry-signal
loop.** trading_system_summary_v2.md spec.md §2.1's module table describes
Watchman as running its "own async loop", and §2.2's data flow implies it
reacts on its own cadence (tick-by-tick for the SL trail, per Appendix A §4's
"ทุกๆ tick/bar close"), independent of the bar-close cadence
`orchestrator/shadow_loop.py`'s entry-signal pipeline runs on. Building that
genuinely-separate, faster/tick-level loop needs real concurrency (asyncio),
which this codebase does not have yet -- `orchestrator/shadow_loop.py` is
still a single blocking loop. Rather than fake concurrency with threads
bolted on prematurely, `WatchmanLoop.run_cycle()` is instead invoked once per
`shadow_loop.py` polling iteration, right after that iteration's entry-signal
processing for every symbol (see `feed/poller.py`'s `on_iteration_end` hook
and `ShadowLoop._run_watchman_cycle`). This is a real, documented
simplification -- Watchman's SL trail and structure-invalidation checks
currently only run as often as the entry-signal loop polls (default every 5
seconds, an H1 bar-close cadence besides), not on every tick -- to be
revisited once the orchestrator gets true asyncio concurrency and Watchman
can run on its own faster loop as originally specified. It is NOT an
oversight; it is the pragmatic Phase 7b scope boundary.

**Per-position error isolation.** Every open position is evaluated in its
own error boundary -- one bad/corrupt position must never stop the others
from being checked this cycle, mirroring `evaluate.evaluate_watchman`'s own
documented requirement (see that module's RAISES section) and
`orchestrator/shadow_loop.py`'s `_process()`/`_process_bar()` split. A
corrupt `PositionMetadata` store (`CorruptPositionMetadataError`) gets its
own explicit, loud handling per `watchman/position_metadata.py`'s module
docstring: halt position-MANAGEMENT actions for that position this cycle,
alert loudly, but never crash the loop -- existing positions stay protected
by their broker-side hard SL regardless of whether this system can currently
manage them.

**Phase 8a: trade-journal reconciliation, two non-overlapping paths.** Most
closed trades never go through an explicit `close_position()` call here at
all -- they close because the broker's own hard SL/TP fired directly against
MT5. A trade journal fed only by this module's own close calls would miss
most real trades, so every close is captured by exactly ONE of two paths,
never both:

  1. **Explicit-close path** (`_act_on_watchman_decision`'s `CLOSE` branch,
     `_act_on_news_decision`'s `CLOSE_ALL` branch): when this module itself
     successfully calls `adapter.close_position()` for a FULL close, it
     immediately computes and writes the trade-journal record right there
     (from `PositionMetadata`'s entry price/initial stop distance/opened_at
     plus the `OrderResult`'s close fill price/volume) and removes the
     position metadata in the same breath -- since metadata is gone
     immediately, path 2 below can never see this ticket again.
  2. **Reconciliation path** (`_reconcile_closed_positions`, run once per
     `run_cycle()` after every still-open position has been managed): for
     every ticket that still has `PositionMetadata` but is NO LONGER in
     `adapter.get_open_positions()`'s result, this queries MT5's own trade
     history (`adapter.get_closed_trade_info()`) for the real closing deal,
     writes the trade-journal record from THAT ground-truth data, and only
     then removes the metadata.

A PARTIAL close (news protection's `CLOSE_HALF_AND_BREAKEVEN`) deliberately
does NOT remove metadata and does NOT write a trade-journal record -- the
position is still open (just smaller), so neither path applies until it is
later fully closed (by either path).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from autotrade.common.symbols import SymbolSpec, get_symbol_spec
from autotrade.council.news_calendar import NewsCalendarProvider
from autotrade.execution.adapter import BrokerAdapter, BrokerPosition, OrderResult
from autotrade.features.indicators import atr
from autotrade.store import journal
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog
from autotrade.watchman.evaluate import WatchmanConfig, WatchmanDecision, evaluate_watchman
from autotrade.watchman.news_protection import (
    NewsProtectionConfig,
    NewsProtectionDecision,
    check_news_protection,
)
from autotrade.watchman.position_metadata import (
    CorruptPositionMetadataError,
    PositionMetadata,
    get_all_tracked_tickets,
    get_position_metadata,
    remove_position_metadata,
    update_news_protected_until,
)

logger = logging.getLogger(__name__)


def _classify_watchman_close_reason(reason: str) -> journal.ExitReason:
    """`evaluate.evaluate_watchman`'s `WatchmanDecision.reason` is a free-text
    human message, not a machine-readable code -- but its two `CLOSE` cases
    each start with a fixed, known prefix (see `evaluate.py`), so this maps
    those prefixes to the trade journal's `exit_reason` vocabulary rather
    than widening `WatchmanDecision`'s shape just for this. Anything
    unrecognized (should never happen given `evaluate_watchman`'s only two
    CLOSE reasons) falls back to `"unknown"`, logged loudly rather than
    silently mis-categorized."""
    if reason.startswith("structure invalidation"):
        return "structure_invalidation"
    if reason.startswith("time stop"):
        return "time_stop"
    logger.warning(
        "Watchman: unrecognized CLOSE reason %r while classifying it for the trade journal -- "
        "recording exit_reason='unknown' rather than guessing.", reason,
    )
    return "unknown"


def _current_atr(history: pd.DataFrame, as_of_index: int, period: int = 14) -> float:
    closes = history["close"].iloc[: as_of_index + 1]
    highs = history["high"].iloc[: as_of_index + 1]
    lows = history["low"].iloc[: as_of_index + 1]
    return float(atr(highs, lows, closes, period=period).iloc[-1])


def _half_volume_rounded(total_volume: float, spec: SymbolSpec) -> float | None:
    """Half of `total_volume`, rounded DOWN to the broker's `volume_step` --
    same "round down, never up" convention as `risk/sizing.py`'s lot
    rounding. Returns `None` if the rounded half would be below
    `volume_min` (too small a lot for the broker to accept at all) -- the
    caller falls back to closing the whole position instead in that case,
    since a full close is still risk-reducing even when an exact half
    isn't a valid lot size."""
    half = total_volume / 2.0
    if spec.volume_step > 0:
        steps = math.floor(half / spec.volume_step + 1e-9)
        half = steps * spec.volume_step
    if half < spec.volume_min - 1e-9 or half <= 0:
        return None
    return round(half, 8)


def _signed_gross_pnl(
    direction: str, entry_price: float, exit_price: float, lot_size: float, symbol_spec: SymbolSpec,
) -> float:
    """Pure price P&L (no commission/swap) for a closed position -- same
    `price_diff * point_value * volume` formula `orchestrator/shadow_loop.py`
    uses for Shield's `risk_pct` approximation, applied here to a KNOWN
    entry/exit instead. `point_value` is money-per-1.0-price-unit-per-1.0-lot
    (`tick_value / tick_size`)."""
    point_value = symbol_spec.tick_value / symbol_spec.tick_size if symbol_spec.tick_size else 0.0
    signed_diff = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)
    return signed_diff * point_value * lot_size


class WatchmanLoop:
    def __init__(
        self,
        adapter: BrokerAdapter,
        watchman_config: WatchmanConfig,
        news_provider: NewsCalendarProvider,
        news_protection_config: NewsProtectionConfig,
        connectivity_watchdog: ConnectivityWatchdog,
        resolve_symbol_spec: Callable[[str], SymbolSpec] | None = None,
        symbol_map: dict[str, str] | None = None,
        state_path: Path | None = None,
        journal_db_path: Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._watchman_config = watchman_config
        self._news_provider = news_provider
        self._news_protection_config = news_protection_config
        self._connectivity_watchdog = connectivity_watchdog
        self._resolve_symbol_spec = resolve_symbol_spec or (
            lambda symbol: get_symbol_spec(symbol, symbol_map)
        )
        self._state_path = state_path
        self._journal_db_path = journal_db_path

    def run_cycle(self, history_by_symbol: dict[str, pd.DataFrame], now: datetime) -> None:
        """One Watchman pass over every currently open position, across
        every symbol -- not just the symbol whose bar just triggered this
        polling iteration (see module docstring's same-cadence
        simplification)."""
        try:
            open_positions = self._adapter.get_open_positions()
        except Exception:
            logger.exception(
                "Watchman: get_open_positions() failed -- MT5 may be unreachable; skipping this "
                "cycle's position management entirely. Existing positions remain protected by "
                "their own broker-side hard stop-loss regardless (Appendix A §4.7)."
            )
            self._connectivity_watchdog.check()
            return

        self._connectivity_watchdog.record_connected()
        self._connectivity_watchdog.check()

        for position in open_positions:
            try:
                self._manage_one_position(position, history_by_symbol, now)
            except Exception:
                logger.exception(
                    "Watchman: unhandled exception managing ticket=%s symbol=%s -- skipping this "
                    "position this cycle, other open positions are still processed normally.",
                    position.ticket, position.symbol,
                )

        self._reconcile_closed_positions(open_positions, now)

    def _reconcile_closed_positions(self, open_positions: list[BrokerPosition], now: datetime) -> None:
        """Catches positions that closed WITHOUT this system ever calling
        `close_position()` itself -- overwhelmingly a broker-side hard SL/TP
        hit (see module docstring's two-path reconciliation design). Runs
        once per cycle, after every still-open position above has been
        managed."""
        open_tickets = {position.ticket for position in open_positions}
        try:
            tracked_tickets = get_all_tracked_tickets(self._state_path)
        except CorruptPositionMetadataError:
            logger.error(
                "Watchman reconciliation: position metadata store is corrupt/unreadable -- "
                "skipping reconciliation entirely this cycle (see "
                "position_metadata.py's module docstring). Existing positions remain protected "
                "by their own broker-side hard stop-loss regardless."
            )
            return

        for ticket in tracked_tickets:
            if ticket in open_tickets:
                continue
            try:
                self._reconcile_one_closed_ticket(ticket, now)
            except Exception:
                logger.exception(
                    "Watchman reconciliation: unhandled exception reconciling closed ticket=%s -- "
                    "skipping this ticket this cycle, other reconciliation continues.", ticket,
                )

    def _reconcile_one_closed_ticket(self, ticket: int, now: datetime) -> None:
        metadata = get_position_metadata(ticket, self._state_path)
        if metadata is None:
            return  # already reconciled/removed earlier this same pass

        info = self._adapter.get_closed_trade_info(ticket)
        if info is None:
            logger.warning(
                "Watchman reconciliation: ticket=%s symbol=%s no longer open but MT5 history has "
                "no closing deal for it yet -- skipping this cycle, will retry next cycle "
                "(metadata retained, no double-counting risk).", ticket, metadata.symbol,
            )
            return

        self._write_trade_record(
            symbol=metadata.symbol, direction=metadata.direction,
            entry_time=metadata.opened_at, entry_price=metadata.entry_price,
            exit_time=info.close_time, exit_price=info.close_price,
            exit_reason=info.exit_reason, lot_size=info.closed_volume,
            gross_pnl=info.gross_pnl, cost=info.cost,
            initial_stop_distance=metadata.initial_stop_distance,
            broker_ticket=ticket, recorded_at=now,
            entry_spread_points=metadata.entry_spread_points, actual_slippage=metadata.actual_slippage,
        )
        remove_position_metadata(ticket, self._state_path)
        logger.info(
            "Watchman reconciliation: ticket=%s symbol=%s closed via %s (broker-side/external, "
            "not initiated by this system) -- trade journal record written, metadata removed.",
            ticket, metadata.symbol, info.exit_reason,
        )

    def _write_trade_record(
        self,
        *,
        symbol: str,
        direction: str,
        entry_time: datetime,
        entry_price: float,
        exit_time: datetime,
        exit_price: float,
        exit_reason: journal.ExitReason,
        lot_size: float,
        gross_pnl: float,
        cost: float,
        initial_stop_distance: float,
        broker_ticket: int,
        recorded_at: datetime,
        entry_spread_points: float | None = None,
        actual_slippage: float | None = None,
    ) -> None:
        """Shared record-building/persistence for both close paths -- same
        `net_pnl = gross_pnl - cost` / `r_multiple = net_pnl / risk_amount`
        convention as `backtest/engine.py`'s `ClosedTrade` (see
        `store/models.py`'s module docstring)."""
        net_pnl = gross_pnl - cost
        spec = self._resolve_symbol_spec(symbol)
        point_value = spec.tick_value / spec.tick_size if spec.tick_size else 0.0
        risk_amount = initial_stop_distance * point_value * lot_size
        r_multiple = net_pnl / risk_amount if risk_amount else 0.0
        journal.record_closed_trade(
            symbol=symbol, direction=direction, entry_time=entry_time, entry_price=entry_price,
            exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason, lot_size=lot_size,
            gross_pnl=gross_pnl, cost=cost, net_pnl=net_pnl, r_multiple=r_multiple,
            entry_spread_points=entry_spread_points, actual_slippage=actual_slippage,
            broker_ticket=broker_ticket, recorded_at=recorded_at,
            db_path=self._journal_db_path,
        )

    def _record_explicit_close(
        self, metadata: PositionMetadata, result: OrderResult, exit_reason: journal.ExitReason, now: datetime,
    ) -> None:
        """Explicit-close path (see module docstring): computes the trade
        record directly from `PositionMetadata` + the close `OrderResult` --
        no MT5 history query. `exit_time` uses the current cycle time `now`
        as a proxy for the real fill time (not carried on `OrderResult`),
        and `cost` (commission/swap) is left at `0.0` -- neither is available
        without an extra history query, which the reconciliation path (not
        this one) already performs for exactly this reason."""
        self._write_trade_record(
            symbol=metadata.symbol, direction=metadata.direction,
            entry_time=metadata.opened_at, entry_price=metadata.entry_price,
            exit_time=now, exit_price=result.filled_price, exit_reason=exit_reason,
            lot_size=result.filled_volume, gross_pnl=_signed_gross_pnl(
                direction=metadata.direction, entry_price=metadata.entry_price,
                exit_price=result.filled_price, lot_size=result.filled_volume,
                symbol_spec=self._resolve_symbol_spec(metadata.symbol),
            ),
            cost=0.0, initial_stop_distance=metadata.initial_stop_distance,
            broker_ticket=metadata.ticket, recorded_at=now,
            entry_spread_points=metadata.entry_spread_points, actual_slippage=metadata.actual_slippage,
        )

    def _manage_one_position(
        self, position: BrokerPosition, history_by_symbol: dict[str, pd.DataFrame], now: datetime,
    ) -> None:
        try:
            metadata = get_position_metadata(position.ticket, self._state_path)
        except CorruptPositionMetadataError:
            logger.error(
                "Watchman: position metadata store is corrupt/unreadable -- halting Watchman "
                "position-MANAGEMENT actions for ticket=%s symbol=%s this cycle (see "
                "watchman/position_metadata.py's module docstring). The broker-side hard SL "
                "already on this position still protects it regardless.",
                position.ticket, position.symbol,
            )
            return

        if metadata is None:
            logger.warning(
                "Watchman: no recorded entry-time metadata for open ticket=%s symbol=%s -- this "
                "system never recorded opening it (manual trade, or recorded-then-lost). Skipping "
                "Watchman management for it; its existing broker-side SL/TP still apply.",
                position.ticket, position.symbol,
            )
            return

        history = history_by_symbol.get(position.symbol)
        if history is None or len(history) == 0:
            logger.warning(
                "Watchman: no seeded history for symbol=%s -- cannot evaluate ticket=%s this cycle",
                position.symbol, position.ticket,
            )
            return

        as_of_index = len(history) - 1
        current_atr = _current_atr(history, as_of_index)

        decision = evaluate_watchman(
            position_metadata=metadata, current_sl=position.current_sl, current_price=position.current_price,
            current_atr=current_atr, df=history, as_of_index=as_of_index, now=now, config=self._watchman_config,
        )
        closed = self._act_on_watchman_decision(position, decision, metadata, now)
        if closed:
            return  # nothing left open to protect from news this cycle

        if metadata.news_protected_until is not None and now < metadata.news_protected_until:
            logger.debug(
                "Watchman: news protection already applied for ticket=%s symbol=%s within the "
                "current news window (protected until %s) -- skipping re-trigger this cycle",
                position.ticket, position.symbol, metadata.news_protected_until,
            )
            return

        news_decision = check_news_protection(
            position_metadata=metadata, current_price=position.current_price,
            news_provider=self._news_provider, now=now, config=self._news_protection_config,
        )
        self._act_on_news_decision(position, metadata, news_decision, now)

    def _act_on_watchman_decision(
        self, position: BrokerPosition, decision: WatchmanDecision, metadata: PositionMetadata, now: datetime,
    ) -> bool:
        """Returns True if the position was closed (so the caller skips any
        further per-cycle action on it)."""
        if decision.action == "CLOSE":
            result = self._adapter.close_position(position.ticket)
            if result.success:
                self._record_explicit_close(
                    metadata, result, _classify_watchman_close_reason(decision.reason), now,
                )
                remove_position_metadata(position.ticket, self._state_path)
                logger.info(
                    "Watchman CLOSE ticket=%s symbol=%s -- %s",
                    position.ticket, position.symbol, decision.reason,
                )
            else:
                logger.error(
                    "Watchman CLOSE FAILED ticket=%s symbol=%s -- intended reason: %s; close error: %s",
                    position.ticket, position.symbol, decision.reason, result.message,
                )
            return True

        if decision.action == "MODIFY_SL":
            result = self._adapter.modify_stop_loss(position.ticket, decision.new_stop_loss)
            if result.success:
                logger.info(
                    "Watchman MODIFY_SL ticket=%s symbol=%s -- %s (applied SL=%.5f)",
                    position.ticket, position.symbol, decision.reason, result.filled_price,
                )
            else:
                logger.error(
                    "Watchman MODIFY_SL FAILED ticket=%s symbol=%s -- intended reason: %s; modify error: %s",
                    position.ticket, position.symbol, decision.reason, result.message,
                )
            return False

        return False

    def _act_on_news_decision(
        self, position: BrokerPosition, metadata: PositionMetadata, decision: NewsProtectionDecision, now: datetime,
    ) -> None:
        entry_price = metadata.entry_price
        if decision.action == "NO_ACTION":
            return

        # Roughly the end of the news blackout window this action is
        # protecting against -- recorded on the position's metadata so
        # `_manage_one_position` skips re-triggering protection for the SAME
        # window on subsequent cycles (news-protection dedup bugfix).
        protected_until = now + timedelta(minutes=self._news_protection_config.news_window_minutes)

        if decision.action == "CLOSE_ALL":
            result = self._adapter.close_position(position.ticket)
            if result.success:
                self._record_explicit_close(metadata, result, "news_protection", now)
                remove_position_metadata(position.ticket, self._state_path)
                logger.info(
                    "Watchman news protection CLOSE_ALL ticket=%s symbol=%s -- %s",
                    position.ticket, position.symbol, decision.reason,
                )
            else:
                logger.error(
                    "Watchman news protection CLOSE_ALL FAILED ticket=%s symbol=%s -- intended "
                    "reason: %s; close error: %s",
                    position.ticket, position.symbol, decision.reason, result.message,
                )
            return

        # CLOSE_HALF_AND_BREAKEVEN -- a PARTIAL close: the position stays
        # open (just smaller), so no trade-journal record and no metadata
        # removal here, per module docstring's two-path design.
        spec = self._resolve_symbol_spec(position.symbol)
        half_volume = _half_volume_rounded(position.volume, spec)
        if half_volume is None:
            logger.warning(
                "Watchman news protection: half of ticket=%s symbol=%s's volume (%.2f) would round "
                "below the broker's minimum lot -- closing the WHOLE position instead (still "
                "risk-reducing) rather than skipping protection entirely.",
                position.ticket, position.symbol, position.volume,
            )
            self._act_on_news_decision(
                position, metadata,
                NewsProtectionDecision(action="CLOSE_ALL", reason=decision.reason), now,
            )
            return

        close_result = self._adapter.close_position(position.ticket, volume=half_volume)
        if not close_result.success:
            logger.error(
                "Watchman news protection CLOSE_HALF FAILED ticket=%s symbol=%s -- intended reason: "
                "%s; close error: %s",
                position.ticket, position.symbol, decision.reason, close_result.message,
            )
            return

        # The close itself succeeded -- the protective action has genuinely
        # been taken for this window, so dedup it regardless of whether the
        # follow-up breakeven modify below also succeeds (a failed modify is
        # its own separately-logged concern, not a reason to re-trigger a
        # second half-close next cycle).
        update_news_protected_until(position.ticket, protected_until, self._state_path)

        modify_result = self._adapter.modify_stop_loss(position.ticket, entry_price)
        if modify_result.success:
            logger.info(
                "Watchman news protection CLOSE_HALF_AND_BREAKEVEN ticket=%s symbol=%s closed "
                "%.2f lots, SL moved to breakeven %.5f -- %s",
                position.ticket, position.symbol, half_volume, entry_price, decision.reason,
            )
        else:
            logger.error(
                "Watchman news protection: closed half (%.2f lots) of ticket=%s symbol=%s but "
                "moving the remainder's SL to breakeven FAILED -- %s. The remaining position "
                "still carries its OLD stop-loss, which still protects it, just not yet at "
                "breakeven.",
                half_volume, position.ticket, position.symbol, modify_result.message,
            )
