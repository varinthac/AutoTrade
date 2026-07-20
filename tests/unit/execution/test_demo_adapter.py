"""Unit tests for execution/demo_adapter.py — ThrottledDemoAdapter. MT5 itself
is mocked (same pattern as test_mt5_connection.py/test_poller.py); no live
terminal needed."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from autotrade.common.config import MT5Credentials
from autotrade.council.order_construction import OrderPlan
from autotrade.execution.adapter import TradeRequest
from autotrade.execution.demo_adapter import ThrottledDemoAdapter, mt5
from autotrade.shield.checkpoint import OpenPositionInfo, Shield
from autotrade.store import journal

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
    def __init__(self, symbol, sl, price_current, volume, type_, ticket=1, tp=0.0):
        self.symbol = symbol
        self.sl = sl
        self.price_current = price_current
        self.volume = volume
        self.type = type_
        self.ticket = ticket
        self.tp = tp


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


def _adapter(clock=None, **kwargs) -> ThrottledDemoAdapter:
    """Shared construction helper -- defaults `symbol_map` and, crucially,
    `sleep_fn` to a no-op so the Phase 7b retry logic (2 retries, 3s apart by
    default) never actually blocks the test suite in real wall-clock time.
    Tests that specifically want to observe the retry delay pass their own
    `sleep_fn` (a spy) via `**kwargs`."""
    clock = clock or FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    kwargs.setdefault("symbol_map", SYMBOL_MAP)
    kwargs.setdefault("sleep_fn", lambda seconds: None)
    return ThrottledDemoAdapter(CREDS, clock, **kwargs)


def test_successful_order_send_returns_correct_order_result(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))

    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=555, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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
    adapter = _adapter(clock, min_seconds_between_trades=300.0)

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
    adapter = _adapter(clock, min_seconds_between_trades=300.0)

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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    with caplog.at_level(logging.WARNING):
        result = adapter.place_order(_request(entry=2400.0))

    assert result.success is True
    assert any("differs from requested entry" in record.message for record in caplog.records)


def test_get_equity_reads_account_info_equity(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert adapter.get_equity() == 1234.5


def test_get_equity_raises_when_account_info_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (5, "no connection"))

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    with pytest.raises(RuntimeError, match="account_info"):
        adapter.get_equity()


def test_get_balance_reads_account_info_balance(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert adapter.get_balance() == 1000.0


def test_get_balance_raises_when_account_info_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5, "shutdown", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (5, "no connection"))

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    with pytest.raises(RuntimeError, match="account_info"):
        adapter.get_balance()


def test_missing_tick_returns_failure_without_sending_order(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (7, "symbol not found"))

    def boom(request):
        pytest.fail("order_send must not be called when the tick is unavailable")

    monkeypatch.setattr(mt5, "order_send", boom)

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    result = adapter.place_order(_request())

    assert result.success is False
    assert result.broker_ticket is None
    assert "symbol_info_tick" in result.message


def test_zero_time_tick_is_treated_same_as_missing_tick(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1, time=0))
    monkeypatch.setattr(mt5, "last_error", lambda: (7, "stale/placeholder tick"))

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    result = adapter.place_order(_request())

    assert result.success is False
    assert "symbol_info_tick" in result.message


def test_order_send_returning_none_is_reported_as_failure(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (9, "no connection to trade server"))

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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
    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    result = adapter.place_order(sell_request)

    assert result.success is True
    assert captured["type"] == mt5.ORDER_TYPE_SELL
    assert captured["price"] == 2399.9  # bid, since SELL


def test_invalid_direction_raises_value_error(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    with caplog.at_level(logging.WARNING):
        result = adapter.place_order(_request(lot_size=0.1))

    assert result.success is True
    assert any("differs from requested lot_size" in record.message for record in caplog.records)


def test_get_open_positions_returns_empty_list_when_no_open_positions(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "positions_get", lambda: ())

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert adapter.get_open_positions() == []


def test_get_open_positions_maps_broker_symbol_and_computes_risk_pct(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    # equity=1234.5 (from _FakeAccount). distance=|2400-2395|=5, point_value=1.0/0.01=100,
    # volume=0.1 -> risk_amount=5*100*0.1=50 -> risk_pct=50/1234.5*100.
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    positions = adapter.get_open_positions()

    assert positions[0].direction == "SELL"


def test_get_open_positions_skips_position_with_unmapped_broker_symbol(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (_FakePosition(symbol="UNKNOWNSYMBOL", sl=1.0, price_current=1.1, volume=0.1, type_=mt5.POSITION_TYPE_BUY),),
    )

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), symbol_map=suffixed_map)
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

    adapter = _adapter(FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
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
    adapter = _adapter(clock, min_seconds_between_trades=300.0)

    first = adapter.place_order(_request())
    clock.advance(300.0)  # exactly the minimum, not "just over"
    second = adapter.place_order(_request())

    assert first.success is True
    assert second.success is True
    assert len(send_calls) == 2


# --- Phase 7b: retry (Appendix A §4.8 item 1) ------------------------------


def test_place_order_retries_on_requote_then_succeeds(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []

    def fake_order_send(request):
        send_calls.append(request)
        if len(send_calls) < 3:
            return _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE, comment="Requote")
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    sleep_calls = []
    adapter = _adapter(sleep_fn=sleep_calls.append)
    result = adapter.place_order(_request())

    assert result.success is True
    assert len(send_calls) == 3  # 1 initial + 2 retries
    assert sleep_calls == [3.0, 3.0]  # default retry_delay_sec, between attempts only


def test_place_order_exhausts_retries_and_logs_execution_failed(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []

    def fake_order_send(request):
        send_calls.append(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE, comment="Requote")

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter(max_retries=2)
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(_request())

    assert result.success is False
    assert len(send_calls) == 3  # 1 initial + 2 retries, never chases price with more
    assert any("execution_failed" in record.message for record in caplog.records)


def test_place_order_retry_never_varies_request_between_attempts(monkeypatch):
    # No price-chasing: every retry must resend the EXACT same request dict.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    captured_requests = []

    def fake_order_send(request):
        captured_requests.append(dict(request))
        if len(captured_requests) < 2:
            return _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE, comment="Requote")
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter()
    adapter.place_order(_request())

    assert captured_requests[0] == captured_requests[1]


def test_place_order_none_result_retried_then_fails(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (9, "no connection to trade server"))

    adapter = _adapter(max_retries=1)
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(_request())

    assert result.success is False
    assert "returned None" in result.message
    assert any("execution_failed" in record.message for record in caplog.records)


def test_place_order_zero_retries_sends_exactly_once(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: send_calls.append(request) or _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE),
    )

    adapter = _adapter(max_retries=0)
    result = adapter.place_order(_request())

    assert result.success is False
    assert len(send_calls) == 1


# --- Phase 7b: partial fill + abnormal slippage (Appendix A §4.8 items 3-4) -


def test_partial_fill_flags_partial_fill_true_on_order_result(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE_PARTIAL, order=1, price=2400.1, volume=0.05),
    )

    adapter = _adapter()
    result = adapter.place_order(_request(lot_size=0.1))

    assert result.success is True
    assert result.partial_fill is True
    assert result.filled_volume == 0.05  # caller must use this, not the original lot_size


def test_full_fill_leaves_partial_fill_false(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.1),
    )

    adapter = _adapter()
    result = adapter.place_order(_request(lot_size=0.1))

    assert result.partial_fill is False


def test_abnormal_slippage_within_rr_floor_logs_but_stays_open(monkeypatch, caplog):
    # Fill slips 2.0 beyond entry (> 0.3 * ATR(5.0)=1.5), but SL/TP are wide
    # enough that realized R:R stays above the 1.3 floor -- must log
    # abnormal_slippage but NOT close the position.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2402.0, volume=0.1),
    )

    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2420.0,
    )
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        result = adapter.place_order(request, current_atr=5.0)

    # risk = |2402-2395|=7, reward=|2420-2402|=18 -> R:R=18/7=2.57 >= 1.3
    assert result.success is True
    assert result.closed_due_to_slippage is False
    assert any("abnormal_slippage" in record.message for record in caplog.records)


def test_abnormal_slippage_below_rr_floor_closes_position_immediately(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2402.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=77, tp=2405.0),
        ) if kwargs.get("ticket") == 77 else (),
    )
    close_send_calls = []

    def fake_close_send(request):
        close_send_calls.append(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1)

    def routed_order_send(request):
        # order_send is used for BOTH the initial open and the follow-up
        # close -- distinguish by whether "position" is present.
        if "position" in request:
            return fake_close_send(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", routed_order_send)

    # entry intended 2400.0, filled 2402.0 -> slippage=2.0 > 0.3*ATR(5.0)=1.5.
    # SL=2395, TP=2401.5 -> risk=|2402-2395|=7, reward=|2401.5-2402|=0.5 -> R:R=0.07 < 1.3.
    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2401.5,
    )
    adapter = _adapter()
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(request, current_atr=5.0)

    assert result.success is False
    assert result.closed_due_to_slippage is True
    assert result.broker_ticket == 77
    assert len(close_send_calls) == 1
    assert any("abnormal_slippage" in record.message for record in caplog.records)


def test_abnormal_slippage_self_close_writes_trade_record(monkeypatch, caplog):
    # Regression test (code-reviewer + test-engineer confirmed): an
    # abnormal-slippage self-close is a real, P&L-bearing trade -- it must
    # land in the trade journal alongside the AnomalyEventRecord, not just
    # log an anomaly, otherwise Phase 8b's Auditor never sees these
    # worst-execution trades in its win-rate/profit-factor stats.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2402.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=77, tp=2405.0),
        ) if kwargs.get("ticket") == 77 else (),
    )

    def routed_order_send(request):
        if "position" in request:
            return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", routed_order_send)

    deals = (
        # Unix timestamps for 2026-07-19 10:00/10:01 UTC -- matching the
        # FakeClock's day below, so get_trades_for_day(2026-07-19) finds it.
        _FakeDeal(entry=mt5.DEAL_ENTRY_IN, reason=mt5.DEAL_REASON_CLIENT, profit=0.0,
                  price=2402.0, time=1_784_455_200, volume=0.1),
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_CLIENT, profit=-5.0,
                  commission=-1.0, swap=0.0, price=2401.5, time=1_784_455_260, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals if kwargs.get("position") == 77 else ())

    # entry intended 2400.0, filled 2402.0 -> slippage=2.0 > 0.3*ATR(5.0)=1.5.
    # SL=2395, TP=2401.5 -> risk=|2402-2395|=7, reward=|2401.5-2402|=0.5 -> R:R=0.07 < 1.3.
    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2401.5,
    )
    clock = FakeClock(datetime(2026, 7, 19, 10, 0))
    adapter = _adapter(clock=clock)
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(request, current_atr=5.0)

    assert result.success is False
    assert result.closed_due_to_slippage is True

    trades = journal.get_trades_for_day(date(2026, 7, 19))
    assert len(trades) == 1
    trade = trades[0]
    assert trade.broker_ticket == 77
    assert trade.exit_reason == "abnormal_slippage"
    assert trade.exit_price == 2401.5
    assert trade.gross_pnl == pytest.approx(-5.0)
    assert trade.cost == pytest.approx(1.0)
    assert trade.net_pnl == pytest.approx(-6.0)
    # Appendix A §5.1 daily-report fields (should-fix #5) -- spread at entry
    # (bid=2401.9/ask=2402.0 -> 0.1 price / 0.01 point = 10.0 points) and the
    # ACTUAL fill-vs-intended slippage (filled 2402.0 vs intended entry 2400.0).
    assert trade.entry_spread_points == pytest.approx(10.0)
    assert trade.actual_slippage == pytest.approx(2.0)

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19))
    assert any(e.event_type == "abnormal_slippage" for e in events)


def test_place_order_retry_request_identical_across_all_three_attempts(monkeypatch):
    # test_place_order_retry_never_varies_request_between_attempts (above)
    # only compares 2 of the up-to-3 requests actually sent (it succeeds on
    # attempt 2). This drives a full 1-initial+2-retries sequence and
    # compares ALL THREE captured requests, closing the "only checked a
    # prefix of the attempts" rigor gap.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    captured_requests = []

    def fake_order_send(request):
        captured_requests.append(dict(request))
        if len(captured_requests) < 3:
            return _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE, comment="Requote")
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter(max_retries=2)
    result = adapter.place_order(_request())

    assert result.success is True
    assert len(captured_requests) == 3
    assert captured_requests[0] == captured_requests[1] == captured_requests[2]


# --- Phase 7b: retry distinguishes retryable vs. terminal/ambiguous       --
# --- retcodes (code-reviewer finding #2, fixed)                           --


def test_place_order_does_not_retry_terminal_no_money_retcode(monkeypatch):
    # NO_MONEY (insufficient margin) can NEVER succeed via retry -- resending
    # the byte-identical order will not conjure margin that doesn't exist.
    # Unlike REQUOTE/REJECT (genuinely transient, tested above with the SAME
    # retry mechanics), this structurally-terminal retcode must fail on the
    # FIRST attempt, without consuming any of the retry budget/delay.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: send_calls.append(request) or _FakeSendResult(
            retcode=mt5.TRADE_RETCODE_NO_MONEY, comment="Not enough money",
        ),
    )

    sleep_calls = []
    adapter = _adapter(max_retries=2, sleep_fn=sleep_calls.append)
    result = adapter.place_order(_request())

    assert result.success is False
    assert result.retcode == mt5.TRADE_RETCODE_NO_MONEY
    # Fixed (code-reviewer finding #2): NO_MONEY is structurally terminal --
    # a single attempt, no retries, no wasted retry-delay sleep.
    assert len(send_calls) == 1
    assert sleep_calls == []


def test_place_order_does_not_retry_none_result_to_avoid_double_fill_on_market_order(monkeypatch):
    # order_send() returning None (or TIMEOUT/CONNECTION) for a market DEAL
    # (place_order, as opposed to modify/close) can mean the order actually
    # WAS executed server-side and only the acknowledgment was lost --
    # blindly resending the exact same market order on retry risks a
    # double-fill (two real positions instead of one). Fixed (code-reviewer
    # finding #2): a non-idempotent TRADE_ACTION_DEAL must NOT retry on this
    # ambiguous outcome -- it fails immediately on the first attempt instead.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []
    monkeypatch.setattr(mt5, "order_send", lambda request: send_calls.append(dict(request)) or None)
    monkeypatch.setattr(mt5, "last_error", lambda: (9, "no connection to trade server"))

    adapter = _adapter(max_retries=2)
    result = adapter.place_order(_request())

    assert result.success is False
    # Only ONE order_send() call -- an ambiguous None outcome on a market
    # DEAL is now treated as immediately terminal, never blindly resent.
    assert len(send_calls) == 1
    assert send_calls[0]["action"] == mt5.TRADE_ACTION_DEAL


# --- Phase 7b: compounding failure -- bad fill AND the follow-up close    --
# --- also fails (gap identified by the code-reviewer's parallel audit,    --
# --- fixed: retry the close once more, then alert loudly and record the   --
# --- position for Watchman if it's still failing)                         --


def test_abnormal_slippage_close_failure_retries_and_alerts_critical_when_still_failing(
    monkeypatch, caplog,
):
    # Worst case: the entry fill triggers the abnormal-slippage auto-close
    # (Appendix A §4.8 items 3-4), AND the follow-up close_position() itself
    # fails every retry (broker rejects the close too), even after being
    # retried a second time. Fixed behavior: the close is attempted TWICE
    # (each with its own internal _send_with_retry attempts), and if it's
    # STILL failing after that, place_order() must be loud (CRITICAL) and
    # honestly report that the position is still open -- never claim
    # "closed_due_to_slippage" for a close that never actually succeeded.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2402.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=77, tp=2405.0),
        ) if kwargs.get("ticket") == 77 else (),
    )
    close_send_calls = []

    def routed_order_send(request):
        if "position" in request:
            close_send_calls.append(request)
            return _FakeSendResult(retcode=mt5.TRADE_RETCODE_REJECT, comment="close rejected too")
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", routed_order_send)

    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2401.5,
    )
    adapter = _adapter(max_retries=1)
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(request, current_atr=5.0)

    # close_position() was called TWICE (initial + one more retry attempt),
    # each of which internally retried once more (max_retries=1 -> 2
    # order_send attempts per close_position() call) -- 2 x 2 = 4 total.
    assert len(close_send_calls) == 4
    assert result.success is False
    assert result.broker_ticket == 77
    # Fixed: closed_due_to_slippage is now False (the close never actually
    # succeeded) and position_still_open is True -- the entry fill DID go
    # through, so this is a REAL open position that still needs managing.
    assert result.closed_due_to_slippage is False
    assert result.position_still_open is True
    assert "position closed immediately" not in result.message
    assert "CLOSE FAILED" in result.message
    assert "reject" in result.message.lower()
    # close_position()'s OWN failure handling must still be loud (a distinct
    # ERROR log per attempt) -- this part of the compounding-failure path
    # was already trustworthy and must stay that way.
    assert sum(
        1 for r in caplog.records
        if "close_position(ticket=77) failed" in r.message and "execution_failed" in r.message
    ) == 2
    # And on top of that, the still-failing outcome must be a loud, distinct
    # CRITICAL alert -- not just another ERROR indistinguishable from the
    # routine per-attempt logging above.
    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1
    assert "77" in critical_records[0].message
    assert "XAUUSD" in critical_records[0].message


def test_abnormal_slippage_close_not_found_retries_and_records_position_for_next_watchman_cycle(
    monkeypatch, caplog,
):
    # Directly exercises the code-reviewer's "MT5 eventual-consistency race"
    # question through the real place_order() -> close_position() path (not
    # just close_position() in isolation): positions_get() finds NOTHING for
    # the just-filled ticket (simulating the just-opened position not yet
    # being visible to positions_get() inside the same mt5_session). Fixed
    # behavior: close_position() is retried once more (re-fetching via
    # _get_position again, which is exactly where a real eventual-
    # consistency race would resolve) before giving up loudly.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(mt5, "positions_get", lambda **kwargs: ())  # never finds it -- any ticket

    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=77, price=2402.0, volume=0.1),
    )

    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2401.5,
    )
    adapter = _adapter()
    with caplog.at_level(logging.ERROR):
        result = adapter.place_order(request, current_atr=5.0)

    assert result.success is False
    assert result.broker_ticket == 77
    # Fixed: never claims the close succeeded when the position couldn't
    # even be found -- and flags it as still open so the caller (orchestrator/
    # shadow_loop.py) records Watchman metadata for it despite the overall
    # failure, so the next Watchman cycle picks it up via get_open_positions().
    assert result.closed_due_to_slippage is False
    assert result.position_still_open is True
    # _get_position() was retried too -- "no open position found" logged
    # (at least) twice, once per close_position() attempt.
    assert sum(
        1 for r in caplog.records if "no open position found for ticket=77" in r.message
    ) >= 2
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_no_current_atr_supplied_skips_slippage_check_entirely(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2401.9, ask=2402.0))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2402.0, volume=0.1),
    )

    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2401.5,
    )
    adapter = _adapter()
    result = adapter.place_order(request)  # current_atr defaults to None

    assert result.success is True
    assert result.closed_due_to_slippage is False


# --- Phase 7b: modify_stop_loss (Appendix A §4.8 item 2) -------------------


def test_modify_stop_loss_success(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=5, tp=2410.0),
        ) if kwargs.get("ticket") == 5 else (),
    )
    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=5)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter()
    result = adapter.modify_stop_loss(5, 2397.0)

    assert result.success is True
    assert result.broker_ticket == 5
    assert result.filled_price == 2397.0
    assert captured["action"] == mt5.TRADE_ACTION_SLTP
    assert captured["position"] == 5
    assert captured["sl"] == 2397.0
    assert captured["tp"] == 2410.0  # preserved, not cleared


def test_modify_stop_loss_clamps_to_broker_stops_level(monkeypatch, caplog):
    # BUY, price=2400.0, stops_level=30 points @ point=0.01 -> min distance
    # 0.30. Requesting SL=2399.99 (0.01 away) violates it -- must clamp to
    # 2399.70 (0.30 away) instead of sending the too-close value.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=5, tp=2410.0),
        ) if kwargs.get("ticket") == 5 else (),
    )
    captured = {}
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: captured.update(request) or _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=5),
    )

    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        result = adapter.modify_stop_loss(5, 2399.99)

    assert result.success is True
    assert result.filled_price == pytest.approx(2399.70)
    assert captured["sl"] == pytest.approx(2399.70)
    assert any("clamped" in record.message for record in caplog.records)


def test_modify_stop_loss_sell_direction_clamps_upward(monkeypatch):
    # SELL, price=2400.0, stops_level=30 points @ point=0.01 -> min distance
    # 0.30. Requesting SL=2400.01 (too close) must clamp to 2400.30.
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2405.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_SELL, ticket=6, tp=2390.0),
        ) if kwargs.get("ticket") == 6 else (),
    )
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=6),
    )

    adapter = _adapter()
    result = adapter.modify_stop_loss(6, 2400.01)

    assert result.filled_price == pytest.approx(2400.30)


def test_modify_stop_loss_no_clamp_needed_when_already_far_enough(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=5, tp=2410.0),
        ) if kwargs.get("ticket") == 5 else (),
    )
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=5),
    )

    adapter = _adapter()
    result = adapter.modify_stop_loss(5, 2398.0)  # 2.0 away, well beyond 0.30 min

    assert result.filled_price == pytest.approx(2398.0)


def test_modify_stop_loss_ticket_not_found_returns_failure(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "positions_get", lambda **kwargs: ())

    adapter = _adapter()
    result = adapter.modify_stop_loss(999, 2397.0)

    assert result.success is False
    assert "no open position found" in result.message


def test_modify_stop_loss_exhausts_retries_and_logs_execution_failed(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=5, tp=2410.0),
        ) if kwargs.get("ticket") == 5 else (),
    )
    send_calls = []
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: send_calls.append(request) or _FakeSendResult(retcode=mt5.TRADE_RETCODE_REQUOTE),
    )

    adapter = _adapter(max_retries=2)
    with caplog.at_level(logging.ERROR):
        result = adapter.modify_stop_loss(5, 2397.0)

    assert result.success is False
    assert len(send_calls) == 3
    assert any("execution_failed" in record.message for record in caplog.records)


# --- Phase 7b: close_position (Appendix A §4.8, news protection) -----------


def test_close_position_full_close(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.2,
                           type_=mt5.POSITION_TYPE_BUY, ticket=8, tp=2410.0),
        ) if kwargs.get("ticket") == 8 else (),
    )
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=8, price=2399.9, volume=0.2)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter()
    result = adapter.close_position(8)

    assert result.success is True
    assert result.filled_volume == 0.2
    assert result.filled_price == 2399.9
    assert captured["type"] == mt5.ORDER_TYPE_SELL  # opposite of the BUY position
    assert captured["volume"] == 0.2
    assert captured["position"] == 8


def test_close_position_partial_close_uses_requested_volume(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.2,
                           type_=mt5.POSITION_TYPE_BUY, ticket=8, tp=2410.0),
        ) if kwargs.get("ticket") == 8 else (),
    )
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=8, price=2399.9, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter()
    result = adapter.close_position(8, volume=0.1)

    assert result.success is True
    assert result.filled_volume == 0.1
    assert captured["volume"] == 0.1


def test_close_position_sell_position_uses_buy_to_close(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2405.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_SELL, ticket=9, tp=2390.0),
        ) if kwargs.get("ticket") == 9 else (),
    )
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    captured = {}

    def fake_order_send(request):
        captured.update(request)
        return _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=9, price=2400.1, volume=0.1)

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    adapter = _adapter()
    result = adapter.close_position(9)

    assert captured["type"] == mt5.ORDER_TYPE_BUY
    assert captured["price"] == 2400.1  # ask, since buying to close a SELL


def test_close_position_requested_volume_exceeds_open_volume_returns_failure(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=8, tp=2410.0),
        ) if kwargs.get("ticket") == 8 else (),
    )

    def boom(request):
        pytest.fail("order_send must not be called with an invalid volume")

    monkeypatch.setattr(mt5, "order_send", boom)

    adapter = _adapter()
    result = adapter.close_position(8, volume=0.5)

    assert result.success is False
    assert "invalid" in result.message.lower()


def test_close_position_ticket_not_found_returns_failure(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "positions_get", lambda **kwargs: ())

    adapter = _adapter()
    result = adapter.close_position(999)

    assert result.success is False
    assert "no open position found" in result.message


def test_close_position_exhausts_retries_and_logs_execution_failed(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda **kwargs: (
            _FakePosition(symbol="XAUUSD", sl=2395.0, price_current=2400.0, volume=0.1,
                           type_=mt5.POSITION_TYPE_BUY, ticket=8, tp=2410.0),
        ) if kwargs.get("ticket") == 8 else (),
    )
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    send_calls = []
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: send_calls.append(request) or _FakeSendResult(retcode=mt5.TRADE_RETCODE_REJECT),
    )

    adapter = _adapter(max_retries=2)
    with caplog.at_level(logging.ERROR):
        result = adapter.close_position(8)

    assert result.success is False
    assert len(send_calls) == 3
    assert any("execution_failed" in record.message for record in caplog.records)


# --- get_closed_trade_info() (Phase 8a reconciliation) ----------------------


class _FakeDeal:
    def __init__(self, entry, reason, profit=0.0, commission=0.0, swap=0.0, price=0.0, time=0, volume=0.0):
        self.entry = entry
        self.reason = reason
        self.profit = profit
        self.commission = commission
        self.swap = swap
        self.price = price
        self.time = time
        self.volume = volume


def test_get_closed_trade_info_sl_hit_maps_to_stop_loss(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    deals = (
        _FakeDeal(entry=mt5.DEAL_ENTRY_IN, reason=mt5.DEAL_REASON_CLIENT, profit=0.0, price=2400.0, time=1_700_000_000, volume=0.1),
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_SL, profit=-50.0, commission=-2.0, swap=-0.5,
                  price=2395.0, time=1_700_003_600, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals if kwargs.get("position") == 8 else ())

    adapter = _adapter()
    info = adapter.get_closed_trade_info(8)

    assert info is not None
    assert info.exit_reason == "stop_loss"
    assert info.close_price == 2395.0
    assert info.closed_volume == pytest.approx(0.1)
    assert info.gross_pnl == pytest.approx(-50.0)
    assert info.cost == pytest.approx(2.5)  # -(sum of commission+swap) = -(-2.0 + -0.5) = 2.5
    assert info.close_time == datetime.fromtimestamp(1_700_003_600, tz=timezone.utc).replace(tzinfo=None)


def test_get_closed_trade_info_tp_hit_maps_to_take_profit(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    deals = (
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_TP, profit=80.0, price=2410.0,
                  time=1_700_003_600, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals)

    info = _adapter().get_closed_trade_info(8)

    assert info.exit_reason == "take_profit"


def test_get_closed_trade_info_expert_closed_maps_to_manual(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    deals = (
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_EXPERT, profit=10.0, price=2401.0,
                  time=1_700_003_600, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals)

    info = _adapter().get_closed_trade_info(8)

    assert info.exit_reason == "manual"


def test_get_closed_trade_info_stop_out_maps_to_unknown_and_logs_warning(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    deals = (
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_SO, profit=-500.0, price=2300.0,
                  time=1_700_003_600, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals)

    with caplog.at_level(logging.WARNING):
        info = _adapter().get_closed_trade_info(8)

    assert info.exit_reason == "unknown"
    assert any("not recognized as SL/TP/manual" in r.message for r in caplog.records)


def test_get_closed_trade_info_aggregates_gross_pnl_and_cost_across_all_deals(monkeypatch):
    # An earlier partial close (news protection half-close) plus the final
    # full close -- both exit deals' profit/commission/swap must be summed,
    # not just the final one's.
    _patch_mt5_boilerplate(monkeypatch)
    deals = (
        _FakeDeal(entry=mt5.DEAL_ENTRY_IN, reason=mt5.DEAL_REASON_CLIENT, profit=0.0, commission=-1.0,
                  price=2400.0, time=1_700_000_000, volume=0.2),
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_CLIENT, profit=40.0, commission=-1.0,
                  price=2410.0, time=1_700_003_600, volume=0.1),
        _FakeDeal(entry=mt5.DEAL_ENTRY_OUT, reason=mt5.DEAL_REASON_SL, profit=-20.0, commission=-1.0, swap=-0.5,
                  price=2395.0, time=1_700_007_200, volume=0.1),
    )
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals)

    info = _adapter().get_closed_trade_info(8)

    assert info.gross_pnl == pytest.approx(20.0)  # 0 + 40 + -20
    assert info.cost == pytest.approx(3.5)  # -(-1 + -1 + -1 + -0.5)
    assert info.closed_volume == pytest.approx(0.2)  # 0.1 + 0.1, the two exit deals
    # last exit deal chronologically is the SL hit -- close price/time/reason come from it
    assert info.exit_reason == "stop_loss"
    assert info.close_price == 2395.0


def test_get_closed_trade_info_no_deals_in_history_returns_none(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: ())

    with caplog.at_level(logging.WARNING):
        info = _adapter().get_closed_trade_info(8)

    assert info is None
    assert any("no deals found in MT5 history" in r.message for r in caplog.records)


def test_get_closed_trade_info_only_entry_deal_no_exit_yet_returns_none(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    deals = (_FakeDeal(entry=mt5.DEAL_ENTRY_IN, reason=mt5.DEAL_REASON_CLIENT, price=2400.0, time=1_700_000_000, volume=0.1),)
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: deals)

    with caplog.at_level(logging.WARNING):
        info = _adapter().get_closed_trade_info(8)

    assert info is None
    assert any("none is an exit deal" in r.message for r in caplog.records)


def test_get_closed_trade_info_query_failure_returns_none(monkeypatch, caplog):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "history_deals_get", lambda **kwargs: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (1, "some MT5 error"))

    with caplog.at_level(logging.WARNING):
        info = _adapter().get_closed_trade_info(8)

    assert info is None
    assert any("history_deals_get" in r.message for r in caplog.records)


# --- anomaly-event recording alongside execution_failed/abnormal_slippage --


def test_execution_failed_on_rejected_order_records_anomaly_event(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2400.1))
    monkeypatch.setattr(
        mt5, "order_send", lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_REJECT),
    )

    clock = FakeClock(datetime(2026, 7, 19, 10, 0))
    adapter = _adapter(clock=clock, max_retries=0)
    adapter.place_order(_request())

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19))
    assert any(e.event_type == "order_reject" for e in events)


def test_abnormal_slippage_records_anomaly_event(monkeypatch):
    _patch_mt5_boilerplate(monkeypatch)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda name: _FakeTick(bid=2399.9, ask=2450.0))
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: _FakeSendResult(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=2450.0, volume=0.1),
    )

    clock = FakeClock(datetime(2026, 7, 19, 10, 0))
    adapter = _adapter(clock=clock, max_entry_slippage_atr=0.3, min_rr_after_slippage=100.0)
    adapter.place_order(_request(entry=2400.0), current_atr=1.0)

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19))
    assert any(e.event_type == "abnormal_slippage" for e in events)
