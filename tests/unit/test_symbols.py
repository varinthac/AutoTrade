"""Unit tests for common/symbols.py — canonical<->broker symbol mapping and
SYMBOL_INFO resolution. MT5 itself is mocked; no live terminal needed."""
from __future__ import annotations

import pytest

from autotrade.common import symbols
from autotrade.common.symbols import UnknownSymbolError, get_symbol_spec, to_broker_name

SAMPLE_MAP = {"XAUUSD": "XAUUSD", "EURUSD": "EURUSD.a"}


def test_to_broker_name_known_symbol_maps_correctly():
    assert to_broker_name("XAUUSD", SAMPLE_MAP) == "XAUUSD"
    assert to_broker_name("EURUSD", SAMPLE_MAP) == "EURUSD.a"


def test_to_broker_name_unknown_symbol_raises_with_available_choices_listed():
    with pytest.raises(UnknownSymbolError) as exc_info:
        to_broker_name("BTCUSD", SAMPLE_MAP)
    message = str(exc_info.value)
    assert "BTCUSD" in message
    # error should surface what *is* configured, to speed up debugging a typo
    assert "EURUSD" in message and "XAUUSD" in message


def test_to_broker_name_falls_back_to_yaml_config_when_no_map_given(monkeypatch):
    monkeypatch.setattr(symbols, "_load_symbol_map", lambda: SAMPLE_MAP)
    assert to_broker_name("EURUSD") == "EURUSD.a"


class _FakeSymbolInfo:
    def __init__(self):
        self.digits = 2
        self.point = 0.01
        self.trade_tick_size = 0.01
        self.trade_tick_value = 1.0
        self.trade_contract_size = 100.0
        self.volume_min = 0.01
        self.volume_max = 100.0
        self.volume_step = 0.01
        self.trade_stops_level = 30
        self.trade_freeze_level = 10


def test_get_symbol_spec_success_maps_all_fields(monkeypatch):
    monkeypatch.setattr(symbols.mt5, "symbol_select", lambda name, enable: True)
    monkeypatch.setattr(symbols.mt5, "symbol_info", lambda name: _FakeSymbolInfo())

    spec = get_symbol_spec("XAUUSD", SAMPLE_MAP)

    assert spec.canonical == "XAUUSD"
    assert spec.broker_name == "XAUUSD"
    assert spec.digits == 2
    assert spec.tick_size == 0.01
    assert spec.tick_value == 1.0
    assert spec.contract_size == 100.0
    assert spec.volume_min == 0.01
    assert spec.volume_max == 100.0
    assert spec.volume_step == 0.01
    assert spec.trade_stops_level == 30
    assert spec.freeze_level == 10


def test_get_symbol_spec_raises_when_symbol_select_fails(monkeypatch):
    monkeypatch.setattr(symbols.mt5, "symbol_select", lambda name, enable: False)
    monkeypatch.setattr(symbols.mt5, "last_error", lambda: (-1, "symbol not found"))
    # symbol_info should never even be consulted once select fails
    monkeypatch.setattr(
        symbols.mt5, "symbol_info", lambda name: (_ for _ in ()).throw(AssertionError("should not be called"))
    )

    with pytest.raises(UnknownSymbolError, match="symbol_select"):
        get_symbol_spec("XAUUSD", SAMPLE_MAP)


def test_get_symbol_spec_raises_when_symbol_info_returns_none(monkeypatch):
    monkeypatch.setattr(symbols.mt5, "symbol_select", lambda name, enable: True)
    monkeypatch.setattr(symbols.mt5, "symbol_info", lambda name: None)
    monkeypatch.setattr(symbols.mt5, "last_error", lambda: (-2, "no info"))

    with pytest.raises(UnknownSymbolError, match="symbol_info"):
        get_symbol_spec("XAUUSD", SAMPLE_MAP)


def test_get_symbol_spec_unknown_canonical_symbol_raises_before_touching_mt5(monkeypatch):
    monkeypatch.setattr(
        symbols.mt5, "symbol_select", lambda name, enable: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    with pytest.raises(UnknownSymbolError):
        get_symbol_spec("BTCUSD", SAMPLE_MAP)
