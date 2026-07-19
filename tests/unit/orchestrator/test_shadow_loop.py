"""Tests for orchestrator/shadow_loop.py -- the Phase 3d wiring of
feed -> council(trivial_signal) -> risk(CFO sizing) -> execution. MT5,
kill_switch_flag, and the broker are all faked/monkeypatched; this exercises
the orchestration logic only (feed/poller, MT5, and live signal generation
are covered by their own module's tests).

The crossover fixture mirrors tests/unit/council/test_trivial_signal.py's
`_flat_then_jump_df` exactly (closes flat at `pre_level` through index 69,
jump to `post_level` from index 70 on) so the EMA20/EMA50 crossover location
(index 70) is already independently verified there, rather than re-derived
here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from autotrade.execution.adapter import BrokerAdapter, OrderResult, TradeRequest
from autotrade.common.symbols import SymbolSpec
from autotrade.feed.snapshot import Bar, MarketSnapshot
from autotrade.orchestrator import shadow_loop as shadow_loop_module
from autotrade.orchestrator.shadow_loop import ShadowLoop, ShadowLoopConfig
from autotrade.risk.circuit_breaker import CircuitBreaker

CROSS_INDEX = 70
BASE_TIME = datetime(2026, 1, 1)


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeAdapter(BrokerAdapter):
    def __init__(self, equity: float = 10_000.0, balance: float | None = None):
        self._equity = equity
        self._balance = equity if balance is None else balance
        self.place_order_calls: list[TradeRequest] = []

    def place_order(self, request: TradeRequest) -> OrderResult:
        self.place_order_calls.append(request)
        return OrderResult(
            success=True, broker_ticket=1, filled_price=request.entry,
            filled_volume=request.lot_size, retcode=None, message="ok",
        )

    def get_equity(self) -> float:
        return self._equity

    def get_balance(self) -> float:
        return self._balance


def _fake_symbol_spec(symbol: str) -> SymbolSpec:
    return SymbolSpec(
        canonical=symbol, broker_name=symbol, digits=2, point=0.01,
        tick_size=0.01, tick_value=1.0, contract_size=100.0,
        volume_min=0.01, volume_max=50.0, volume_step=0.01,
        trade_stops_level=0, freeze_level=0,
    )


def _flat_then_jump_df(pre_level: float, post_level: float, swing_dip_index: int | None = None,
                        swing_is_high: bool = False, post_bars: int = 5) -> pd.DataFrame:
    closes = [pre_level] * CROSS_INDEX + [post_level] * post_bars
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    if swing_dip_index is not None:
        if swing_is_high:
            highs[swing_dip_index] = pre_level + 20
        else:
            lows[swing_dip_index] = pre_level - 20
    times = [BASE_TIME + timedelta(hours=i) for i in range(len(closes))]
    return pd.DataFrame({
        "time": times, "open": closes, "high": highs, "low": lows, "close": closes,
        "tick_volume": [10] * len(closes), "spread": [1] * len(closes),
    })


def _bar_from_row(df: pd.DataFrame, index: int) -> Bar:
    row = df.iloc[index]
    return Bar(
        time=row["time"], open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        tick_volume=int(row["tick_volume"]), spread=int(row["spread"]),
    )


def _default_loop(adapter, circuit_breaker, seed_history, clock=None) -> ShadowLoop:
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    return ShadowLoop(
        adapter=adapter, circuit_breaker=circuit_breaker, cfg=cfg,
        initial_history={"XAUUSD": seed_history}, resolve_symbol_spec=_fake_symbol_spec,
        clock=clock or FixedClock(BASE_TIME),
    )


def test_kill_switch_active_blocks_adapter_and_evaluates_no_signal(monkeypatch):
    monkeypatch.setattr(shadow_loop_module.kill_switch_flag, "is_active", lambda: True)
    monkeypatch.setattr(
        shadow_loop_module.kill_switch_flag, "get_status",
        lambda: {"reason": "manual halt for test", "activated_at": "2026-01-01T00:00:00"},
    )

    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    # History must be untouched -- kill switch is checked before anything else.
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX


def test_circuit_breaker_tripped_blocks_adapter():
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter(equity=9_000.0)

    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    # Pre-trip the drawdown gate -- 10% down from a 10,000 peak, well past
    # the 8% threshold. The drawdown gate is one-way (never auto-clears), so
    # this stays tripped regardless of what ShadowLoop later records.
    breaker.record_equity(equity=9_000.0, peak_equity=10_000.0, live_start_equity=10_000.0, as_of=BASE_TIME)

    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX


def test_floating_pnl_from_equity_minus_balance_feeds_daily_loss_gate():
    # equity - balance approximates floating P&L (no Watchman/position
    # tracking exists yet to report it directly) -- confirms ShadowLoop
    # actually wires this into record_equity() rather than defaulting to 0.0.
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter(equity=9_900.0, balance=10_000.0)  # -100 floating P&L
    breaker = CircuitBreaker(daily_loss_limit_pct=1.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []  # ~1.01% floating loss >= 1.0% limit


def test_no_signal_does_not_call_adapter_or_raise():
    # Flat closes throughout -- no EMA crossover ever fires.
    times = [BASE_TIME + timedelta(hours=i) for i in range(60)]
    seed = pd.DataFrame({
        "time": times[:59], "open": [100.0] * 59, "high": [101.0] * 59, "low": [99.0] * 59,
        "close": [100.0] * 59, "tick_volume": [10] * 59, "spread": [1] * 59,
    })
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = Bar(time=times[59], open=100.0, high=101.0, low=99.0, close=100.0, tick_volume=10, spread=1)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == 60  # bar still appended, just no trade


def test_confirmed_crossover_and_swing_places_order_via_adapter():
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    assert request.symbol == "XAUUSD"
    assert request.direction == "BUY"
    assert request.entry == 180.0
    assert request.stop_loss < request.entry < request.take_profit
    assert request.lot_size > 0
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX + 1


def test_on_new_bar_raises_key_error_for_unseeded_symbol():
    import pytest

    seed = _flat_then_jump_df(100.0, 180.0).iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)  # only seeded for "XAUUSD"

    bar = Bar(time=BASE_TIME, open=1.0, high=1.0, low=1.0, close=1.0, tick_volume=1, spread=1)
    with pytest.raises(KeyError):
        loop.on_new_bar(MarketSnapshot(symbol="EURUSD", timeframe="H1", bar=bar))


def test_crossover_without_confirmed_swing_places_no_order_but_still_appends_bar():
    # No swing_dip_index at all -- the crossover fires, but there is no
    # fractal swing anywhere in the fixture, so build_trade_idea() has no
    # stop-loss anchor and returns None. Distinct code path from
    # "no EMA crossover at all" (test_no_signal_does_not_call_adapter_or_raise).
    df = _flat_then_jump_df(100.0, 180.0)  # no swing_dip_index
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX + 1  # bar still appended


def test_lot_size_below_broker_minimum_places_no_order():
    # Tiny equity forces compute_lot_size() to return None (below
    # volume_min) even though the signal + swing are both valid.
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter(equity=1.0)  # far too small to clear volume_min=0.01
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX + 1  # bar still appended


def test_rejected_order_result_does_not_raise_and_leaves_history_appended():
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)

    class RejectingAdapter(FakeAdapter):
        def place_order(self, request: TradeRequest):
            self.place_order_calls.append(request)
            return OrderResult(
                success=False, broker_ticket=None, filled_price=None,
                filled_volume=None, retcode=99, message="rejected in test",
            )

    adapter = RejectingAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))  # must not raise

    assert len(adapter.place_order_calls) == 1
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX + 1


def test_sell_signal_places_sell_order_with_stop_above_entry():
    df = _flat_then_jump_df(100.0, 20.0, swing_dip_index=30, swing_is_high=True)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    assert request.direction == "SELL"
    assert request.take_profit < request.entry < request.stop_loss
    assert request.lot_size > 0


def test_multiple_symbols_are_tracked_and_processed_independently():
    df_a = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    df_b = _flat_then_jump_df(50.0, 50.0)  # flat throughout -- never signals
    seed_a = df_a.iloc[:CROSS_INDEX].reset_index(drop=True)
    seed_b = df_b.iloc[:CROSS_INDEX].reset_index(drop=True)

    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, cfg=cfg,
        initial_history={"XAUUSD": seed_a, "EURUSD": seed_b},
        resolve_symbol_spec=_fake_symbol_spec, clock=FixedClock(BASE_TIME),
    )

    loop.on_new_bar(MarketSnapshot(symbol="EURUSD", timeframe="H1", bar=_bar_from_row(df_b, CROSS_INDEX)))
    assert adapter.place_order_calls == []
    assert len(loop._history["EURUSD"]) == CROSS_INDEX + 1
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX  # untouched by the EURUSD bar

    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=_bar_from_row(df_a, CROSS_INDEX)))
    assert len(adapter.place_order_calls) == 1
    assert adapter.place_order_calls[0].symbol == "XAUUSD"


def test_history_is_trimmed_to_max_history_bars_configured_limit():
    df = _flat_then_jump_df(100.0, 100.0, post_bars=0)  # flat, never signals
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)  # 70 bars seeded
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0, max_history_bars=71)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, cfg=cfg,
        initial_history={"XAUUSD": seed}, resolve_symbol_spec=_fake_symbol_spec,
        clock=FixedClock(BASE_TIME),
    )

    # Feed 3 more distinct closed bars -- history would grow to 73, must be
    # trimmed down to max_history_bars=71.
    for i in range(3):
        bar = Bar(
            time=BASE_TIME + timedelta(hours=CROSS_INDEX + i), open=100.0, high=101.0,
            low=99.0, close=100.0, tick_volume=10, spread=1,
        )
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=bar))

    assert len(loop._history["XAUUSD"]) == 71


def test_duplicate_bar_is_skipped_without_reevaluating():
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    # The "new" bar has the same timestamp as the last seeded bar -- this can
    # happen on startup when the seed already includes the latest closed bar.
    duplicate_bar = _bar_from_row(df, CROSS_INDEX - 1)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=duplicate_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX  # unchanged, not double-appended


def test_transient_get_equity_error_skips_this_bar_but_loop_keeps_running(caplog):
    # A transient MT5 hiccup (get_equity() raising, per demo_adapter.py's
    # RuntimeError on account_info() failure) must skip only this bar -- not
    # crash the loop or tear down monitoring entirely (spec.md §7).
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)

    class FlakyAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(equity=10_000.0)
            self.get_equity_calls = 0

        def get_equity(self) -> float:
            self.get_equity_calls += 1
            if self.get_equity_calls == 1:
                raise RuntimeError("account_info() failed: transient MT5 hiccup")
            return super().get_equity()

    adapter = FlakyAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    with caplog.at_level(logging.ERROR):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))  # must not raise

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX  # this bar's processing was skipped entirely
    assert any("unhandled exception" in record.message for record in caplog.records)

    # The loop keeps running -- retrying the same bar on the next poll now
    # succeeds since the transient error has passed.
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX + 1


def test_keyboard_interrupt_during_bar_processing_is_not_swallowed():
    # _process()'s `except Exception` must NOT be widened to `except
    # BaseException` (accidentally or otherwise) -- a real Ctrl-C (or any
    # other BaseException) raised mid-bar must still propagate out of
    # on_new_bar()/run(), not be logged-and-ignored like a transient MT5
    # hiccup. KeyboardInterrupt subclasses BaseException, not Exception, so
    # it is the natural probe for this.
    import pytest

    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)
    seed = df.iloc[:CROSS_INDEX].reset_index(drop=True)

    class InterruptingAdapter(FakeAdapter):
        def get_equity(self) -> float:
            raise KeyboardInterrupt()

    adapter = InterruptingAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed)

    new_bar = _bar_from_row(df, CROSS_INDEX)
    with pytest.raises(KeyboardInterrupt):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    # And history must be left untouched -- the bar was never actually
    # processed, not silently marked done.
    assert len(loop._history["XAUUSD"]) == CROSS_INDEX
