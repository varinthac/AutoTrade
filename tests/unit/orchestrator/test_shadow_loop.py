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
    def __init__(self, equity: float = 10_000.0):
        self._equity = equity
        self.place_order_calls: list[TradeRequest] = []

    def place_order(self, request: TradeRequest) -> OrderResult:
        self.place_order_calls.append(request)
        return OrderResult(
            success=True, broker_ticket=1, filled_price=request.entry,
            filled_volume=request.lot_size, retcode=None, message="ok",
        )

    def get_equity(self) -> float:
        return self._equity


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
