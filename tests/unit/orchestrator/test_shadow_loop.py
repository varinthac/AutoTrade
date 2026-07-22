"""Tests for orchestrator/shadow_loop.py -- Phase 6b wiring of
feed -> council(Bull/Bear scoring + Decision Matrix + Risk Voice) ->
shield(portfolio checkpoint) -> risk(CFO sizing) -> execution. MT5,
kill_switch_flag, and the broker are all faked/monkeypatched; this exercises
the orchestration/wiring logic only.

Bull/Bear Voice scoring is monkeypatched to return exact, hand-picked
scores -- same convention as tests/unit/council/test_decision_matrix.py's
own docstring explains -- so every fixture here deterministically triggers a
clean BUY/SELL/borderline/no-conviction Council decision without needing to
engineer real OHLC crossing score thresholds (already covered by
test_scoring.py). This keeps these wiring tests focused on ordering/plumbing,
not scoring internals.

The swing fixture (`_council_df`) mirrors test_decision_matrix.py's own
`_order_capable_df`: flat OHLC with one confirmed swing low and one
confirmed swing high, both confirmed well before `AS_OF`, so order
construction succeeds regardless of which direction a given fixture picks.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pandas as pd
import pytest

from autotrade.common import stop_request_flag
from autotrade.common.symbols import SymbolSpec
from autotrade.council.news_calendar import NewsEvent
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.council.scoring import BullBearScore
from autotrade.execution.adapter import (
    BrokerAdapter,
    BrokerPosition,
    ClosedTradeInfo,
    OrderResult,
    TradeRequest,
)
from autotrade.feed.snapshot import Bar, MarketSnapshot
from autotrade.orchestrator import shadow_loop as shadow_loop_module
from autotrade.orchestrator.shadow_loop import ShadowLoop, ShadowLoopConfig
from autotrade.risk.circuit_breaker import CircuitBreaker
from autotrade.shield.checkpoint import Shield
from autotrade.store import journal
from autotrade.watchman import position_metadata

N = 40
SWING_LOW_INDEX = 10
SWING_HIGH_INDEX = 30
AS_OF = N - 1  # index of the "new" bar once appended to an (N-1)-bar seed
BASE_TIME = datetime(2026, 1, 1)  # a Thursday -- never accidentally trips the Friday-close condition


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeAdapter(BrokerAdapter):
    def __init__(
        self, equity: float = 10_000.0, balance: float | None = None,
        open_positions: list[BrokerPosition] | None = None,
    ):
        self._equity = equity
        self._balance = equity if balance is None else balance
        self._open_positions = open_positions or []
        self.place_order_calls: list[TradeRequest] = []

    def place_order(self, request: TradeRequest, current_atr: float | None = None) -> OrderResult:
        self.place_order_calls.append(request)
        return OrderResult(
            success=True, broker_ticket=1, filled_price=request.entry,
            filled_volume=request.lot_size, retcode=None, message="ok",
        )

    def modify_stop_loss(self, ticket: int, new_stop_loss: float) -> OrderResult:
        raise NotImplementedError("not exercised by these wiring tests")

    def close_position(self, ticket: int, volume: float | None = None) -> OrderResult:
        raise NotImplementedError("not exercised by these wiring tests")

    def get_equity(self) -> float:
        return self._equity

    def get_balance(self) -> float:
        return self._balance

    def get_open_positions(self) -> list[BrokerPosition]:
        return self._open_positions

    def get_closed_trade_info(self, ticket: int) -> ClosedTradeInfo | None:
        raise NotImplementedError("not exercised by these wiring tests")


def _fake_symbol_spec(symbol: str) -> SymbolSpec:
    return SymbolSpec(
        canonical=symbol, broker_name=symbol, digits=2, point=0.01,
        tick_size=0.01, tick_value=1.0, contract_size=100.0,
        volume_min=0.01, volume_max=50.0, volume_step=0.01,
        trade_stops_level=0, freeze_level=0,
    )


def _council_df(n: int = N) -> pd.DataFrame:
    """Flat OHLC with a confirmed swing low at `SWING_LOW_INDEX` (low=90)
    and a confirmed swing high at `SWING_HIGH_INDEX` (high=110), both
    confirmed well before `AS_OF` -- so order construction succeeds
    regardless of which hypothetical/real direction a fixture's patched
    scores pick."""
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    lows[SWING_LOW_INDEX] = 90.0
    highs[SWING_HIGH_INDEX] = 110.0
    times = [BASE_TIME + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "time": times, "open": closes, "high": highs, "low": lows, "close": closes,
        "tick_volume": [10] * n, "spread": [10] * n,
    })


def _flat_df(level: float, n: int) -> pd.DataFrame:
    """Perfectly flat OHLC, no swing anywhere -- used for fixtures that
    never need order construction to succeed (e.g. history-trimming)."""
    times = [BASE_TIME + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "time": times, "open": [level] * n, "high": [level + 1] * n, "low": [level - 1] * n,
        "close": [level] * n, "tick_volume": [10] * n, "spread": [10] * n,
    })


def _bar_from_row(df: pd.DataFrame, index: int) -> Bar:
    row = df.iloc[index]
    return Bar(
        time=row["time"], open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        tick_volume=int(row["tick_volume"]), spread=int(row["spread"]),
    )


def _score(total: int) -> BullBearScore:
    return BullBearScore(
        score=total, trend_alignment=0, momentum_rsi=0, momentum_macd=0, market_structure=0, confluence=0
    )


def _patch_scores(monkeypatch, bull_total: int, bear_total: int) -> None:
    monkeypatch.setattr(
        "autotrade.council.decision_matrix.score_bull_voice", lambda *args, **kwargs: _score(bull_total),
    )
    monkeypatch.setattr(
        "autotrade.council.decision_matrix.score_bear_voice", lambda *args, **kwargs: _score(bear_total),
    )


class AllClearNewsProvider:
    """Always reports "fetched fine, nothing found" -- the opposite of
    `StubNewsCalendarProvider`, used so tests not specifically about the
    news condition don't get vetoed by it."""

    def get_high_impact_events(self, currency, window_start, window_end):
        return []


class FlakyNewsProvider:
    """Returns `[]` on its first call, then a high-impact event on every
    call after that -- simulates news appearing between Risk Voice's
    signal-time check and its order-send-time re-check."""

    def __init__(self):
        self.call_count = 0

    def get_high_impact_events(self, currency, window_start, window_end):
        self.call_count += 1
        if self.call_count == 1:
            return []
        return [NewsEvent(currency=currency, impact="high", event_time=window_start)]


def _permissive_risk_voice_cfg() -> RiskVoiceConfig:
    """Widened thresholds so only the condition a given test cares about can
    fire -- session covers the whole day, spread/ATR/stop-distance ceilings
    are generous multiples that this module's flat fixtures never approach."""
    return RiskVoiceConfig(
        max_spread_multiple=100.0, max_spread_points_xauusd=1000.0,
        news_blackout_before_min=45.0, news_blackout_after_min=30.0,
        max_stop_atr_multiple=100.0, session_start_hour=0, session_end_hour=24,
        friday_close_hour=20, max_atr_panic_multiple=100.0,
    )


def _default_shield() -> Shield:
    return Shield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )


class SpyShield(Shield):
    """Records every check()/record_trade_opened() call, on top of Shield's
    real behavior -- used to prove ordering (Risk Voice vetoes before Shield
    is even consulted)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.check_calls: list[dict] = []
        self.record_trade_opened_calls: list[dict] = []

    def check(self, order_plan, symbol, open_positions, new_trade_risk_pct, swing_index, clock):
        self.check_calls.append({"symbol": symbol, "direction": order_plan.direction})
        return super().check(order_plan, symbol, open_positions, new_trade_risk_pct, swing_index, clock)

    def record_trade_opened(self, symbol, direction, opened_at, swing_index) -> None:
        self.record_trade_opened_calls.append(
            {"symbol": symbol, "direction": direction, "opened_at": opened_at, "swing_index": swing_index}
        )
        super().record_trade_opened(symbol, direction, opened_at, swing_index)


def _default_loop(
    adapter, circuit_breaker, seed_history, clock=None, shield=None,
    news_provider=None, risk_voice_cfg=None, borderline_log_path=None,
    watchman_loop=None, position_metadata_path=None, journal_db_path=None,
) -> ShadowLoop:
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    # position_metadata_path/journal_db_path default alongside
    # borderline_log_path -- all test-scoped tmp_path files, never the
    # repo's real default state files, so a test that reaches a successful
    # place_order()/blocked-signal point never writes to
    # data/db/position_metadata.json or data/db/trade_journal.sqlite.
    if position_metadata_path is None and borderline_log_path is not None:
        position_metadata_path = borderline_log_path.parent / "position_metadata.json"
    if journal_db_path is None and borderline_log_path is not None:
        journal_db_path = borderline_log_path.parent / "trade_journal.sqlite"
    return ShadowLoop(
        adapter=adapter, circuit_breaker=circuit_breaker, shield=shield or _default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed_history}, resolve_symbol_spec=_fake_symbol_spec,
        clock=clock or FixedClock(BASE_TIME),
        news_provider=news_provider or AllClearNewsProvider(),
        risk_voice_cfg=risk_voice_cfg or _permissive_risk_voice_cfg(),
        journal_db_path=journal_db_path,
        borderline_log_path=borderline_log_path,
        watchman_loop=watchman_loop, position_metadata_path=position_metadata_path,
    )


def test_kill_switch_active_blocks_adapter_and_evaluates_no_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_loop_module.kill_switch_flag, "is_active", lambda: True)
    monkeypatch.setattr(
        shadow_loop_module.kill_switch_flag, "get_status",
        lambda: {"reason": "manual halt for test", "activated_at": "2026-01-01T00:00:00"},
    )

    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    # History must be untouched -- kill switch is checked before anything else.
    assert len(loop._history["XAUUSD"]) == AS_OF


def test_circuit_breaker_tripped_blocks_adapter(tmp_path):
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=9_000.0)

    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    breaker.record_equity(equity=9_000.0, peak_equity=10_000.0, live_start_equity=10_000.0, as_of=BASE_TIME)

    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF


def test_floating_pnl_from_equity_minus_balance_feeds_daily_loss_gate(tmp_path):
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=9_900.0, balance=10_000.0)  # -100 floating P&L
    breaker = CircuitBreaker(daily_loss_limit_pct=1.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []  # ~1.01% floating loss >= 1.0% limit


def test_no_conviction_does_not_call_adapter_or_raise(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=20, bear_total=20)  # neither near threshold nor conflicting
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF + 1  # bar still appended, just no trade


def test_clean_buy_decision_places_order_via_adapter(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    assert request.symbol == "XAUUSD"
    assert request.direction == "BUY"
    assert request.entry == 100.0
    assert request.stop_loss < request.entry < request.take_profit
    assert request.lot_size > 0
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_clean_buy_decision_notifies_once_with_key_order_facts(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")
    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1  # existing behavior unaffected by notify being mocked
    assert len(calls) == 1
    assert "XAUUSD" in calls[0]
    assert "BUY" in calls[0]
    assert "100.00000" in calls[0]  # entry


def test_clean_buy_decision_notify_message_matches_actual_placed_order_facts(monkeypatch, tmp_path):
    # Stronger content check than the sibling test above: asserts the
    # notified lot size/SL/TP/fill price match the SAME request object that
    # was actually sent to the adapter, catching a bug where the right hook
    # fires but with garbled/wrong data (e.g. sl/tp swapped or a stale lot).
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")
    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    assert len(calls) == 1
    message = calls[0]
    assert f"entry={request.entry:.5f}" in message
    assert f"sl={request.stop_loss:.5f}" in message
    assert f"tp={request.take_profit:.5f}" in message
    assert f"lot={request.lot_size:.2f}" in message
    # FakeAdapter.place_order() fills at request.entry -- confirms the
    # notified fill price is the real filled price, not e.g. the raw entry.
    assert f"filled={request.entry}" in message


def test_rejected_order_does_not_notify(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class RejectingAdapter(FakeAdapter):
        def place_order(self, request: TradeRequest, current_atr: float | None = None) -> OrderResult:
            self.place_order_calls.append(request)
            return OrderResult(
                success=False, broker_ticket=None, filled_price=None,
                filled_volume=None, retcode=None, message="rejected",
            )

    adapter = RejectingAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")
    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    assert calls == []


def test_clean_sell_decision_places_sell_order_with_stop_above_entry(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=30, bear_total=75)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    assert request.direction == "SELL"
    assert request.take_profit < request.entry < request.stop_loss
    assert request.lot_size > 0


def test_decision_without_confirmed_swing_places_no_order_but_still_appends_bar(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)  # clean BUY
    df = _flat_df(100.0, N)  # no swing anywhere -- order construction returns None
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_shield_blocked_trade_never_reaches_cfo_sizing_or_place_order(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    open_positions = [
        BrokerPosition(ticket=101, symbol="EURUSD", direction="SELL", risk_pct=0.1, current_sl=1.10, current_price=1.09, volume=0.1),
        BrokerPosition(ticket=102, symbol="GBPUSD", direction="SELL", risk_pct=0.1, current_sl=1.30, current_price=1.29, volume=0.1),
        BrokerPosition(ticket=103, symbol="USDJPY", direction="SELL", risk_pct=0.1, current_sl=150.0, current_price=149.0, volume=0.1),
    ]
    adapter = FakeAdapter(equity=10_000.0, open_positions=open_positions)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    loop = _default_loop(adapter, breaker, seed, shield=shield, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert shield.record_trade_opened_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_shield_blocked_trade_logs_warning_with_reason(monkeypatch, tmp_path, caplog):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    open_positions = [
        BrokerPosition(ticket=101, symbol="EURUSD", direction="SELL", risk_pct=0.1, current_sl=1.10, current_price=1.09, volume=0.1),
        BrokerPosition(ticket=102, symbol="GBPUSD", direction="SELL", risk_pct=0.1, current_sl=1.30, current_price=1.29, volume=0.1),
        BrokerPosition(ticket=103, symbol="USDJPY", direction="SELL", risk_pct=0.1, current_sl=150.0, current_price=149.0, volume=0.1),
    ]
    adapter = FakeAdapter(equity=10_000.0, open_positions=open_positions)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    with caplog.at_level(logging.WARNING):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert any("Shield blocked" in record.message for record in caplog.records)


def test_shield_approved_trade_proceeds_to_cfo_sizing_and_records_trade_opened(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    loop = _default_loop(adapter, breaker, seed, shield=shield, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1  # reached CFO sizing + placement
    assert len(shield.record_trade_opened_calls) == 1
    call = shield.record_trade_opened_calls[0]
    assert call["symbol"] == "XAUUSD"
    assert call["direction"] == "BUY"
    assert call["opened_at"] == BASE_TIME  # from the FixedClock used by _default_loop
    assert call["swing_index"] == SWING_LOW_INDEX


def test_shield_approved_but_lot_size_below_minimum_does_not_record_trade_opened(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=1.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    loop = _default_loop(adapter, breaker, seed, shield=shield, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert shield.record_trade_opened_calls == []


def test_shield_record_trade_opened_not_called_when_order_rejected(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class RejectingAdapter(FakeAdapter):
        def place_order(self, request: TradeRequest, current_atr: float | None = None):
            self.place_order_calls.append(request)
            return OrderResult(
                success=False, broker_ticket=None, filled_price=None,
                filled_volume=None, retcode=99, message="rejected in test",
            )

    adapter = RejectingAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    loop = _default_loop(adapter, breaker, seed, shield=shield, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    assert shield.record_trade_opened_calls == []


def test_on_new_bar_raises_key_error_for_unseeded_symbol(tmp_path):
    import pytest

    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    bar = Bar(time=BASE_TIME, open=1.0, high=1.0, low=1.0, close=1.0, tick_volume=1, spread=1)
    with pytest.raises(KeyError):
        loop.on_new_bar(MarketSnapshot(symbol="EURUSD", timeframe="H1", bar=bar))


def test_lot_size_below_broker_minimum_places_no_order(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=1.0)  # far too small to clear volume_min=0.01
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_rejected_order_result_does_not_raise_and_leaves_history_appended(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class RejectingAdapter(FakeAdapter):
        def place_order(self, request: TradeRequest, current_atr: float | None = None):
            self.place_order_calls.append(request)
            return OrderResult(
                success=False, broker_ticket=None, filled_price=None,
                filled_volume=None, retcode=99, message="rejected in test",
            )

    adapter = RejectingAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))  # must not raise

    assert len(adapter.place_order_calls) == 1
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_multiple_symbols_are_tracked_and_processed_independently(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)  # clean BUY whenever reached
    df_a = _council_df()  # XAUUSD: has a confirmed swing -- trades
    df_b = _flat_df(50.0, N)  # EURUSD: no swing anywhere -- decision fires but never becomes an order
    seed_a = df_a.iloc[:AS_OF].reset_index(drop=True)
    seed_b = df_b.iloc[:AS_OF].reset_index(drop=True)

    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, shield=_default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed_a, "EURUSD": seed_b},
        resolve_symbol_spec=_fake_symbol_spec, clock=FixedClock(BASE_TIME),
        news_provider=AllClearNewsProvider(), risk_voice_cfg=_permissive_risk_voice_cfg(),
        borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=tmp_path / "position_metadata.json",
        journal_db_path=tmp_path / "trade_journal.sqlite",
    )

    loop.on_new_bar(MarketSnapshot(symbol="EURUSD", timeframe="H1", bar=_bar_from_row(df_b, AS_OF)))
    assert adapter.place_order_calls == []
    assert len(loop._history["EURUSD"]) == AS_OF + 1
    assert len(loop._history["XAUUSD"]) == AS_OF  # untouched by the EURUSD bar

    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=_bar_from_row(df_a, AS_OF)))
    assert len(adapter.place_order_calls) == 1
    assert adapter.place_order_calls[0].symbol == "XAUUSD"


def test_history_is_trimmed_to_max_history_bars_configured_limit(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=20, bear_total=20)  # no conviction, never signals
    df = _flat_df(100.0, 73)
    seed = df.iloc[:70].reset_index(drop=True)  # 70 bars seeded
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0, max_history_bars=71)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, shield=_default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed}, resolve_symbol_spec=_fake_symbol_spec,
        clock=FixedClock(BASE_TIME), news_provider=AllClearNewsProvider(),
        risk_voice_cfg=_permissive_risk_voice_cfg(), borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=tmp_path / "position_metadata.json",
        journal_db_path=tmp_path / "trade_journal.sqlite",
    )

    # Feed 3 more distinct closed bars -- history would grow to 73, must be
    # trimmed down to max_history_bars=71.
    for i in range(3):
        bar = Bar(
            time=BASE_TIME + timedelta(hours=70 + i), open=100.0, high=101.0,
            low=99.0, close=100.0, tick_volume=10, spread=10,
        )
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=bar))

    assert len(loop._history["XAUUSD"]) == 71


def test_duplicate_bar_is_skipped_without_reevaluating(tmp_path):
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    # The "new" bar has the same timestamp as the last seeded bar -- this can
    # happen on startup when the seed already includes the latest closed bar.
    duplicate_bar = _bar_from_row(df, AS_OF - 1)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=duplicate_bar))

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF  # unchanged, not double-appended


def test_transient_get_equity_error_skips_this_bar_but_loop_keeps_running(monkeypatch, tmp_path, caplog):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

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
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    with caplog.at_level(logging.ERROR):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))  # must not raise

    assert adapter.place_order_calls == []
    assert len(loop._history["XAUUSD"]) == AS_OF  # this bar's processing was skipped entirely
    assert any("unhandled exception" in record.message for record in caplog.records)

    # The loop keeps running -- retrying the same bar on the next poll now
    # succeeds since the transient error has passed.
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_keyboard_interrupt_during_bar_processing_is_not_swallowed(tmp_path):
    import pytest

    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class InterruptingAdapter(FakeAdapter):
        def get_equity(self) -> float:
            raise KeyboardInterrupt()

    adapter = InterruptingAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    with pytest.raises(KeyboardInterrupt):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    # And history must be left untouched -- the bar was never actually
    # processed, not silently marked done.
    assert len(loop._history["XAUUSD"]) == AS_OF


# --- Phase 6b: borderline logging + Risk Voice wiring ---------------------


def test_borderline_case_is_logged_to_jsonl_and_no_trade_placed(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=60, bear_total=65)  # both >= 55 -> conflicting, borderline
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    log_path = tmp_path / "borderline.jsonl"
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=log_path)

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["symbol"] == "XAUUSD"
    assert payload["hypothetical_direction"] == "SELL"  # bear(65) > bull(60)
    assert payload["bull_score"] == 60
    assert payload["bear_score"] == 65
    assert payload["order_plan"]["direction"] == "SELL"


def test_borderline_case_jsonl_round_trips_every_field(monkeypatch, tmp_path):
    # Every field of BorderlineCase (decision_matrix.py) must survive the
    # JSONL round-trip, including the nested OrderPlan dataclass and the
    # datetime field -- Appendix A §1.3's note requires a *full* hypothetical
    # order for the future Auditor to replay, not just the scores.
    _patch_scores(monkeypatch, bull_total=60, bear_total=65)  # both >= 55 -> conflicting, borderline
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    log_path = tmp_path / "borderline.jsonl"
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=log_path)

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])

    assert set(payload.keys()) == {
        "symbol", "as_of_time", "hypothetical_direction", "bull_score",
        "bear_score", "risk_voice_score", "order_plan", "spread_at_evaluation",
    }
    assert payload["symbol"] == "XAUUSD"
    assert payload["hypothetical_direction"] == "SELL"  # bear(65) > bull(60)
    assert payload["bull_score"] == 60
    assert payload["bear_score"] == 65
    assert payload["risk_voice_score"] is None  # JSON null -- not yet wired per BorderlineCase's docstring
    assert payload["spread_at_evaluation"] == 10.0  # from _council_df's flat spread=10 column

    # as_of_time: dataclasses.asdict + json.dumps(default=str) has no native
    # datetime support -- confirm the stringified value is still exactly the
    # bar's own time and can be parsed back to that same value.
    assert payload["as_of_time"] == str(new_bar.time)
    assert datetime.fromisoformat(payload["as_of_time"]) == new_bar.time

    # Nested OrderPlan dataclass -- every field, not just direction.
    order_plan = payload["order_plan"]
    assert set(order_plan.keys()) == {"direction", "entry", "stop_loss", "take_profit", "stop_distance"}
    assert order_plan["direction"] == "SELL"
    assert order_plan["entry"] == 100.0
    assert order_plan["take_profit"] < order_plan["entry"] < order_plan["stop_loss"]
    assert order_plan["stop_distance"] > 0
    assert order_plan["take_profit"] == order_plan["entry"] - 2.0 * order_plan["stop_distance"]  # SELL, 2R TP


def test_borderline_log_write_failure_does_not_crash_loop_and_skips_this_bar(monkeypatch, tmp_path, caplog):
    # A write failure while persisting a borderline case (disk full,
    # permission error, ...) must degrade gracefully -- caught by
    # `_process`'s broad except (same "log loudly, skip this bar, keep
    # running" contract as the transient-get_equity-error test above) rather
    # than crashing the whole loop.
    _patch_scores(monkeypatch, bull_total=60, bear_total=65)  # borderline -> triggers the write
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    real_log_borderline_case = shadow_loop_module._log_borderline_case

    def _raise(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(shadow_loop_module, "_log_borderline_case", _raise)

    new_bar = _bar_from_row(df, AS_OF)
    with caplog.at_level(logging.ERROR):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))  # must not raise

    assert adapter.place_order_calls == []
    # This bar's processing was skipped entirely (not partially committed) --
    # same "whole-bar-atomic" contract as the transient-error test.
    assert len(loop._history["XAUUSD"]) == AS_OF
    assert any("unhandled exception" in record.message for record in caplog.records)

    # The loop keeps running: retrying the same bar (write failure resolved)
    # on the next poll now succeeds and is logged as usual, not silently lost.
    monkeypatch.setattr(shadow_loop_module, "_log_borderline_case", real_log_borderline_case)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))
    assert len(loop._history["XAUUSD"]) == AS_OF + 1


def test_risk_voice_veto_blocks_before_shield_is_consulted(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)  # clean BUY
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    # Session restricted to hours that never include BASE_TIME's hour (0) --
    # the only condition this fixture trips is the session condition.
    risk_voice_cfg = RiskVoiceConfig(
        max_spread_multiple=100.0, max_spread_points_xauusd=1000.0,
        news_blackout_before_min=45.0, news_blackout_after_min=30.0,
        max_stop_atr_multiple=100.0, session_start_hour=14, session_end_hour=18,
        friday_close_hour=20, max_atr_panic_multiple=100.0,
    )
    loop = _default_loop(
        adapter, breaker, seed, shield=shield, risk_voice_cfg=risk_voice_cfg,
        borderline_log_path=tmp_path / "borderline.jsonl",
    )

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert shield.check_calls == []  # Risk Voice vetoed before Shield was ever consulted


def test_risk_voice_veto_logs_warning_with_reason(monkeypatch, tmp_path, caplog):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    risk_voice_cfg = RiskVoiceConfig(
        max_spread_multiple=100.0, max_spread_points_xauusd=1000.0,
        news_blackout_before_min=45.0, news_blackout_after_min=30.0,
        max_stop_atr_multiple=100.0, session_start_hour=14, session_end_hour=18,
        friday_close_hour=20, max_atr_panic_multiple=100.0,
    )
    loop = _default_loop(
        adapter, breaker, seed, risk_voice_cfg=risk_voice_cfg, borderline_log_path=tmp_path / "borderline.jsonl",
    )

    new_bar = _bar_from_row(df, AS_OF)
    with caplog.at_level(logging.WARNING):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert any("Risk Voice vetoed" in record.message for record in caplog.records)


def test_default_news_provider_is_stub_and_vetoes_every_trade(monkeypatch, tmp_path):
    # No news_provider/risk_voice_cfg override -- ShadowLoop's own defaults
    # (StubNewsCalendarProvider, RiskVoiceConfig()) apply. Clock is inside
    # the default session window and not a Friday, so news is the only
    # condition left that can veto -- and the stub always does.
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, shield=_default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed}, resolve_symbol_spec=_fake_symbol_spec,
        clock=FixedClock(datetime(2026, 1, 1, 15, 0)),  # Thursday, inside default [14,18) session
        borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=tmp_path / "position_metadata.json",
        journal_db_path=tmp_path / "trade_journal.sqlite",
    )

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []


def test_stale_signal_recheck_vetoes_and_skips_placing_order(monkeypatch, tmp_path, caplog):
    # News is clear at the first (signal-time) Risk Voice check but has
    # appeared by the second (order-send-time) re-check -- must cancel the
    # trade and log stale_signal, not place it, and must not tell Shield a
    # trade was opened.
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = SpyShield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    loop = _default_loop(
        adapter, breaker, seed, shield=shield, news_provider=FlakyNewsProvider(),
        borderline_log_path=tmp_path / "borderline.jsonl",
    )

    new_bar = _bar_from_row(df, AS_OF)
    with caplog.at_level(logging.WARNING):
        loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert adapter.place_order_calls == []
    assert shield.record_trade_opened_calls == []
    assert any("stale_signal" in record.message for record in caplog.records)


# --- Phase 7b: Watchman wiring ----------------------------------------------


def test_successful_trade_records_position_metadata(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    state_path = tmp_path / "position_metadata.json"
    loop = _default_loop(
        adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=state_path,
    )

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    request = adapter.place_order_calls[0]
    recorded = position_metadata.get_position_metadata(1, state_path)  # FakeAdapter always returns ticket=1
    assert recorded is not None
    assert recorded.symbol == "XAUUSD"
    assert recorded.direction == "BUY"
    assert recorded.entry_price == request.entry  # ACTUAL filled price (FakeAdapter echoes request.entry)
    assert recorded.initial_stop_distance > 0
    assert recorded.entry_swing_index == SWING_LOW_INDEX
    assert recorded.opened_at == BASE_TIME
    assert recorded.entry_spread_points is not None  # Appendix A §5.1 daily-report field
    assert recorded.actual_slippage == pytest.approx(0.0)  # FakeAdapter fills exactly at request.entry


def test_place_order_receives_current_atr_kwarg(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class AtrCapturingAdapter(FakeAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.current_atr_calls: list[float | None] = []

        def place_order(self, request, current_atr=None):
            self.current_atr_calls.append(current_atr)
            return super().place_order(request, current_atr)

    adapter = AtrCapturingAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.current_atr_calls) == 1
    assert adapter.current_atr_calls[0] is not None  # a real ATR value, not the None default


def test_ticket_none_does_not_record_position_metadata(monkeypatch, tmp_path):
    # NoOpBrokerAdapter-style success (dry run: broker_ticket=None) -- there
    # is no real position to manage, so Watchman metadata must not be
    # recorded for a ticket that doesn't exist.
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)

    class DryRunAdapter(FakeAdapter):
        def place_order(self, request, current_atr=None):
            self.place_order_calls.append(request)
            return OrderResult(
                success=True, broker_ticket=None, filled_price=request.entry,
                filled_volume=request.lot_size, retcode=None, message="dry run",
            )

    adapter = DryRunAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    state_path = tmp_path / "position_metadata.json"
    loop = _default_loop(
        adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=state_path,
    )

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    assert len(adapter.place_order_calls) == 1
    assert not state_path.exists()  # nothing was ever written


def test_run_wires_watchman_cycle_as_on_iteration_end_hook_when_given(monkeypatch, tmp_path):
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)

    class SpyWatchmanLoop:
        def __init__(self):
            self.run_cycle_calls = []

        def run_cycle(self, history_by_symbol, now):
            self.run_cycle_calls.append((history_by_symbol, now))

    watchman_loop = SpyWatchmanLoop()
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, shield=_default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed}, resolve_symbol_spec=_fake_symbol_spec,
        clock=FixedClock(BASE_TIME), news_provider=AllClearNewsProvider(),
        risk_voice_cfg=_permissive_risk_voice_cfg(), borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=tmp_path / "position_metadata.json", watchman_loop=watchman_loop,
        journal_db_path=tmp_path / "trade_journal.sqlite",
    )

    captured_kwargs = {}

    def fake_poll_new_bars(symbols, timeframe, on_new_bar, poll_interval_sec, max_iterations, on_iteration_end):
        captured_kwargs["on_iteration_end"] = on_iteration_end
        if on_iteration_end is not None:
            on_iteration_end()

    monkeypatch.setattr(shadow_loop_module, "poll_new_bars", fake_poll_new_bars)
    loop.run(["XAUUSD"], "H1", poll_interval_sec=0.0, max_iterations=1)

    assert captured_kwargs["on_iteration_end"] is not None
    assert len(watchman_loop.run_cycle_calls) == 1
    history_arg, now_arg = watchman_loop.run_cycle_calls[0]
    assert set(history_arg.keys()) == {"XAUUSD"}
    assert now_arg == BASE_TIME


def test_run_passes_non_none_on_iteration_end_even_without_watchman_loop(monkeypatch, tmp_path):
    # Unlike the pre-stop-flag-feature behavior, on_iteration_end is now
    # always given -- it must run the stop-request check every cycle
    # regardless of whether a watchman_loop was injected (see run()'s
    # docstring). Calling it here (no watchman_loop, no stop flag set) must
    # not raise and must not try to touch a watchman_loop that doesn't exist.
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    captured_kwargs = {}

    def fake_poll_new_bars(symbols, timeframe, on_new_bar, poll_interval_sec, max_iterations, on_iteration_end):
        captured_kwargs["on_iteration_end"] = on_iteration_end
        on_iteration_end()

    monkeypatch.setattr(shadow_loop_module, "poll_new_bars", fake_poll_new_bars)
    loop.run(["XAUUSD"], "H1", poll_interval_sec=0.0, max_iterations=1)

    assert captured_kwargs["on_iteration_end"] is not None


def test_on_iteration_end_calls_injected_autotrading_watchdog_with_reader_result(tmp_path):
    # Constructs ShadowLoop through its REAL __init__ (matching
    # test_run_wires_watchman_cycle_as_on_iteration_end_hook_when_given's
    # pattern) rather than poking private attributes directly -- that would
    # only prove _on_iteration_end() uses self._autotrading_watchdog/
    # self._read_autotrading_state IF they happen to be set, not that the
    # constructor actually threads the autotrading_watchdog=/
    # read_autotrading_state= kwargs into those attributes in the first
    # place (a swapped-kwarg or dropped-assignment regression in __init__
    # would still pass the old version of this test).
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)

    class SpyAutoTradingWatchdog:
        def __init__(self):
            self.check_calls: list[bool | None] = []

        def check(self, trade_allowed):
            self.check_calls.append(trade_allowed)

    autotrading_watchdog = SpyAutoTradingWatchdog()
    cfg = ShadowLoopConfig(risk_per_trade_pct=1.0)
    loop = ShadowLoop(
        adapter=adapter, circuit_breaker=breaker, shield=_default_shield(), cfg=cfg,
        initial_history={"XAUUSD": seed}, resolve_symbol_spec=_fake_symbol_spec,
        clock=FixedClock(BASE_TIME), news_provider=AllClearNewsProvider(),
        risk_voice_cfg=_permissive_risk_voice_cfg(), borderline_log_path=tmp_path / "borderline.jsonl",
        position_metadata_path=tmp_path / "position_metadata.json",
        journal_db_path=tmp_path / "trade_journal.sqlite",
        autotrading_watchdog=autotrading_watchdog, read_autotrading_state=lambda: False,
    )

    loop._on_iteration_end()

    assert autotrading_watchdog.check_calls == [False]


def test_on_iteration_end_does_not_raise_when_autotrading_watchdog_not_given(tmp_path):
    # Default backward-compat path: autotrading_watchdog=None must never be
    # touched/called, and _on_iteration_end() must still complete normally.
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    assert loop._autotrading_watchdog is None
    loop._on_iteration_end()  # must not raise


# --- Phase 8a: blocked-signal recording (store/journal.py) ------------------


def test_no_conviction_records_blocked_signal(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=20, bear_total=20)  # neither near threshold nor conflicting
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    borderline_log_path = tmp_path / "borderline.jsonl"
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=borderline_log_path)

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    counts = journal.count_blocked_signals_for_day(
        new_bar.time.date(), db_path=borderline_log_path.parent / "trade_journal.sqlite",
    )
    assert counts == {"borderline_no_conviction": 1}


def test_risk_voice_veto_records_blocked_signal(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter(equity=10_000.0)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    risk_voice_cfg = RiskVoiceConfig(
        max_spread_multiple=100.0, max_spread_points_xauusd=1000.0,
        news_blackout_before_min=45.0, news_blackout_after_min=30.0,
        max_stop_atr_multiple=100.0, session_start_hour=14, session_end_hour=18,
        friday_close_hour=20, max_atr_panic_multiple=100.0,
    )
    borderline_log_path = tmp_path / "borderline.jsonl"
    loop = _default_loop(
        adapter, breaker, seed, risk_voice_cfg=risk_voice_cfg, borderline_log_path=borderline_log_path,
    )

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    counts = journal.count_blocked_signals_for_day(
        new_bar.time.date(), db_path=borderline_log_path.parent / "trade_journal.sqlite",
    )
    assert counts == {"risk_voice": 1}


def test_shield_blocked_trade_records_blocked_signal(monkeypatch, tmp_path):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _council_df()
    seed = df.iloc[:AS_OF].reset_index(drop=True)
    open_positions = [
        BrokerPosition(ticket=101, symbol="EURUSD", direction="SELL", risk_pct=0.1, current_sl=1.10, current_price=1.09, volume=0.1),
        BrokerPosition(ticket=102, symbol="GBPUSD", direction="SELL", risk_pct=0.1, current_sl=1.30, current_price=1.29, volume=0.1),
        BrokerPosition(ticket=103, symbol="USDJPY", direction="SELL", risk_pct=0.1, current_sl=150.0, current_price=149.0, volume=0.1),
    ]
    adapter = FakeAdapter(equity=10_000.0, open_positions=open_positions)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    shield = Shield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=1,
        max_positions_total=3, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    borderline_log_path = tmp_path / "borderline.jsonl"
    loop = _default_loop(adapter, breaker, seed, shield=shield, borderline_log_path=borderline_log_path)

    new_bar = _bar_from_row(df, AS_OF)
    loop.on_new_bar(MarketSnapshot(symbol="XAUUSD", timeframe="H1", bar=new_bar))

    counts = journal.count_blocked_signals_for_day(
        new_bar.time.date(), db_path=borderline_log_path.parent / "trade_journal.sqlite",
    )
    assert counts == {"shield": 1}


# --- Start/stop workflow: graceful stop-request flag wiring -----------------


@pytest.fixture
def stop_flag_path(tmp_path, monkeypatch):
    path = tmp_path / "stop_request.flag"
    monkeypatch.setattr(stop_request_flag, "DEFAULT_FLAG_PATH", path)
    return path


def _run_one_iteration(loop, monkeypatch, symbols=("XAUUSD",), before_iteration=None):
    """Drives ShadowLoop.run() through a single fake poll_new_bars iteration
    that only invokes on_iteration_end (no new bar) -- same fake-poller
    pattern test_run_wires_watchman_cycle_as_on_iteration_end_hook_when_given
    already uses, reused here for the stop-flag checks. `before_iteration`,
    if given, runs just before on_iteration_end -- simulating a stop request
    arriving mid-run (AFTER run()'s own startup stale-flag clear), not one
    already present before run() was even called."""
    def fake_poll_new_bars(symbols_, timeframe, on_new_bar, poll_interval_sec, max_iterations, on_iteration_end):
        if before_iteration is not None:
            before_iteration()
        on_iteration_end()

    monkeypatch.setattr(shadow_loop_module, "poll_new_bars", fake_poll_new_bars)
    loop.run(list(symbols), "H1", poll_interval_sec=0.0, max_iterations=1)


def test_stop_request_with_no_open_positions_exits_loop_clears_flag_and_notifies(
    monkeypatch, tmp_path, stop_flag_path,
):
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()  # no open_positions given -> []
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    # Request the stop mid-run (after run()'s own startup stale-flag clear),
    # not before run() is even called -- see _run_one_iteration's docstring.
    _run_one_iteration(
        loop, monkeypatch, before_iteration=lambda: stop_request_flag.request("manual stop button"),
    )

    assert stop_request_flag.is_requested() is False  # cleared immediately
    assert len(calls) == 1
    assert "no open positions" in calls[0]


def test_stop_request_with_open_positions_exits_loop_clears_flag_and_warns_in_notify(
    monkeypatch, tmp_path, stop_flag_path,
):
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    open_positions = [
        BrokerPosition(ticket=101, symbol="XAUUSD", direction="BUY", risk_pct=0.5, current_sl=99.0, current_price=100.0, volume=0.1),
        BrokerPosition(ticket=102, symbol="EURUSD", direction="SELL", risk_pct=0.5, current_sl=1.10, current_price=1.09, volume=0.1),
    ]
    adapter = FakeAdapter(open_positions=open_positions)
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    _run_one_iteration(
        loop, monkeypatch, before_iteration=lambda: stop_request_flag.request("manual stop button"),
    )

    assert stop_request_flag.is_requested() is False
    assert len(calls) == 1
    assert "2" in calls[0]
    assert "NOT be managed" in calls[0]
    assert "emergency stop" in calls[0]


def test_stop_request_when_get_open_positions_raises_still_clears_flag_stops_loop_and_notifies(
    monkeypatch, tmp_path, stop_flag_path,
):
    """Guards the exact hazard _check_stop_request's own comment calls out:
    a get_open_positions() failure must never (a) leave the flag set (so a
    retry is needed) or (b) let the loop silently resume polling instead of
    stopping -- the stop must still complete, just with an "unknown" count."""
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    def _raise_get_open_positions():
        raise RuntimeError("MT5 positions_get() hiccup")

    monkeypatch.setattr(adapter, "get_open_positions", _raise_get_open_positions)

    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    _run_one_iteration(
        loop, monkeypatch, before_iteration=lambda: stop_request_flag.request("manual stop button"),
    )

    assert stop_request_flag.is_requested() is False  # cleared despite the failure, not left pending
    assert len(calls) == 1
    assert "could not be determined" in calls[0]


def test_no_stop_request_does_not_notify_or_raise(monkeypatch, tmp_path, stop_flag_path):
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    _run_one_iteration(loop, monkeypatch)  # no stop flag set -- must complete normally

    assert calls == []


def test_stale_stop_request_flag_from_previous_session_does_not_instantly_stop_a_fresh_run(
    monkeypatch, tmp_path, stop_flag_path,
):
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    stop_request_flag.request("stale flag from a previous, abnormally-ended session")
    assert stop_request_flag.is_requested() is True

    iteration_calls = {"count": 0}

    def fake_poll_new_bars(symbols_, timeframe, on_new_bar, poll_interval_sec, max_iterations, on_iteration_end):
        # A fresh run() must clear the stale flag BEFORE poll_new_bars is
        # ever invoked -- so on_iteration_end() here must complete without
        # finding a (re-set) stop request and without raising.
        iteration_calls["count"] += 1
        on_iteration_end()

    monkeypatch.setattr(shadow_loop_module, "poll_new_bars", fake_poll_new_bars)
    calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: calls.append(text))

    loop.run(["XAUUSD"], "H1", poll_interval_sec=0.0, max_iterations=1)

    assert iteration_calls["count"] == 1  # the fake loop actually ran, wasn't skipped
    assert stop_request_flag.is_requested() is False  # cleared at startup
    assert calls == []  # the stale flag must never trigger a stop notification


def test_stop_requested_mid_cycle_still_processes_every_remaining_symbol_before_stopping(
    monkeypatch, tmp_path, stop_flag_path,
):
    """Proves the granularity is once-per-FULL-CYCLE, not once-per-symbol:
    a stop request that appears while symbol A is being processed must NOT
    cut the cycle short -- symbol B (later in the same `symbols` list) must
    still be dispatched to on_new_bar before the stop-request check (which
    only runs from on_iteration_end, i.e. after poll_new_bars' own
    `for symbol in symbols` loop has already finished -- see
    feed/poller.py's poll_new_bars) is ever consulted. Uses a spy in place
    of the real on_new_bar (council wiring is irrelevant here -- this is
    purely about run()'s cycle-vs-symbol granularity), and a fake poller
    that mirrors poll_new_bars' real call order: on_new_bar for every symbol
    first, on_iteration_end only once at the very end."""
    seed = _council_df().iloc[:AS_OF].reset_index(drop=True)
    adapter = FakeAdapter()
    breaker = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    loop = _default_loop(adapter, breaker, seed, borderline_log_path=tmp_path / "borderline.jsonl")

    processed_symbols: list[str] = []

    def spy_on_new_bar(snapshot: MarketSnapshot) -> None:
        processed_symbols.append(snapshot.symbol)
        if snapshot.symbol == "XAUUSD":
            # Simulate the operator hitting "stop" WHILE the first symbol of
            # this cycle is still being processed.
            stop_request_flag.request("manual stop button")

    monkeypatch.setattr(loop, "on_new_bar", spy_on_new_bar)

    bar = _bar_from_row(_council_df(), AS_OF)
    symbols = ["XAUUSD", "EURUSD"]

    def fake_poll_new_bars(symbols_, timeframe, on_new_bar, poll_interval_sec, max_iterations, on_iteration_end):
        assert symbols_ == symbols
        for symbol in symbols_:
            on_new_bar(MarketSnapshot(symbol=symbol, timeframe=timeframe, bar=bar))
        on_iteration_end()

    monkeypatch.setattr(shadow_loop_module, "poll_new_bars", fake_poll_new_bars)
    notify_calls = []
    monkeypatch.setattr(shadow_loop_module, "notify", lambda text: notify_calls.append(text))

    loop.run(symbols, "H1", poll_interval_sec=0.0, max_iterations=1)

    # Both symbols were dispatched in this cycle -- the stop request set
    # mid-way through symbol A did NOT skip symbol B's processing.
    assert processed_symbols == ["XAUUSD", "EURUSD"]
    # Only after the whole cycle finished was the stop actually honored.
    assert stop_request_flag.is_requested() is False
    assert len(notify_calls) == 1
