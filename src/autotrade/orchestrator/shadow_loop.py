"""Continuous demo shadow-running loop (Phase 3d/5/6b/7b) -- the orchestrator
wiring that turns each newly-closed bar into, at most, one placed order via
the pipeline: `feed -> features(inside council/risk) -> council
(Bull/Bear scoring + Decision Matrix + Risk Voice veto) -> shield (portfolio
checkpoint) -> risk (CFO sizing + circuit breaker) -> execution` (spec.md
§2.2). The Auditor doesn't exist yet (a later phase) -- this loop simply
never calls it, rather than stubbing it out.

**Phase 7b: Watchman wiring.** After a successful `place_order()`, this
loop also records the position's entry-time context via
`watchman.position_metadata.record_position_opened` (ticket, symbol,
direction, ACTUAL filled entry price, the ORIGINAL `stop_distance` from the
`OrderPlan`, the same `swing_index` Shield's own cooldown check already
derived, and the current time) -- Watchman needs this to survive past this
bar's processing. If an injected `watchman_loop` (a `watchman.loop.WatchmanLoop`)
is given, its `run_cycle()` is also invoked once per polling iteration, via
`feed.poller.poll_new_bars`'s `on_iteration_end` hook, AFTER the
entry-signal processing for every symbol that iteration -- see
`watchman/loop.py`'s module docstring for why this shares the entry-signal
loop's polling cadence rather than running as a genuinely separate faster
loop (a deliberate, documented Phase 7b simplification, not an oversight).
`watchman_loop=None` (the default) skips Watchman entirely -- useful for
tests/wiring that don't care about position management.

**AutoTrading-toggle wiring.** If an injected `autotrading_watchdog` (a
`watchman.autotrading_watchdog.AutoTradingWatchdog`) is given, `_on_iteration_end()`
calls its `check()` once per polling iteration too -- same cadence as the
Watchman cycle above, i.e. near-real-time (~`poll_interval_sec`), not once
per H1 bar close. This module deliberately has zero direct `MetaTrader5`
imports, so the actual `mt5.terminal_info().trade_allowed` read happens in
`scripts/run_shadow_loop.py` instead, via the small injected
`read_autotrading_state` callable -- `_on_iteration_end()` just calls that
callable and feeds its result straight into `check()`. Both default to
`None`, which together fully disable this feature (matching
`watchman_loop=None`'s own opt-out convention).

**Risk Voice is checked twice** (Appendix A §1.5's explicit re-check
requirement): once right after a clean BUY/SELL Council decision (before
Shield -- it is Council's own internal veto gate, per spec.md §2.2's data
flow), and again immediately before `execution/`'s `place_order()` is
called. If the second check fails when the first one didn't, the trade is
cancelled and logged as `stale_signal` rather than placed.

**What the re-check actually covers, honestly:** `_risk_voice_inputs()`
(below) recomputes spread/ATR/stop-distance from the SAME already-closed
bar/history on both calls, and the two calls happen close enough in
wall-clock time that session/Friday-close almost never differ either -- so
in this pipeline, spread/ATR/stop-distance/session/Friday-close are
structurally near-identical between the two `check_risk_voice` calls, not
independently re-verified against fresh market state. The re-check's
genuine, currently-real value is the news-provider re-query, which IS
re-fetched fresh on each call and can flip pass -> veto, plus (rarely) a
session/Friday-close boundary the wall-clock happens to cross if enough
real time elapses between the two calls. Do not assume this re-check
catches, e.g., spread widening between signal-time and send-time -- it
currently doesn't, in this pipeline. See `council/risk_voice.py`'s module
docstring for the same note from the other side of this wiring.

**News-calendar limitation (read this first):** `StubNewsCalendarProvider`
(the only `NewsCalendarProvider` wired in by default -- see
`scripts/run_shadow_loop.py`) always returns "calendar unavailable", which
Risk Voice's fail-safe rule treats as "there IS news" -> veto. Until a real
provider replaces it (see `council/news_calendar.py`'s module docstring),
this loop will therefore veto **every single trade** on the news condition
alone -- a known, deliberate, conservative limitation, not a bug.

Every `borderline` Council decision (Appendix A §1.3's note) is appended as
one JSON line to `data/db/borderline_log.jsonl` (see `_log_borderline_case`)
-- same simple-file-persistence pattern as `common/kill_switch_flag.py`/
`risk/circuit_breaker.py`'s state files -- for a future Phase 8 Auditor to
replay (Appendix A §5.4).

Known simplifications, documented rather than hidden:
- The 20-day rolling averages Risk Voice needs (spread, ATR) are
  approximated from the last `ROLLING_AVG_DAYS * BARS_PER_DAY_H1` bars of
  seeded H1 history -- see `features/indicators.rolling_average`'s docstring
  for the exact bars-per-day assumption and its caveat.
- `risk.sizing.compute_lot_size`'s volatility dampening (`current_atr` /
  `avg_atr_20d`) is not wired in yet -- both are left `None`, which disables
  that extra risk-halving in high-volatility regimes. A correct 20-*trading*
  *-day* average ATR needs a bars-per-day constant per timeframe, which is
  more machinery than this Phase-3 proof point needs; revisit once a real
  volatility regime is observed on the demo account.
- `CircuitBreaker.record_trade_close()` is never called here -- there is no
  position-closing/Watchman loop yet to report closes, so the daily-loss and
  consecutive-loss gates only ever see the account-equity view fed by
  `record_equity()` (via `BrokerAdapter.get_equity()`/`get_balance()`, with
  `floating_pnl` approximated as `equity - balance`), never realized P&L
  from an individual closed trade. This under-reacts to losses until the
  Watchman (Phase 7) exists to report them -- the equity-drawdown gate still
  works in the meantime since it's equity-based, not P&L-event-based.
- Shield's `open_positions` risk_pct comes from `BrokerAdapter.get_open_positions()`,
  which for `ThrottledDemoAdapter` approximates each position's risk using
  its CURRENT distance-to-stop and CURRENT equity, not the risk actually
  intended when it was opened (MT5 doesn't retain that) -- see
  execution/demo_adapter.py's docstring for the exact formula/caveat.
- When `ShadowLoopConfig.min_lot_risk_cap_pct` is set (2026-07-22, see
  `risk.sizing.compute_lot_size`'s docstring for the fallback mechanics),
  Shield's `check()` is called (step 5, above) BEFORE CFO sizing runs (step
  6) with `new_trade_risk_pct=self._cfg.risk_per_trade_pct` -- the
  CONFIGURED risk (e.g. 1.0%), not the true risk a fallback-rescued trade
  may end up carrying (up to `min_lot_risk_cap_pct`, e.g. 1.5%). Shield's
  `total_risk_ceiling_pct` check therefore UNDERSTATES the true portfolio
  risk whenever the fallback fires. This is a real, accepted gap -- not
  silently fixed in this pass by also threading the true post-fallback risk
  into Shield; that is out of scope here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from autotrade.common import kill_switch_flag, stop_request_flag
from autotrade.common.clock import Clock
from autotrade.common.config import REPO_ROOT
from autotrade.common.symbols import SymbolSpec, get_symbol_spec
from autotrade.council.decision_matrix import BorderlineCase, evaluate_council
from autotrade.council.news_calendar import NewsCalendarProvider, StubNewsCalendarProvider
from autotrade.council.risk_voice import RiskVoiceConfig, check_risk_voice
from autotrade.execution.adapter import BrokerAdapter, TradeRequest
from autotrade.feed.poller import poll_new_bars
from autotrade.feed.snapshot import Bar, MarketSnapshot
from autotrade.features.indicators import atr, rolling_average
from autotrade.features.swing import latest_confirmed_swing_high, latest_confirmed_swing_low
from autotrade.notify.telegram import notify
from autotrade.risk.circuit_breaker import CircuitBreaker
from autotrade.risk.sizing import compute_lot_size
from autotrade.shield.checkpoint import OpenPositionInfo, Shield
from autotrade.store import journal
from autotrade.watchman import position_metadata
from autotrade.watchman.autotrading_watchdog import AutoTradingWatchdog
from autotrade.watchman.loop import WatchmanLoop

logger = logging.getLogger(__name__)

DEFAULT_BORDERLINE_LOG_PATH = REPO_ROOT / "data" / "db" / "borderline_log.jsonl"


class _StopLoopRequested(Exception):
    """Internal sentinel raised by `ShadowLoop._check_stop_request()` to
    break out of `feed.poller.poll_new_bars`'s otherwise-unconditional loop
    once a graceful stop has been requested (`common/stop_request_flag.py`).
    `poll_new_bars` has no built-in early-exit mechanism (by design -- it's a
    plain blocking loop), so this reuses the same "raise through the
    on_iteration_end hook, catch it in run()" approach rather than modifying
    that lower-level module. Never escapes `ShadowLoop.run()`."""

# council/decision_matrix.py's four `borderline_reason` values, mapped to
# store/journal.py's finer-grained BlockSource vocabulary -- fine enough to
# distinguish Appendix A §5.1's "borderline no-trade" bucket into its actual
# sub-cases for the daily report.
_BORDERLINE_BLOCK_SOURCES: dict[str, journal.BlockSource] = {
    "no conviction": "borderline_no_conviction",
    "conflicting signals": "borderline_conflicting",
    "strong-but-not-negated": "borderline_strong_not_negated",
    "near-threshold": "borderline_near_threshold",
}

# `rolling_average`/`ROLLING_AVG_DAYS`/`BARS_PER_DAY_H1` now live in
# `features/indicators.py` (shared with `backtest/engine.py`'s replay of this
# same Risk Voice re-check, so both stay consistent by construction). See
# that module's docstring for the exact bars-per-day approximation and its
# known caveat.


def _log_borderline_case(case: BorderlineCase, path: Path) -> None:
    """Append one borderline case as a single JSON line to `path` (JSONL) --
    what a future Phase 8 Auditor will read to replay borderline cases
    (Appendix A §5.4)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(case)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


@dataclass(frozen=True)
class ShadowLoopConfig:
    """Strategy/risk parameters for one shadow-loop run -- values come from
    `config/base.yaml`'s `cfo:` / `order:` blocks (see scripts/run_shadow_loop.py),
    kept here rather than read from YAML directly so this module stays
    testable without touching the filesystem."""

    risk_per_trade_pct: float
    sl_buffer_atr: float = 0.2
    sl_min_atr: float = 0.8
    sl_max_atr: float = 2.5
    tp_r_multiple: float = 2.0
    pivot_bars: int = 3
    bull_threshold: int = 70
    bear_threshold: int = 70
    conflict_threshold: int = 55
    max_history_bars: int = 500
    min_lot_risk_cap_pct: float | None = None
    """`None` (the default) means `risk.sizing.compute_lot_size`'s min-lot
    risk-cap fallback is NOT modeled -- spec-exact §3.1 behavior, zero
    change. See that function's docstring for the exact deliberate-deviation
    mechanics; `config/base.yaml`'s `cfo.min_lot_risk_cap_pct: 1.5` is the
    adopted live value, threaded through by `scripts/run_shadow_loop.py`. See
    this module's "Known simplifications" section above for the accepted gap
    this creates with Shield's `total_risk_ceiling_pct` check."""


def _append_bar(history: pd.DataFrame, bar: Bar, max_bars: int) -> pd.DataFrame:
    """Append one closed bar to the rolling history, trimmed to `max_bars`
    (contiguous 0..n-1 index throughout -- required by features/swing.py's
    positional `as_of_index` contract)."""
    new_row = pd.DataFrame([{
        "time": bar.time, "open": bar.open, "high": bar.high,
        "low": bar.low, "close": bar.close,
        "tick_volume": bar.tick_volume, "spread": bar.spread,
    }])
    history = pd.concat([history, new_row], ignore_index=True)
    if len(history) > max_bars:
        history = history.iloc[-max_bars:].reset_index(drop=True)
    return history


class ShadowLoop:
    """Wires feed -> council(Bull/Bear scoring + Decision Matrix + Risk
    Voice) -> shield -> risk -> execution for one or more symbols. `adapter`
    is injected so the exact same loop runs against `NoOpBrokerAdapter`
    (safe dry-run) or `ThrottledDemoAdapter` (real demo orders) unmodified.

    `initial_history` must contain a seeded DataFrame (columns at least
    `time, open, high, low, close`) per symbol this loop will be asked to
    process -- see scripts/run_shadow_loop.py for how that's fetched from
    MT5 on startup. EMA20/50/200 and swing detection both need real history,
    not a single bar.

    `news_provider` defaults to `StubNewsCalendarProvider` (always vetoes,
    see module docstring's news-calendar limitation) and `risk_voice_cfg`
    defaults to `RiskVoiceConfig()`'s defaults if not given.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        circuit_breaker: CircuitBreaker,
        shield: Shield,
        cfg: ShadowLoopConfig,
        initial_history: dict[str, pd.DataFrame],
        clock: Clock,
        resolve_symbol_spec: Callable[[str], SymbolSpec] | None = None,
        symbol_map: dict[str, str] | None = None,
        news_provider: NewsCalendarProvider | None = None,
        risk_voice_cfg: RiskVoiceConfig | None = None,
        borderline_log_path: Path | None = None,
        watchman_loop: WatchmanLoop | None = None,
        position_metadata_path: Path | None = None,
        journal_db_path: Path | None = None,
        autotrading_watchdog: AutoTradingWatchdog | None = None,
        read_autotrading_state: Callable[[], bool | None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._circuit_breaker = circuit_breaker
        self._shield = shield
        self._cfg = cfg
        self._history = {symbol: df.reset_index(drop=True) for symbol, df in initial_history.items()}
        self._resolve_symbol_spec = resolve_symbol_spec or (
            lambda symbol: get_symbol_spec(symbol, symbol_map)
        )
        self._clock = clock
        self._news_provider = news_provider or StubNewsCalendarProvider()
        self._risk_voice_cfg = risk_voice_cfg or RiskVoiceConfig()
        self._borderline_log_path = borderline_log_path or DEFAULT_BORDERLINE_LOG_PATH
        self._watchman_loop = watchman_loop
        self._position_metadata_path = position_metadata_path
        self._journal_db_path = journal_db_path
        self._autotrading_watchdog = autotrading_watchdog
        self._read_autotrading_state = read_autotrading_state or (lambda: None)
        self._peak_equity: float | None = None
        self._live_start_equity: float | None = None

    def run(
        self,
        symbols: list[str],
        timeframe: str,
        poll_interval_sec: float = 5.0,
        max_iterations: int | None = None,
    ) -> None:
        """Blocking call -- runs `feed.poller.poll_new_bars` forever (or for
        `max_iterations`), dispatching every new closed bar to `on_new_bar`.
        If a `watchman_loop` was injected, its per-cycle position management
        also runs once per iteration (see module docstring).

        A pending stop-request flag left over from a previous, abnormally-
        ended session is cleared before entering the polling loop -- a stale
        flag from last time must never instantly stop a fresh run. Once
        running, `common/stop_request_flag.py` is checked once per full poll
        cycle (not per symbol/per-signal-evaluation, unlike the kill switch
        in `_process_bar`) via the same `on_iteration_end` hook the Watchman
        cycle uses; when triggered, this clears the flag, notifies (with the
        open-position count), logs, and returns promptly."""
        if stop_request_flag.is_requested():
            logger.warning(
                "Stale stop-request flag found at startup (left over from a previous session) -- "
                "clearing it so it can't instantly stop this fresh run."
            )
            stop_request_flag.clear()

        try:
            poll_new_bars(
                symbols, timeframe, self.on_new_bar,
                poll_interval_sec=poll_interval_sec, max_iterations=max_iterations,
                on_iteration_end=self._on_iteration_end,
            )
        except _StopLoopRequested:
            pass

    def _on_iteration_end(self) -> None:
        # Broad except here, deliberately -- matches `_process()`'s own
        # per-bar guard below (2026-07-28 audit finding: this was the one
        # place that pattern wasn't applied). Without it, an unexpected
        # exception from the Watchman cycle or the AutoTrading-toggle check
        # -- e.g. a state-file error `_reconcile_closed_positions`/
        # `_reconcile_orphan_positions`'s own inner guards don't happen to
        # classify as `CorruptPositionMetadataError` -- would propagate all
        # the way out of poll_new_bars() and kill the ENTIRE shadow loop
        # process, not just skip one iteration: open-position management
        # (SL trailing, news protection, reconciliation) then goes fully
        # dark until the heartbeat notices and restarts it (up to ~10
        # minutes), and if the cause is persistent, every restart
        # immediately crash-loops again. `_check_stop_request()` is
        # deliberately OUTSIDE this try block -- it must keep raising
        # `_StopLoopRequested` to actually stop the loop; it already has its
        # own internal exception handling (see its own docstring) and must
        # never be swallowed by this one.
        try:
            if self._watchman_loop is not None:
                self._run_watchman_cycle()
            if self._autotrading_watchdog is not None:
                self._autotrading_watchdog.check(self._read_autotrading_state())
        except Exception:
            logger.exception(
                "Watchman cycle / AutoTrading-toggle check raised an unexpected exception -- logged and "
                "skipped for this iteration rather than crashing the whole shadow loop. Position "
                "management resumes next iteration; if this keeps recurring, investigate -- it will log "
                "here every cycle rather than going silent."
            )
        self._check_stop_request()

    def _run_watchman_cycle(self) -> None:
        self._watchman_loop.run_cycle(self._history, self._clock.now())

    def _check_stop_request(self) -> None:
        if not stop_request_flag.is_requested():
            return

        # get_open_positions() can raise (e.g. ThrottledDemoAdapter surfaces
        # a transient MT5 positions_get()/account_info() hiccup as a plain
        # RuntimeError) -- a requested stop must still actually happen even
        # if the position count can't be determined, so this is caught and
        # degraded to "unknown" rather than left to crash the whole loop
        # with an uncaught exception (nothing between here and run() catches
        # a plain RuntimeError, only _StopLoopRequested).
        try:
            count: int | None = len(self._adapter.get_open_positions())
        except Exception:
            logger.exception(
                "Graceful stop requested, but get_open_positions() failed -- stopping anyway with "
                "an unknown open-position count rather than letting this crash the stop itself."
            )
            count = None

        # Only cleared once the stop is actually about to happen (position
        # count resolved or degraded to unknown, notify about to fire) --
        # never before, so a get_open_positions() failure can never "consume"
        # the user's stop request without the stop actually completing.
        stop_request_flag.clear()

        if count is None:
            notify(
                "[AutoTrade] \U0001F6D1 AutoTrade stopped (graceful stop requested) -- open-position "
                "count could not be determined (check the terminal manually)."
            )
        elif count == 0:
            notify("[AutoTrade] \U0001F6D1 AutoTrade stopped (graceful stop requested) -- no open positions.")
        else:
            notify(
                f"[AutoTrade] \U0001F6D1 AutoTrade stopped (graceful stop requested) -- {count} "
                "position(s) still open and will NOT be managed until restarted. Use the "
                "emergency stop button if you need them closed."
            )
        logger.info(
            "Graceful stop requested -- exiting shadow loop poll cycle (open_positions=%s)",
            count if count is not None else "unknown",
        )
        raise _StopLoopRequested()

    def on_new_bar(self, snapshot: MarketSnapshot) -> None:
        if snapshot.symbol not in self._history:
            raise KeyError(
                f"No seeded history for symbol {snapshot.symbol!r} -- "
                "seed initial_history for every symbol before starting the loop"
            )
        self._history[snapshot.symbol] = self._process(snapshot)

    def _process(self, snapshot: MarketSnapshot) -> pd.DataFrame:
        """Wraps `_process_bar()` in a broad `except Exception` -- a transient
        MT5 hiccup (get_equity()/place_order() raising, symbol spec lookup
        failing, ...) must skip THIS bar's processing (log loudly, no new
        entry attempted) and let the loop keep running to retry on the next
        bar, per spec.md §7 ("halt new entries ... never silently continue",
        not "tear down the whole loop"). Anything not a plain Exception
        (KeyboardInterrupt, ...) still propagates."""
        history = self._history[snapshot.symbol]
        try:
            return self._process_bar(snapshot, history)
        except Exception:
            logger.exception(
                "%s %s: unhandled exception while processing this bar -- skipping, "
                "no new entry attempted, loop continues", snapshot.symbol, snapshot.bar.time,
            )
            return history

    def _process_bar(self, snapshot: MarketSnapshot, history: pd.DataFrame) -> pd.DataFrame:
        symbol = snapshot.symbol
        bar = snapshot.bar

        # 1. Kill switch -- checked first, before anything else. Halted means
        # halted: no equity recording, no circuit-breaker check, no signal.
        if kill_switch_flag.is_active():
            status = kill_switch_flag.get_status()
            reason = status.get("reason") if status else "unknown"
            logger.warning(
                "%s %s: KILL SWITCH ACTIVE (reason=%r) -- skipping, no signal evaluated",
                symbol, bar.time, reason,
            )
            return history

        # 2. Circuit breaker -- fed a fresh equity snapshot every bar close
        # (at least once per loop iteration), then checked before any entry.
        # floating_pnl is approximated as equity - balance (no Watchman/
        # position-tracking exists yet to report it directly).
        equity = self._adapter.get_equity()
        balance = self._adapter.get_balance()
        self._peak_equity = equity if self._peak_equity is None else max(self._peak_equity, equity)
        if self._live_start_equity is None:
            self._live_start_equity = equity
        self._circuit_breaker.record_equity(
            equity=equity, peak_equity=self._peak_equity,
            live_start_equity=self._live_start_equity, as_of=self._clock.now(),
            floating_pnl=equity - balance,
        )

        cb_state = self._circuit_breaker.check(self._clock)
        if cb_state.blocks_new_entries:
            reasons = [
                r for r in (cb_state.daily_loss_reason, cb_state.consecutive_loss_reason, cb_state.drawdown_reason)
                if r
            ]
            logger.warning(
                "%s %s: circuit breaker blocks new entries -- %s", symbol, bar.time, "; ".join(reasons),
            )
            return history

        if len(history) and pd.Timestamp(history["time"].iloc[-1]) == pd.Timestamp(bar.time):
            logger.info("%s %s: bar already in seeded history, skipping duplicate", symbol, bar.time)
            return history

        # 3. Append + evaluate via the real Council (Bull/Bear scoring +
        # Decision Matrix, Appendix A §1.1-§1.3).
        history = _append_bar(history, bar, self._cfg.max_history_bars)
        as_of_index = len(history) - 1
        symbol_spec = self._resolve_symbol_spec(symbol)

        council_decision, borderline_case = evaluate_council(
            history, as_of_index, symbol, symbol_spec,
            bull_threshold=self._cfg.bull_threshold, bear_threshold=self._cfg.bear_threshold,
            conflict_threshold=self._cfg.conflict_threshold,
            sl_buffer_atr=self._cfg.sl_buffer_atr, sl_min_atr=self._cfg.sl_min_atr,
            sl_max_atr=self._cfg.sl_max_atr, tp_r_multiple=self._cfg.tp_r_multiple,
            pivot_bars=self._cfg.pivot_bars,
        )

        if borderline_case is not None:
            _log_borderline_case(borderline_case, self._borderline_log_path)
            logger.info(
                "%s %s: borderline case logged (%s) -- no trade",
                symbol, bar.time, council_decision.borderline_reason,
            )

        if council_decision.direction is None:
            reason = council_decision.borderline_reason or "no conviction"
            logger.info("%s %s: no council decision (%s)", symbol, bar.time, reason)
            journal.record_blocked_signal(
                timestamp=bar.time, symbol=symbol,
                block_source=_BORDERLINE_BLOCK_SOURCES.get(reason, "borderline_no_conviction"),
                reason=reason,
                direction=borderline_case.hypothetical_direction if borderline_case is not None else None,
                db_path=self._journal_db_path,
            )
            return history

        plan = council_decision.order_plan
        if plan is None:
            logger.info(
                "%s %s: %s decision but no confirmed swing available yet -- "
                "no stop-loss anchor, skipping", symbol, bar.time, council_decision.direction,
            )
            return history

        # 4. Risk Voice -- Council's own veto gate (Appendix A §1.5), checked
        # right after a clean BUY/SELL decision and BEFORE Shield: it is
        # council's internal veto, not shield's portfolio-level filtering
        # (spec.md §2.2's data-flow ordering).
        risk_voice_decision = check_risk_voice(
            symbol=symbol, order_plan=plan, news_provider=self._news_provider,
            clock=self._clock, config=self._risk_voice_cfg,
            **self._risk_voice_inputs(history, as_of_index),
        )
        if risk_voice_decision.vetoed:
            logger.warning(
                "%s %s: Risk Voice vetoed %s trade -- %s",
                symbol, bar.time, plan.direction, "; ".join(risk_voice_decision.reasons),
            )
            journal.record_blocked_signal(
                timestamp=bar.time, symbol=symbol, block_source="risk_voice",
                reason="; ".join(risk_voice_decision.reasons), direction=plan.direction,
                db_path=self._journal_db_path,
            )
            return history

        # 5. Shield -- portfolio-level checkpoint, before CFO sizing (spec.md
        # §2.2: council -> shield -> risk). Re-derives the same swing
        # evaluate_council() used internally -- cheap, and CouncilDecision
        # doesn't currently expose the swing index it found.
        if plan.direction == "BUY":
            swing = latest_confirmed_swing_low(history, as_of_index, pivot_bars=self._cfg.pivot_bars)
        else:
            swing = latest_confirmed_swing_high(history, as_of_index, pivot_bars=self._cfg.pivot_bars)
        swing_index = swing[0]

        open_positions = [
            OpenPositionInfo(symbol=pos.symbol, direction=pos.direction, risk_pct=pos.risk_pct)
            for pos in self._adapter.get_open_positions()
        ]
        shield_decision = self._shield.check(
            order_plan=plan, symbol=symbol, open_positions=open_positions,
            new_trade_risk_pct=self._cfg.risk_per_trade_pct, swing_index=swing_index, clock=self._clock,
        )
        if shield_decision.blocked:
            logger.warning(
                "%s %s: Shield blocked %s trade -- %s",
                symbol, bar.time, plan.direction, "; ".join(shield_decision.reasons),
            )
            journal.record_blocked_signal(
                timestamp=bar.time, symbol=symbol, block_source="shield",
                reason="; ".join(shield_decision.reasons), direction=plan.direction,
                db_path=self._journal_db_path,
            )
            return history

        # 6. CFO sizing, then place (or skip below broker minimum).
        point_value = symbol_spec.tick_value / symbol_spec.tick_size
        lot = compute_lot_size(
            equity=equity, risk_per_trade_pct=self._cfg.risk_per_trade_pct,
            entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
            volume_min=symbol_spec.volume_min, volume_max=symbol_spec.volume_max,
            volume_step=symbol_spec.volume_step,
            min_lot_risk_cap_pct=self._cfg.min_lot_risk_cap_pct,
        )
        if lot is None:
            logger.info(
                "%s %s: %s signal with confirmed swing, but computed lot size below broker "
                "minimum %.2f -- no trade placed", symbol, bar.time, plan.direction, symbol_spec.volume_min,
            )
            return history
        if self._cfg.min_lot_risk_cap_pct is not None:
            # Detect whether the fallback actually fired -- i.e. whether the
            # risk-based lot alone (cap disabled) would have been None --
            # by re-running the same real compute_lot_size call without the
            # cap, rather than inferring it from `lot == volume_min` (which
            # would also false-positive whenever the risk-based lot
            # naturally rounds to exactly volume_min on its own).
            lot_without_fallback = compute_lot_size(
                equity=equity, risk_per_trade_pct=self._cfg.risk_per_trade_pct,
                entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
                volume_min=symbol_spec.volume_min, volume_max=symbol_spec.volume_max,
                volume_step=symbol_spec.volume_step,
            )
            if lot_without_fallback is None:
                logger.warning(
                    "%s %s: %s signal's risk-based lot was below broker minimum %.2f -- rescued to "
                    "the minimum lot via the min_lot_risk_cap_pct=%.2f%% fallback (risks up to that "
                    "%% of equity, above the configured risk_per_trade_pct=%.2f%%)",
                    symbol, bar.time, plan.direction, symbol_spec.volume_min,
                    self._cfg.min_lot_risk_cap_pct, self._cfg.risk_per_trade_pct,
                )

        # 7. Re-check Risk Voice immediately before sending the order
        # (Appendix A §1.5's explicit re-check requirement): spread/news can
        # go stale between signal evaluation and order placement. No live
        # tick-level spread source exists in this pipeline yet, so this
        # reuses the same bar-close spread -- the news provider IS re-queried
        # fresh, the part that can genuinely change between the two calls.
        recheck_inputs = self._risk_voice_inputs(history, as_of_index)
        recheck_decision = check_risk_voice(
            symbol=symbol, order_plan=plan, news_provider=self._news_provider,
            clock=self._clock, config=self._risk_voice_cfg, **recheck_inputs,
        )
        if recheck_decision.vetoed:
            logger.warning(
                "%s %s: stale_signal -- Risk Voice re-check failed immediately before "
                "order placement -- %s", symbol, bar.time, "; ".join(recheck_decision.reasons),
            )
            journal.record_blocked_signal(
                timestamp=bar.time, symbol=symbol, block_source="risk_voice",
                reason="stale_signal: " + "; ".join(recheck_decision.reasons), direction=plan.direction,
                db_path=self._journal_db_path,
            )
            return history

        request = TradeRequest(
            symbol=symbol, direction=plan.direction, lot_size=lot,
            entry=plan.entry, stop_loss=plan.stop_loss, take_profit=plan.take_profit,
        )
        # current_atr (Phase 7b, Appendix A §4.8's abnormal-slippage check)
        # reuses the exact same ATR value the re-check above just computed --
        # no reason to recompute it a third time from the same bar.
        result = self._adapter.place_order(request, current_atr=recheck_inputs["current_atr"])
        opened_at = self._clock.now()
        if result.success:
            logger.info(
                "%s %s: order PLACED %s lot=%.2f entry=%.5f sl=%.5f tp=%.5f -- %s",
                symbol, bar.time, plan.direction, lot, plan.entry, plan.stop_loss, plan.take_profit, result.message,
            )
            notify(
                f"[AutoTrade] Trade OPENED {symbol} {plan.direction} lot={lot:.2f} "
                f"entry={plan.entry:.5f} sl={plan.stop_loss:.5f} tp={plan.take_profit:.5f} "
                f"filled={result.filled_price}"
            )
            self._shield.record_trade_opened(
                symbol=symbol, direction=plan.direction, opened_at=opened_at, swing_index=swing_index,
            )
        else:
            logger.warning("%s %s: order REJECTED -- %s", symbol, bar.time, result.message)

        # Phase 7b: Watchman needs this position's entry-time context to
        # survive past this bar -- entry price is the ACTUAL fill
        # (result.filled_price), stop_distance is the ORIGINAL plan value
        # (never a post-modification one, per
        # watchman/position_metadata.py's module docstring). Recorded
        # whenever a real broker ticket exists AND the position is actually
        # open on the broker -- either the normal success path, OR the
        # compounding-failure path where the entry fill succeeded but the
        # abnormal-slippage auto-close then failed even after a retry
        # (execution/demo_adapter.py's `OrderResult.position_still_open`):
        # that position is genuinely open and would otherwise be
        # permanently invisible to Watchman. Skipped when there's no real
        # broker ticket (NoOpBrokerAdapter dry runs never open a position
        # with any broker) or when the position was actually closed (a
        # normal rejection, or a slippage-close that DID succeed).
        #
        # 2026-07-29: ticket=1825965537 APPEARED to have had this write
        # silently never happen (an "orphan position" alert fired for it) --
        # a same-day trade-history audit then proved this write actually
        # SUCCEEDED for that ticket, and the alert was a false positive from
        # watchman/loop.py's `_reconcile_orphan_positions` re-using its
        # start-of-cycle `open_positions` snapshot after an explicit close
        # in the same cycle (fixed there via `_closed_this_cycle`). The
        # explicit success/failure logging below predates that correction
        # and stays: this function previously had NO log line at all for
        # the success path (silence was the only signal), which is exactly
        # why the false alarm was initially believed -- and a REAL failure
        # of this write remains possible and would otherwise still be
        # undiagnosable. Caught-and-logged rather than re-raised (the
        # broker-side position is genuinely open either way); the orphan
        # reconciliation is the safety net of last resort if a real write
        # failure ever occurs.
        if result.broker_ticket is not None and (result.success or result.position_still_open):
            # entry_spread_points/actual_slippage (Appendix A §5.1's daily-
            # report fields) -- entry_spread_points reuses the same
            # bar-close spread reading the immediately-preceding Risk Voice
            # re-check just computed (no live tick-level spread source
            # exists in this pipeline yet, see _risk_voice_inputs's
            # docstring); actual_slippage is the ACTUAL fill vs. the
            # intended entry price, `None` if no fill price is known.
            actual_slippage = (
                abs(result.filled_price - plan.entry) if result.filled_price is not None else None
            )
            try:
                position_metadata.record_position_opened(
                    ticket=result.broker_ticket, symbol=symbol, direction=plan.direction,
                    entry_price=result.filled_price, initial_stop_distance=plan.stop_distance,
                    entry_swing_index=swing_index, opened_at=opened_at,
                    state_path=self._position_metadata_path,
                    entry_spread_points=recheck_inputs["current_spread_points"],
                    actual_slippage=actual_slippage,
                )
                logger.info(
                    "%s %s: Watchman position metadata recorded for ticket=%s (entry=%s at %s)",
                    symbol, bar.time, result.broker_ticket, result.filled_price, opened_at,
                )
            except Exception:
                logger.exception(
                    "%s %s: FAILED to record Watchman position metadata for ticket=%s -- the "
                    "broker-side position is open regardless, but Watchman will not manage/track it "
                    "until its own orphan-position reconciliation catches it later (seeding "
                    "approximate metadata from whatever price/SL are current AT THAT LATER TIME, "
                    "not this bar's real entry price/time=%s/%s). Investigate promptly.",
                    symbol, bar.time, result.broker_ticket, result.filled_price, opened_at,
                )

        return history

    def _risk_voice_inputs(self, history: pd.DataFrame, as_of_index: int) -> dict[str, float]:
        """Cheap-to-recompute inputs `check_risk_voice` needs beyond the
        `OrderPlan` itself -- computed fresh on every call (both the
        signal-time check and the order-send-time re-check) rather than
        cached, since a re-check that reused stale numbers would defeat its
        own purpose."""
        closes = history["close"].iloc[: as_of_index + 1]
        highs = history["high"].iloc[: as_of_index + 1]
        lows = history["low"].iloc[: as_of_index + 1]
        atr_series = atr(highs, lows, closes)
        return {
            "current_spread_points": float(history["spread"].iloc[as_of_index]),
            "avg_spread_points_20d": rolling_average(history["spread"], as_of_index),
            "current_atr": float(atr_series.iloc[-1]),
            "avg_atr_20d": rolling_average(atr_series, as_of_index),
        }
