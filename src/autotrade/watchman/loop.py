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
from autotrade.execution.adapter import BrokerAdapter, BrokerPosition
from autotrade.features.indicators import atr
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog
from autotrade.watchman.evaluate import WatchmanConfig, WatchmanDecision, evaluate_watchman
from autotrade.watchman.news_protection import (
    NewsProtectionConfig,
    NewsProtectionDecision,
    check_news_protection,
)
from autotrade.watchman.position_metadata import (
    CorruptPositionMetadataError,
    get_position_metadata,
    remove_position_metadata,
    update_news_protected_until,
)

logger = logging.getLogger(__name__)


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
        closed = self._act_on_watchman_decision(position, decision)
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
        self._act_on_news_decision(position, metadata.entry_price, news_decision, now)

    def _act_on_watchman_decision(self, position: BrokerPosition, decision: WatchmanDecision) -> bool:
        """Returns True if the position was closed (so the caller skips any
        further per-cycle action on it)."""
        if decision.action == "CLOSE":
            result = self._adapter.close_position(position.ticket)
            if result.success:
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
        self, position: BrokerPosition, entry_price: float, decision: NewsProtectionDecision, now: datetime,
    ) -> None:
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

        # CLOSE_HALF_AND_BREAKEVEN
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
                position, entry_price,
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
