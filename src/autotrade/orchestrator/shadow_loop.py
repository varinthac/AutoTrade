"""Continuous demo shadow-running loop (Phase 3d) -- the orchestrator wiring
that turns each newly-closed bar into, at most, one placed order via the
Phase-3 pipeline: `feed -> features(inside council/risk) -> council
(trivial_signal) -> risk (CFO sizing + circuit breaker) -> execution`
(spec.md §2.2). Shield/Watchman/Auditor don't exist yet (later phases) --
this loop simply never calls them, rather than stubbing them out.

Known simplifications, documented rather than hidden:
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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from autotrade.common import kill_switch_flag
from autotrade.common.clock import Clock, RealClock
from autotrade.common.symbols import SymbolSpec, get_symbol_spec
from autotrade.council.trivial_signal import build_trade_idea, generate_trivial_signal
from autotrade.execution.adapter import BrokerAdapter, TradeRequest
from autotrade.feed.poller import poll_new_bars
from autotrade.feed.snapshot import Bar, MarketSnapshot
from autotrade.risk.circuit_breaker import CircuitBreaker
from autotrade.risk.sizing import compute_lot_size

logger = logging.getLogger(__name__)


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
    max_history_bars: int = 500


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
    """Wires feed -> council(trivial_signal) -> risk -> execution for one or
    more symbols. `adapter` is injected so the exact same loop runs against
    `NoOpBrokerAdapter` (safe dry-run) or `ThrottledDemoAdapter` (real demo
    orders) unmodified.

    `initial_history` must contain a seeded DataFrame (columns at least
    `time, open, high, low, close`) per symbol this loop will be asked to
    process -- see scripts/run_shadow_loop.py for how that's fetched from
    MT5 on startup. EMA20/50 and swing detection both need real history, not
    a single bar.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        circuit_breaker: CircuitBreaker,
        cfg: ShadowLoopConfig,
        initial_history: dict[str, pd.DataFrame],
        resolve_symbol_spec: Callable[[str], SymbolSpec] | None = None,
        symbol_map: dict[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._adapter = adapter
        self._circuit_breaker = circuit_breaker
        self._cfg = cfg
        self._history = {symbol: df.reset_index(drop=True) for symbol, df in initial_history.items()}
        self._resolve_symbol_spec = resolve_symbol_spec or (
            lambda symbol: get_symbol_spec(symbol, symbol_map)
        )
        self._clock = clock or RealClock()
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
        `max_iterations`), dispatching every new closed bar to `on_new_bar`."""
        poll_new_bars(
            symbols, timeframe, self.on_new_bar,
            poll_interval_sec=poll_interval_sec, max_iterations=max_iterations,
        )

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

        # 3. Append + evaluate the trivial signal.
        history = _append_bar(history, bar, self._cfg.max_history_bars)
        as_of_index = len(history) - 1

        direction = generate_trivial_signal(history, as_of_index)
        if direction is None:
            logger.info("%s %s: no signal (no EMA crossover)", symbol, bar.time)
            return history

        plan = build_trade_idea(
            history, as_of_index,
            sl_buffer_atr=self._cfg.sl_buffer_atr, sl_min_atr=self._cfg.sl_min_atr,
            sl_max_atr=self._cfg.sl_max_atr, tp_r_multiple=self._cfg.tp_r_multiple,
            pivot_bars=self._cfg.pivot_bars,
        )
        if plan is None:
            logger.info(
                "%s %s: %s crossover fired but no confirmed swing available yet -- "
                "no stop-loss anchor, skipping", symbol, bar.time, direction,
            )
            return history

        # 4. CFO sizing, then place (or skip below broker minimum).
        spec = self._resolve_symbol_spec(symbol)
        point_value = spec.tick_value / spec.tick_size
        lot = compute_lot_size(
            equity=equity, risk_per_trade_pct=self._cfg.risk_per_trade_pct,
            entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
            volume_min=spec.volume_min, volume_max=spec.volume_max, volume_step=spec.volume_step,
        )
        if lot is None:
            logger.info(
                "%s %s: %s signal with confirmed swing, but computed lot size below broker "
                "minimum %.2f -- no trade placed", symbol, bar.time, plan.direction, spec.volume_min,
            )
            return history

        request = TradeRequest(
            symbol=symbol, direction=plan.direction, lot_size=lot,
            entry=plan.entry, stop_loss=plan.stop_loss, take_profit=plan.take_profit,
        )
        result = self._adapter.place_order(request)
        if result.success:
            logger.info(
                "%s %s: order PLACED %s lot=%.2f entry=%.5f sl=%.5f tp=%.5f -- %s",
                symbol, bar.time, plan.direction, lot, plan.entry, plan.stop_loss, plan.take_profit, result.message,
            )
        else:
            logger.warning("%s %s: order REJECTED -- %s", symbol, bar.time, result.message)

        return history
