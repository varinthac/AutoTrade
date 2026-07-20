"""Unit tests for execution/noop_adapter.py — the zero-risk dry-run
adapter."""
from __future__ import annotations

from autotrade.execution.adapter import TradeRequest
from autotrade.execution.noop_adapter import NoOpBrokerAdapter


def test_place_order_always_succeeds_and_echoes_request():
    adapter = NoOpBrokerAdapter()
    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2410.0,
    )

    result = adapter.place_order(request)

    assert result.success is True
    assert result.broker_ticket is None
    assert result.filled_price == 2400.0
    assert result.filled_volume == 0.1
    assert result.retcode is None
    assert result.message


def test_get_equity_returns_configured_fixed_value():
    assert NoOpBrokerAdapter(fixed_equity=5_000.0).get_equity() == 5_000.0
    assert NoOpBrokerAdapter().get_equity() == 10_000.0


def test_get_balance_returns_same_fixed_value_as_get_equity():
    # No floating P&L in a dry run.
    adapter = NoOpBrokerAdapter(fixed_equity=5_000.0)
    assert adapter.get_balance() == adapter.get_equity() == 5_000.0


def test_get_open_positions_always_returns_empty_list():
    # A dry run never actually opens a position with any broker.
    adapter = NoOpBrokerAdapter()
    assert adapter.get_open_positions() == []

    adapter.place_order(TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2410.0,
    ))
    assert adapter.get_open_positions() == []


def test_place_order_ignores_current_atr_and_never_flags_slippage():
    adapter = NoOpBrokerAdapter()
    request = TradeRequest(
        symbol="XAUUSD", direction="BUY", lot_size=0.1,
        entry=2400.0, stop_loss=2395.0, take_profit=2410.0,
    )

    result = adapter.place_order(request, current_atr=0.5)

    assert result.success is True
    assert result.partial_fill is False
    assert result.closed_due_to_slippage is False


def test_modify_stop_loss_always_succeeds_and_echoes_new_sl():
    adapter = NoOpBrokerAdapter()

    result = adapter.modify_stop_loss(ticket=42, new_stop_loss=2397.0)

    assert result.success is True
    assert result.broker_ticket == 42
    assert result.filled_price == 2397.0
    assert result.message


def test_close_position_full_close_echoes_ticket_and_none_volume():
    adapter = NoOpBrokerAdapter()

    result = adapter.close_position(ticket=42)

    assert result.success is True
    assert result.broker_ticket == 42
    assert result.filled_volume is None


def test_close_position_partial_close_echoes_requested_volume():
    adapter = NoOpBrokerAdapter()

    result = adapter.close_position(ticket=42, volume=0.05)

    assert result.success is True
    assert result.filled_volume == 0.05
