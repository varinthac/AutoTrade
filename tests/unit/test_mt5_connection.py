"""Unit tests for common/mt5_connection.py — the single shared MT5 terminal
session context manager. MT5 itself is mocked; no live terminal needed."""
from __future__ import annotations

import pytest

from autotrade.common import mt5_connection
from autotrade.common.config import MT5Credentials
from autotrade.common.mt5_connection import MT5ConnectionError, mt5_session

CREDS = MT5Credentials(login=123, password="pw", server="ICMarketsSC-Demo", terminal_path=None)


class _FakeAccount:
    login = 123
    server = "ICMarketsSC-Demo"
    balance = 1000.0
    currency = "USD"


def test_mt5_session_success_yields_and_shuts_down(monkeypatch):
    calls = {"shutdown": 0}
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1))

    entered = False
    with mt5_session(CREDS):
        entered = True
        assert calls["shutdown"] == 0  # not shut down while still inside the block

    assert entered
    assert calls["shutdown"] == 1


def test_mt5_session_passes_login_password_server_to_initialize(monkeypatch):
    captured = {}

    def fake_initialize(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(mt5_connection.mt5, "initialize", fake_initialize)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: None)

    with mt5_session(CREDS):
        pass

    assert captured == {"login": 123, "password": "pw", "server": "ICMarketsSC-Demo"}


def test_mt5_session_includes_terminal_path_when_set(monkeypatch):
    creds = MT5Credentials(login=1, password="pw", server="srv", terminal_path=r"C:\MT5\terminal64.exe")
    captured = {}

    def fake_initialize(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(mt5_connection.mt5, "initialize", fake_initialize)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: None)

    with mt5_session(creds):
        pass

    assert captured["path"] == r"C:\MT5\terminal64.exe"


def test_mt5_session_raises_when_initialize_fails_and_does_not_shutdown(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: False)
    monkeypatch.setattr(mt5_connection.mt5, "last_error", lambda: (10014, "terminal not found"))
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: shutdown_calls.append(1))

    with pytest.raises(MT5ConnectionError, match="initialize"):
        with mt5_session(CREDS):
            pytest.fail("body must not run when initialize() fails")

    assert shutdown_calls == []


def test_mt5_session_raises_and_shuts_down_when_account_info_is_none(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: None)
    monkeypatch.setattr(mt5_connection.mt5, "last_error", lambda: (10015, "login failed"))
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: shutdown_calls.append(1))

    with pytest.raises(MT5ConnectionError, match="account_info"):
        with mt5_session(CREDS):
            pytest.fail("body must not run when account_info() returns None")

    assert shutdown_calls == [1]


def test_mt5_session_shuts_down_even_if_body_raises(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: True)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: shutdown_calls.append(1))

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with mt5_session(CREDS):
            raise _Boom("orchestrator-side failure mid-session")

    assert shutdown_calls == [1]


def test_mt5_session_nested_calls_initialize_once_and_shutdown_once_at_outermost_exit(monkeypatch):
    init_calls = []
    shutdown_calls = []
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: init_calls.append(1) or True)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: shutdown_calls.append(1))

    with mt5_session(CREDS):
        assert init_calls == [1]
        with mt5_session(CREDS):
            assert init_calls == [1]  # inner call reuses the outer connection
            assert shutdown_calls == []
        assert shutdown_calls == []  # inner exit must not shut down the shared connection

    assert init_calls == [1]
    assert shutdown_calls == [1]


def test_mt5_session_sequential_calls_each_initialize_and_shutdown(monkeypatch):
    init_calls = []
    shutdown_calls = []
    monkeypatch.setattr(mt5_connection.mt5, "initialize", lambda **kwargs: init_calls.append(1) or True)
    monkeypatch.setattr(mt5_connection.mt5, "account_info", lambda: _FakeAccount())
    monkeypatch.setattr(mt5_connection.mt5, "shutdown", lambda: shutdown_calls.append(1))

    with mt5_session(CREDS):
        pass
    with mt5_session(CREDS):
        pass

    assert init_calls == [1, 1]
    assert shutdown_calls == [1, 1]
