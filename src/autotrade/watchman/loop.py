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

**2026-07-23 fix: `CircuitBreaker.record_trade_close()` is now called from
`_write_trade_record()`** (the single choke point both close paths above
funnel through). Before this, it was never called anywhere in the live
loop -- `orchestrator/shadow_loop.py` only ever called `record_equity()`,
so the consecutive-loss and realized-daily-P&L gates silently never fired
regardless of actual trade outcomes (only the equity-based drawdown gate
worked, since that's fed by `record_equity()` separately). Found by
inspecting `circuit_breaker_state.json` after two real consecutive losing
trades showed `consecutive_losses: 0`. `ThrottledDemoAdapter`'s separate
abnormal-slippage self-close path (`execution/demo_adapter.py`) is wired
the same way, since it is a third, independent trade-closing path that
also needs to feed the same counters.
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
from autotrade.notify.telegram import notify
from autotrade.risk.circuit_breaker import CircuitBreaker
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
    record_position_opened,
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


# How long to back off re-attempting a CLOSE on the same ticket after it
# just failed, before trying again -- see WatchmanLoop.__init__'s own
# comment on self._last_failed_close_attempt for the full incident this
# fixes. 15 minutes bounds the spam to a small fraction of its previous
# ~5-second cadence while still recovering promptly once the underlying
# cause (e.g. a weekend market closure) resolves.
_CLOSE_RETRY_BACKOFF = timedelta(minutes=15)


class WatchmanLoop:
    def __init__(
        self,
        adapter: BrokerAdapter,
        watchman_config: WatchmanConfig,
        news_provider: NewsCalendarProvider,
        news_protection_config: NewsProtectionConfig,
        connectivity_watchdog: ConnectivityWatchdog,
        circuit_breaker: CircuitBreaker,
        own_magic: int,
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
        self._circuit_breaker = circuit_breaker
        # Fix C (2026-07-25), orphan-position reconciliation: the ONLY way
        # to tell "almost certainly opened by THIS system" (BrokerPosition's
        # own `magic`, sourced from the broker) apart from a genuine manual/
        # other-script trade -- must match whatever `magic` the live adapter
        # (`execution.demo_adapter.ThrottledDemoAdapter`) actually tags its
        # own orders with (`execution.demo_adapter.DEFAULT_MAGIC` unless
        # overridden). No default here deliberately -- a silent mismatch
        # would make _reconcile_orphan_positions() either never fire or
        # (worse) start seeding metadata for genuinely manual trades.
        self._own_magic = own_magic
        self._resolve_symbol_spec = resolve_symbol_spec or (
            lambda symbol: get_symbol_spec(symbol, symbol_map)
        )
        self._state_path = state_path
        self._journal_db_path = journal_db_path
        # 2026-07-25 fix: a failed CLOSE isn't remembered anywhere, so an
        # unchanged decision (e.g. structure invalidation still true) gets
        # re-attempted and re-alerted EVERY cycle (~5s) for as long as the
        # underlying cause persists -- observed live over a weekend market
        # closure (retcode 10018 "market closed"), where it would otherwise
        # have hammered order_send() and journal.record_anomaly_event()
        # (which itself notify()s on every call, unconditionally -- see that
        # function's own docstring) roughly every 5 seconds for two straight
        # days. In-memory only (not file-persisted): a shadow_loop.py
        # restart re-establishing this dict from empty is fine, it just
        # means one immediate retry attempt post-restart, never a safety
        # issue since the position stays broker-SL-protected regardless.
        self._last_failed_close_attempt: dict[int, datetime] = {}
        # 2026-07-29 audit finding (false-orphan bug, ticket=1825965537):
        # tickets this system itself explicitly closed DURING the current
        # cycle. `run_cycle` fetches `open_positions` ONCE at the top, then
        # `_manage_one_position` may close one of them (Watchman CLOSE /
        # news-protection CLOSE_ALL) and remove its metadata mid-cycle --
        # after which `_reconcile_orphan_positions`, still iterating that
        # start-of-cycle snapshot, saw the just-closed ticket as "open with
        # no metadata" and mis-flagged it as an orphan: a spurious CRITICAL
        # alert + `orphan_position_found` anomaly event, and (worse)
        # approximate metadata seeded for an already-CLOSED position, whose
        # only defense against overwriting the real trade record next cycle
        # was `TradeRecord.broker_ticket`'s UNIQUE constraint winning a
        # write-ordering race. In-memory and reset every cycle -- this only
        # ever needs to describe the CURRENT cycle's own closes.
        self._closed_this_cycle: set[int] = set()

    def run_cycle(self, history_by_symbol: dict[str, pd.DataFrame], now: datetime) -> None:
        """One Watchman pass over every currently open position, across
        every symbol -- not just the symbol whose bar just triggered this
        polling iteration (see module docstring's same-cadence
        simplification)."""
        self._closed_this_cycle = set()
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
        self._reconcile_orphan_positions(open_positions, now)

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

    def _reconcile_orphan_positions(self, open_positions: list[BrokerPosition], now: datetime) -> None:
        """Fix C (2026-07-25): the MIRROR gap `_reconcile_closed_positions`
        above does not cover -- a broker position that IS currently open,
        carries THIS adapter's own `magic` number (so it was almost
        certainly opened by this system, not a genuine manual/other-script
        trade), but has NO `PositionMetadata` at all (e.g. a crash between a
        successful `place_order()` fill and the orchestrator's
        `record_position_opened()` call right after it).

        Without this, such a ticket is invisible to
        `_reconcile_closed_positions`'s own tracked-tickets loop (it only
        walks `get_all_tracked_tickets()`) -- so its EVENTUAL close (broker
        SL/TP or otherwise) would never be observed by either of the two
        documented reconciliation paths and would permanently vanish from
        `trade_records`, not just be delayed like the already-handled
        closed-with-metadata case above.

        On discovery: `notify()`/`logger.critical`/an `orphan_position_found`
        anomaly event fire immediately (a surprising, actionable event worth
        a human's attention), and approximate `PositionMetadata` is seeded
        from the position's CURRENT price/SL (the TRUE entry price/time is
        unknown and unrecoverable) -- this both lets Watchman start managing
        it going forward and, critically, makes it visible to
        `_reconcile_closed_positions` from the NEXT cycle onward, so its
        real eventual close DOES get a genuine `trade_records` row written
        via the normal reconciliation path, rather than fabricating one here
        for a position that has not actually closed yet (which would also
        collide with `TradeRecord.broker_ticket`'s `UNIQUE` constraint and
        silently swallow the real close record later).

        A position whose `magic` does NOT match `self._own_magic` (this
        includes the `magic=0` default `BrokerPosition` carries when the
        adapter doesn't populate it) is deliberately left alone -- could be
        a genuine manual trade a human placed directly in the terminal, and
        silently starting to MANAGE (let alone eventually auto-close) a
        human's own trade would be a much worse outcome than an occasional
        missed alert."""
        try:
            tracked_tickets = set(get_all_tracked_tickets(self._state_path))
        except CorruptPositionMetadataError:
            # Already logged loudly by _reconcile_closed_positions above this
            # same cycle (same underlying store) -- avoid a second, redundant
            # CRITICAL log for the identical root cause.
            return

        for position in open_positions:
            if position.ticket in tracked_tickets:
                continue
            # 2026-07-29 audit finding: `open_positions` here is the
            # START-of-cycle snapshot -- a position this very cycle just
            # explicitly closed (Watchman CLOSE / news-protection CLOSE_ALL,
            # which also removed its metadata) still appears in it, and
            # previously got mis-flagged as an orphan 8ms after its own
            # successful close (real occurrence: ticket=1825965537,
            # 2026-07-28 -- spurious CRITICAL alert + anomaly event, and
            # approximate metadata seeded for a CLOSED position, leaving the
            # real trade record protected only by broker_ticket's UNIQUE
            # constraint winning a write-ordering race).
            if position.ticket in self._closed_this_cycle:
                logger.debug(
                    "Watchman orphan-scan: ticket=%s symbol=%s was explicitly closed earlier this "
                    "same cycle -- not an orphan, skipping.",
                    position.ticket, position.symbol,
                )
                continue
            if position.magic != self._own_magic:
                logger.debug(
                    "Watchman orphan-scan: ticket=%s symbol=%s has no PositionMetadata and its "
                    "magic=%s does not match this system's own magic=%s -- likely a manual/"
                    "other-script trade, left unmanaged/unseeded.",
                    position.ticket, position.symbol, position.magic, self._own_magic,
                )
                continue
            try:
                self._reconcile_one_orphan_position(position, now)
            except Exception:
                logger.exception(
                    "Watchman orphan-scan: unhandled exception seeding metadata for orphan "
                    "ticket=%s symbol=%s -- skipping this ticket this cycle, other reconciliation "
                    "continues. Existing broker-side stop-loss still protects it regardless.",
                    position.ticket, position.symbol,
                )

    def _reconcile_one_orphan_position(self, position: BrokerPosition, now: datetime) -> None:
        message = (
            f"[AutoTrade] \U0001F6A8 Found an open position with NO recorded entry-time metadata: "
            f"ticket={position.ticket} {position.symbol} {position.direction} volume={position.volume} "
            f"current_sl={position.current_sl} current_price={position.current_price} -- its magic "
            f"number matches this system's own, so it was almost certainly opened by this system "
            f"(a crash between the fill and recording its Watchman metadata is the most likely "
            f"cause), but its TRUE entry price/time is unknown. Seeding approximate metadata now "
            f"(current price/SL as a stand-in) so Watchman can manage it going forward and its "
            f"eventual close is captured in the trade journal -- please verify manually."
        )
        logger.critical(message)
        notify(message)
        journal.record_anomaly_event(
            timestamp=now, event_type="orphan_position_found",
            details=(
                f"ticket={position.ticket} {position.symbol} {position.direction} "
                f"volume={position.volume} found open with no PositionMetadata -- seeded "
                f"approximate entry-time metadata from current price/SL so its eventual close is "
                f"still captured."
            ),
            db_path=self._journal_db_path,
        )
        record_position_opened(
            ticket=position.ticket, symbol=position.symbol, direction=position.direction,
            entry_price=position.current_price,
            initial_stop_distance=abs(position.current_price - position.current_sl),
            entry_swing_index=0, opened_at=now, state_path=self._state_path,
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
        inserted = journal.record_closed_trade(
            symbol=symbol, direction=direction, entry_time=entry_time, entry_price=entry_price,
            exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason, lot_size=lot_size,
            gross_pnl=gross_pnl, cost=cost, net_pnl=net_pnl, r_multiple=r_multiple,
            entry_spread_points=entry_spread_points, actual_slippage=actual_slippage,
            broker_ticket=broker_ticket, recorded_at=recorded_at,
            db_path=self._journal_db_path,
        )
        if not inserted:
            return  # swallowed duplicate write (see record_closed_trade docstring) -- do not double-notify
        # Feeds the circuit breaker's consecutive-loss/daily-realized-P&L
        # gates (2026-07-23 fix: this was never wired up anywhere in the live
        # loop -- both gates silently never fired regardless of real trade
        # outcomes; only the equity-based drawdown gate worked). Both close
        # paths funnel through this one method, so this is the single choke
        # point that sees every genuine close exactly once (the `inserted`
        # guard above already de-dupes).
        self._circuit_breaker.record_trade_close(pnl=net_pnl, closed_at=exit_time)
        notify(
            f"[AutoTrade] Trade CLOSED {symbol} {direction} entry={entry_price:.5f} "
            f"exit={exit_price:.5f} reason={exit_reason} net_pnl={net_pnl:.2f} R={r_multiple:.2f}"
        )

    def _record_explicit_close(
        self, metadata: PositionMetadata, result: OrderResult, exit_reason: journal.ExitReason, now: datetime,
    ) -> None:
        """Explicit-close path (see module docstring): computes the trade
        record directly from `PositionMetadata` + the close `OrderResult`.
        `exit_time` uses the current cycle time `now` as a proxy for the
        real fill time (not carried on `OrderResult`).

        `cost` (commission/swap): previously always `0.0` here (a documented
        trade-off -- no history query on this path). A 2026-07-29
        trade-history audit measured the real cost of that trade-off:
        journal `net_pnl`/`r_multiple` off by the actual commission+swap
        (e.g. -$1.70 on one overnight-held trade), which also skews what
        `CircuitBreaker.record_trade_close` counts toward the daily-loss/
        consecutive-loss gates -- growing with every overnight hold. Now
        best-effort fetched via the same `get_closed_trade_info()` history
        query the reconciliation path already uses: the close we just
        executed is synchronous, so its deals are essentially always
        immediately visible. ONLY `cost` is taken from it -- exit_reason
        stays the Watchman decision's own classification (history would
        re-derive a less specific one), and price/volume/gross stay from
        the `OrderResult`/metadata as before (audit confirmed those were
        already exactly right). Any failure/None falls back to the old
        `cost=0.0` behavior -- recording the close is never blocked or
        delayed on a history query (metadata is removed right after this
        returns, so there is no retry-next-cycle here, unlike
        reconciliation)."""
        cost = 0.0
        try:
            info = self._adapter.get_closed_trade_info(metadata.ticket)
            if info is not None:
                cost = info.cost
            else:
                logger.warning(
                    "explicit close ticket=%s: get_closed_trade_info returned no data -- recording "
                    "cost=0.0 (commission/swap excluded from this trade's net_pnl, the pre-2026-07-29 "
                    "behavior).",
                    metadata.ticket,
                )
        except Exception:
            logger.exception(
                "explicit close ticket=%s: get_closed_trade_info raised -- recording cost=0.0 "
                "(commission/swap excluded from this trade's net_pnl, the pre-2026-07-29 behavior).",
                metadata.ticket,
            )

        self._write_trade_record(
            symbol=metadata.symbol, direction=metadata.direction,
            entry_time=metadata.opened_at, entry_price=metadata.entry_price,
            exit_time=now, exit_price=result.filled_price, exit_reason=exit_reason,
            lot_size=result.filled_volume, gross_pnl=_signed_gross_pnl(
                direction=metadata.direction, entry_price=metadata.entry_price,
                exit_price=result.filled_price, lot_size=result.filled_volume,
                symbol_spec=self._resolve_symbol_spec(metadata.symbol),
            ),
            cost=cost, initial_stop_distance=metadata.initial_stop_distance,
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
        further per-cycle action on it) -- also True while a recent CLOSE
        failure's backoff is still active (see _CLOSE_RETRY_BACKOFF), since
        skipping the retry this cycle means "nothing further to do for this
        position this cycle" either way."""
        if decision.action == "CLOSE":
            last_failure = self._last_failed_close_attempt.get(position.ticket)
            if last_failure is not None and (now - last_failure) < _CLOSE_RETRY_BACKOFF:
                logger.debug(
                    "Watchman CLOSE ticket=%s symbol=%s -- skipping re-attempt, still within "
                    "backoff (%s ago, threshold %s) after a recent failure.",
                    position.ticket, position.symbol, now - last_failure, _CLOSE_RETRY_BACKOFF,
                )
                return True

            result = self._adapter.close_position(position.ticket)
            if result.success:
                self._record_explicit_close(
                    metadata, result, _classify_watchman_close_reason(decision.reason), now,
                )
                remove_position_metadata(position.ticket, self._state_path)
                self._closed_this_cycle.add(position.ticket)
                self._last_failed_close_attempt.pop(position.ticket, None)
                logger.info(
                    "Watchman CLOSE ticket=%s symbol=%s -- %s",
                    position.ticket, position.symbol, decision.reason,
                )
            else:
                self._last_failed_close_attempt[position.ticket] = now
                logger.error(
                    "Watchman CLOSE FAILED ticket=%s symbol=%s -- intended reason: %s; close error: %s "
                    "-- will not re-attempt for %s.",
                    position.ticket, position.symbol, decision.reason, result.message, _CLOSE_RETRY_BACKOFF,
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
                self._closed_this_cycle.add(position.ticket)
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
