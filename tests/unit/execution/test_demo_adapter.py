"""Unit tests for execution/demo_adapter.py — ThrottledDemoAdapter. MT5 itself
is mocked (same pattern as test_mt5_connection.py/test_poller.py); no live
terminal needed."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from autotrade.common.config import MT5Credentials
from autotrade.council.order_construction import OrderPlan
from autotrade.execution.adapter import TradeRequest
from autotrade.execution.demo_adapter import ThrottledDemoAdapter, mt5
from autotrade.shield.checkpoint import OpenPositionInfo, Shield

CREDS = MT5Credentials(login=123, password="pw", server="ICMarketsSC-Demo", terminal_path=None)
SYMBOL_MAP = {"XAUUSD": "XAUUSD"}


class _FakeAccount:
    login = 123
    server = "ICMarketsSC-Demo"
    balance = 1000.0
    currency = "USD"
    equity = 1234.5


class _FakeSymbolInfo:
    digits = 2
    point = 0.01
    trade_tick_size = 0.01
    trade_tick_value = 1.0
    trade_contract_size = 100.0
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01
    trade_stops_level = 30
    trade_freeze_level = 10


class _FakePosition:
    def __init__(self, symbol, sl, price_current, volume, type_, ticket=1):
        self.symbol = symbol
        self.sl = sl
        self.price_current = price_current
        self.volume = volume
        self.type = type_
        self.ticket = ticket


class _FakeTick:
    def __init__(self, bid: float, ask: float, time: int = 1_700_000_000):
        self.bid = bid
        self.ask = ask
        self.time = time


class _FakeSendResult:
    def __init__(self, retcode, order=None, price=None, volume=None, comment=""):
        self.retcode = retcode
        self.order = order
        self.price = price
        self.volume = volume
        self.comment = comment


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _patch_mt5_boilerplate(monkeypatch):
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "symbol_select", lambda name, enable: True)
    monkeypatch.setattr(mt5, "symbol_info", lambda name: _FakeSymbolInfo())


def _request(entry=2400.0, lot_size=0.1):
    return TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=lot_size,
        entry=entry, stop_loss=2395.0, take_profit=2410.0,
    )


def test_successful_order_send_returns_correct_order_result(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))

    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=555, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request())

    assert result.success is True
    assert result.broker_ticket == 555
    assert result.filled_price == 2400.1
    assert result.filled_volume == 0.1
    assert result.retcode == mt5.TRADE_RETCODE_DONE
    assert "filled" in result.message

    assert captured["action"] == mt5.TRADE_ACTION_DEAL
    assert captured["type"] == mt5.ORDER_TYPE_BUY
    assert captured["symbol"] == "XAUUSD"
    assert captured["volume"] == 0.1
    assert captured["sl"] == 2395.0
    assert captured["tp"] == 2410.0
    assert captured["price"] == 2400.1  # ask, since BUY


def test_throttle_refuses_second_call_within_cooldown_without_touching_mt5(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.1),
    )

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    adapter = ThrottledDemoAdapter(
        CREDS, clock, min_seconds_between_trades=300.0, symbol_map=SYMBOL_MAP,
    )

    first = adapter.place_order(_request())
    assert first.success is True

    def boom(request):
        pytest.fail("order_send must not be called while throttled")

    monkeypatch.setattr(mt5, "order_send", boom)
    clock.advance(60.0)
    second = adapter.place_order(_request())

    assert second.success is False
    assert second.broker_ticket is None
    assert second.retcode is None
    assert "throttl" in second.message.lower()


def test_throttle_allows_call_again_after_cooldown_elapses(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []

    def fake_order_send(request):
        send_calls.append(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=len(send_calls), price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    adapter = ThrottledDemoAdapter(
        CREDS, clock, min_seconds_between_trades=300.0, symbol_map=SYMBOL_MAP,
    )

    first = adapter.place_order(_request())
    clock.advance(300.0)
    second = adapter.place_order(_request())

    assert first.success is True
    assert second.success is True
    assert len(send_calls) == 2


def test_rejected_retcode_returns_failure_without_raising(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE, comment="Requote"),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request())

    assert result.success is False
    assert result.broker_ticket is None
    assert result.filled_price is None
    assert result.retcode == mt5.TRADE_RETCODE_REQUOTE
    assert "requote" in result.message.lower()


def test_fill_mismatch_beyond_tolerance_logs_warning_but_still_succeeds(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    # Filled far from the requested entry: 2400.0 requested vs 2401.0 filled,
    # 100 points on a 0.01-point symbol, well beyond the default 5-point tolerance.
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2401.0, volume=0.1),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    with caplog.at_level(logging.WARNING):
        result = adapter.place_order(_request(entry=2400.0))

    assert result.success is True
    assert any("differs from requested entry" in record.message for record in caplog.records)


def test_get_equity_reads_account_info_equity(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    assert adapter.get_equity() == 1234.5


def test_get_equity_raises_when_account_info_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (5, "no connection"))

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    with pytest.raises(RuntimeError, match="account_info"):
        adapter.get_equity()


def test_get_balance_reads_account_info_balance(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    assert adapter.get_balance() == 1000.0


def test_get_balance_raises_when_account_info_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (5, "no connection"))

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    with pytest.raises(RuntimeError, match="account_info"):
        adapter.get_balance()


def test_missing_tick_returns_failure_without_sending_order(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (7, "symbol not found"))

    def boom(request):
        pytest.fail("order_send must not be called when the tick is unavailable")

    monkeypatch.setattr(mt5, "order_send", boom)

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request())

    assert result.success is False
    assert result.broker_ticket is None
    assert "symbol_info_tick" in result.message


def test_zero_time_tick_is_treated_same_as_missing_tick(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1, time=0))
    monkeypatch.setattr(mt5, "last_error", lambda: (7, "stale/placeholder tick"))

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request())

    assert result.success is False
    assert "symbol_info_tick" in result.message


def test_order_send_returning_none_is_reported_as_failure(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (9, "no connection to trade server"))

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request())

    assert result.success is False
    assert result.broker_ticket is None
    assert "returned None" in result.message


def test_sell_direction_uses_bid_price_not_ask(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))

    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2399.9, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    sell_request = TradeRequest(
        symbol="XAUUSD", direction="SELL", lot_size=0.1,
        entry=2400.0, stop_loss=2405.0, take_profit=2390.0,
    )
    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(sell_request)

    assert result.success is True
    assert captured["type"] == mt5.ORDER_TYPE_SELL
    assert captured["price"] == 2399.9  # bid, since SELL


def test_invalid_direction_raises_value_error(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    bad_request = TradeRequest(
        symbol="XAUUSD", direction="HOLD", lot_size=0.1,  # type: ignore[arg-type]
        entry=2400.0, stop_loss=2395.0, take_profit=2410.0,
    )
    with pytest.raises(ValueError):
        adapter.place_order(bad_request)


def test_partial_fill_retcode_reports_success_with_partial_message(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE_PARTIAL, order=42, price=2400.1, volume=0.05),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    result = adapter.place_order(_request(lot_size=0.1))

    assert result.success is True
    assert result.filled_volume == 0.05
    assert result.retcode == mt5.TRADE_RETCODE_DONE_PARTIAL
    assert "partially filled" in result.message.lower()


def test_fill_volume_mismatch_beyond_tolerance_logs_warning_but_still_succeeds(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    # Filled volume 0.05 vs requested 0.1 -- must warn about the mismatch.
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.05),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)

    with caplog.at_level(logging.WARNING):
        result = adapter.place_order(_request(lot_size=0.1))

    assert result.success is True
    assert any("differs from requested lot_size" in record.message for record in caplog.records)


def test_get_open_positions_returns_empty_list_when_no_open_positions(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "positions_get", lambda: ())

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    assert adapter.get_open_positions() == []


def test_get_open_positions_maps_broker_symbol_and_computes_risk_pct(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    # equity=1234.5 (from _FakeAccount). distance=|2400-2395|=5, point_value=1.0/0.01=100,
    # volume=0.1 -> risk_amount=5*100*0.1=50 -> risk_pct=50/1234.5*100.
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    positions = adapter.get_open_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "XAUUSD"
    assert positions[0].direction == "BUY"
    assert positions[0].risk_pct == pytest.approx(50.0 / 1234.5 * 100)


def test_get_open_positions_maps_sell_type_to_sell_direction(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="XAUUSD", sl=2405.0, price_current=2400.0, volume=0.1, type_=mt5.POSITION_TYPE_SELL),),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    positions = adapter.get_open_positions()

    assert positions[0].direction == "SELL"


def test_get_open_positions_skips_position_with_unmapped_broker_symbol(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="UNKNOWNSYMBOL", sl=1.0, price_current=1.1, volume=0.1, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    with caplog.at_level(logging.WARNING):
        positions = adapter.get_open_positions()

    assert positions == []
    assert any("no canonical mapping" in record.message for record in caplog.records)


def test_get_open_positions_treats_missing_stop_loss_as_unbounded_risk(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="XAUUSD", sl=0.0, price_current=2400.0, volume=0.1, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    with caplog.at_level(logging.WARNING):
        positions = adapter.get_open_positions()

    assert len(positions) == 1
    assert positions[0].risk_pct == float("inf")
    assert any("no stop-loss set" in record.message for record in caplog.records)


def test_get_open_positions_sl_zero_treated_as_unbounded_risk_blocks_new_entries(monkeypatch):
    # Fix for code-reviewer finding #1: a position with no stop-loss
    # (sl == 0) has effectively UNBOUNDED risk, not zero -- see
    # test_get_open_positions_treats_missing_stop_loss_as_unbounded_risk
    # above for that behavior in isolation. This test shows the fix closes
    # the real gap: it feeds the same BrokerPosition orchestrator/
    # shadow_loop.py would build straight into Shield's rule 5 (total risk
    # ceiling) and confirms a large, entirely-unprotected position (5.0
    # lots, no SL) now forces the ceiling check to block any new trade,
    # instead of silently contributing zero.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="XAUUSD", sl=0.0, price_current=2400.0, volume=5.0, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    positions = adapter.get_open_positions()

    assert len(positions) == 1
    assert positions[0].risk_pct == float("inf")  # the fix: 5.0 lots, no stop, "unbounded" risk

    open_positions = [
        OpenPositionInfo(symbol=positions[0].symbol, direction=positions[0].direction, risk_pct=positions[0].risk_pct)
    ]
    shield = Shield(
        min_rr=1.5, max_correlation=0.7, max_positions_per_symbol=5,
        max_positions_total=5, total_risk_ceiling_pct=3.0, duplicate_signal_cooldown_hours=4.0,
    )
    # A brand-new trade at 2.9% risk on its own -- the existing (real,
    # unstopped) exposure is now counted as unbounded, so this must block
    # regardless of how small the new trade's own risk is.
    new_plan = OrderPlan(direction="SELL", entry=1.10, stop_loss=1.11, take_profit=1.08, stop_distance=0.01)
    decision = shield.check(
        order_plan=new_plan, symbol="EURUSD", open_positions=open_positions,
        new_trade_risk_pct=2.9, swing_index=1, clock=FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )

    assert decision.risk_ceiling_blocked is True  # the gap is closed
    assert decision.blocked is True


def test_get_open_positions_raises_when_positions_get_returns_none(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "positions_get", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (11, "no connection"))

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    with pytest.raises(RuntimeError, match="positions_get"):
        adapter.get_open_positions()


def test_get_open_positions_reverse_maps_broker_suffix_to_canonical_symbol(monkeypatch):
    # All other get_open_positions tests use SYMBOL_MAP = {"XAUUSD": "XAUUSD"}
    # (broker name == canonical name), which would still pass even if the
    # reverse (broker -> canonical) lookup were built backwards (e.g. by
    # accident reusing the canonical->broker dict as if it were already
    # reversed). Using a real broker suffix here is the only way to catch
    # that class of bug.
    _patch_mt5_boilerplate(monkeypatch)
    suffixed_map = {"XAUUSD": "XAUUSD.a", "EURUSD": "EURUSD.a"}
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (
            _FakePosition(symbol="XAUUSD.a", sl=2395.0, price_current=2400.0, volume=0.1, type_=mt5.POSITION_TYPE_BUY, ticket=1),
            _FakePosition(symbol="EURUSD.a", sl=1.10, price_current=1.09, volume=0.2, type_=mt5.POSITION_TYPE_SELL, ticket=2),
        ),
    )

    adapter = ThrottledDemoAdapter(
        CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=suffixed_map,
    )
    positions = adapter.get_open_positions()

    by_symbol = {pos.symbol: pos for pos in positions}
    assert set(by_symbol) == {"XAUUSD", "EURUSD"}
    assert by_symbol["XAUUSD"].direction == "BUY"
    assert by_symbol["EURUSD"].direction == "SELL"


def test_get_open_positions_raises_when_account_info_returns_none(monkeypatch):
    # account_info() must succeed for mt5_session() itself to establish the
    # connection -- so make it fail only on the SECOND call (the one
    # get_open_positions() makes itself), not the session's own login check.
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "symbol_select", lambda name, enable: True)
    monkeypatch.setattr(mt5, "symbol_info", lambda name: _FakeSymbolInfo())
    monkeypatch.setattr(mt5, "positions_get", lambda: ())
    monkeypatch.setattr(mt5, "last_error", lambda: (5, "no connection"))

    account_info_calls = {"count": 0}

    def flaky_account_info():
        account_info_calls["count"] += 1
        return _FakeAccount() if account_info_calls["count"] == 1 else None

    monkeypatch.setattr(mt5, "account_info", flaky_account_info)

    adapter = ThrottledDemoAdapter(CREDS, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=SYMBOL_MAP)
    with pytest.raises(RuntimeError, match="account_info"):
        adapter.get_open_positions()


def test_throttle_boundary_elapsed_exactly_equal_to_minimum_is_allowed(monkeypatch):
    # elapsed < min_seconds_between_trades blocks -- elapsed == min exactly
    # must NOT be blocked (boundary is exclusive on the "still throttled" side).
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []

    def fake_order_send(request):
        send_calls.append(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=len(send_calls), price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    adapter = ThrottledDemoAdapter(CREDS, clock, min_seconds_between_trades=300.0, symbol_map=SYMBOL_MAP)

    first = adapter.place_order(_request())
    clock.advance(300.0)  # exactly the minimum, not "just over"
    second = adapter.place_order(_request())

    assert first.success is True
    assert second.success is True
    assert len(send_calls) == 2
