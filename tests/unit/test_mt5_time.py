"""Unit tests for common/mt5_time.py's server_now() — reads MT5 broker
server time (naive, not UTC, not local) off the latest tick. MT5 is mocked;
no live terminal needed."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autotrade.common import mt5_time
from autotrade.common.mt5_time import ServerClock, ServerTimeError, server_now


class _FakeTick:
    def __init__(self, time):
        self.time = time


def test_server_now_reads_tick_time_as_naive_server_time(monkeypatch):
    monkeypatch.setattr(mt5_time.mt5, "symbol_info_tick", lambda symbol: _FakeTick(1_700_000_000))

    result = server_now("XAUUSD")

    assert result == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).replace(tzinfo=None)
    assert result.tzinfo is None


def test_server_now_raises_when_tick_is_none(monkeypatch):
    monkeypatch.setattr(mt5_time.mt5, "symbol_info_tick", lambda symbol: None)
    monkeypatch.setattr(mt5_time.mt5, "last_error", lambda: (1, "no tick"))

    with pytest.raises(ServerTimeError, match="symbol_info_tick"):
        server_now("XAUUSD")


def test_server_now_raises_on_zero_filled_placeholder_tick(monkeypatch):
    # A just-selected symbol can report a zero-filled placeholder tick
    # (time=0) before the first real tick arrives.
    monkeypatch.setattr(mt5_time.mt5, "symbol_info_tick", lambda symbol: _FakeTick(0))
    monkeypatch.setattr(mt5_time.mt5, "last_error", lambda: (2, "no ticks yet"))

    with pytest.raises(ServerTimeError, match="symbol_info_tick"):
        server_now("XAUUSD")


def test_server_clock_now_matches_server_now_convention(monkeypatch):
    monkeypatch.setattr(mt5_time.mt5, "symbol_info_tick", lambda symbol: _FakeTick(1_700_000_000))

    clock = ServerClock("XAUUSD")
    result = clock.now()

    assert result == server_now("XAUUSD")
    assert result.tzinfo is None
