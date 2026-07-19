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
