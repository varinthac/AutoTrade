"""Unit tests for dashboard/positions.py -- MT5-query logic for currently
open positions, extracted out of dashboard/app.py so it can be shared with
notify/telegram_control.py's /positions command without pulling in a
transitive Flask dependency. Moved here (from tests/unit/dashboard/test_app.py)
alongside the code they exercise; same mocking style, only the monkeypatch
target module changed."""
from __future__ import annotations

import logging
from contextlib import contextmanager

import MetaTrader5 as mt5
import pytest

from autotrade.dashboard import positions as dashboard_positions
from autotrade.dashboard.positions import get_open_positions_display


class _FakeOpenPosition:
    def __init__(self, symbol, type_, volume, price_open, price_current, sl, tp, profit, ticket=1):
        self.symbol = symbol
        self.type = type_
        self.volume = volume
        self.price_open = price_open
        self.price_current = price_current
        self.sl = sl
        self.tp = tp
        self.profit = profit
        self.ticket = ticket


def test_get_open_positions_display_returns_none_when_mt5_session_raises(monkeypatch):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    def _raise(creds, **kwargs):
        raise RuntimeError("MT5 terminal not running")

    monkeypatch.setattr(dashboard_positions, "mt5_session", _raise)

    assert get_open_positions_display() is None


def test_get_open_positions_display_returns_none_when_positions_get_returns_none(monkeypatch):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    @contextmanager
    def _fake_session(creds, **kwargs):
        yield

    monkeypatch.setattr(dashboard_positions, "mt5_session", _fake_session)
    monkeypatch.setattr(dashboard_positions.mt5, "positions_get", lambda: None)

    assert get_open_positions_display() is None


def test_get_open_positions_display_returns_empty_list_for_genuinely_zero_positions(monkeypatch):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    @contextmanager
    def _fake_session(creds, **kwargs):
        yield

    monkeypatch.setattr(dashboard_positions, "mt5_session", _fake_session)
    monkeypatch.setattr(dashboard_positions.mt5, "positions_get", lambda: ())

    result = get_open_positions_display()

    assert result == []
    assert result is not None


def test_get_open_positions_display_maps_broker_symbol_and_skips_unmapped(monkeypatch, caplog):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(
        dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a", "EURUSD": "EURUSD.a"}},
    )

    @contextmanager
    def _fake_session(creds, **kwargs):
        yield

    monkeypatch.setattr(dashboard_positions, "mt5_session", _fake_session)
    monkeypatch.setattr(
        dashboard_positions.mt5, "positions_get",
        lambda: (
            _FakeOpenPosition(
                symbol="XAUUSD.a", type_=mt5.POSITION_TYPE_BUY, volume=0.1, price_open=2400.0,
                price_current=2410.0, sl=2390.0, tp=2420.0, profit=10.0, ticket=1,
            ),
            _FakeOpenPosition(
                symbol="UNKNOWNSYMBOL", type_=mt5.POSITION_TYPE_BUY, volume=0.1, price_open=1.0,
                price_current=1.1, sl=0.9, tp=1.2, profit=1.0, ticket=2,
            ),
        ),
    )

    with caplog.at_level(logging.WARNING):
        result = get_open_positions_display()

    assert len(result) == 1
    assert result[0].symbol == "XAUUSD"
    assert result[0].direction == "BUY"
    assert any("no canonical mapping" in record.message for record in caplog.records)


def test_get_open_positions_display_maps_sell_type_to_sell_direction(monkeypatch):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    @contextmanager
    def _fake_session(creds, **kwargs):
        yield

    monkeypatch.setattr(dashboard_positions, "mt5_session", _fake_session)
    monkeypatch.setattr(
        dashboard_positions.mt5, "positions_get",
        lambda: (
            _FakeOpenPosition(
                symbol="XAUUSD.a", type_=mt5.POSITION_TYPE_SELL, volume=0.2, price_open=2410.0,
                price_current=2405.0, sl=2420.0, tp=2390.0, profit=-5.0, ticket=1,
            ),
        ),
    )

    result = get_open_positions_display()

    assert result[0].direction == "SELL"
    assert result[0].profit == -5.0


def test_get_open_positions_display_passes_a_short_timeout_ms_to_mt5_session(monkeypatch):
    monkeypatch.setattr(dashboard_positions, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_positions, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})
    captured = {}

    @contextmanager
    def _fake_session(creds, **kwargs):
        captured.update(kwargs)
        yield

    monkeypatch.setattr(dashboard_positions, "mt5_session", _fake_session)
    monkeypatch.setattr(dashboard_positions.mt5, "positions_get", lambda: ())

    get_open_positions_display()

    assert captured.get("timeout_ms") is not None
    assert captured["timeout_ms"] <= 5000
