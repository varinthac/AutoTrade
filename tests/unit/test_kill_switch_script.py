"""Unit tests for scripts/kill_switch.py — MT5 is mocked (same pattern as
tests/unit/test_mt5_connection.py); no live terminal needed. scripts/ has no
__init__.py, so the script is loaded directly via importlib."""
from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from autotrade.common import kill_switch_flag
from autotrade.common.config import MT5Credentials

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "kill_switch.py"
_spec = importlib.util.spec_from_file_location("kill_switch_script", SCRIPT_PATH)
kill_switch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = kill_switch
_spec.loader.exec_module(kill_switch)

mt5 = kill_switch.mt5  # the real MetaTrader5 module, for its type/retcode constants
CREDS = MT5Credentials(login=1, password="pw", server="srv", terminal_path=None)


class _FakePosition:
    def __init__(self, ticket, symbol, volume, position_type):
        self.ticket = ticket
        self.symbol = symbol
        self.volume = volume
        self.type = position_type


class _FakeTick:
    def __init__(self, bid=1.0, ask=1.1, time=1):
        self.bid = bid
        self.ask = ask
        self.time = time


class _FakeSendResult:
    def __init__(self, retcode, price=1.0, volume=1.0, comment=""):
        self.retcode = retcode
        self.price = price
        self.volume = volume
        self.comment = comment


@pytest.fixture
def flag_path(tmp_path, monkeypatch):
    path = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(kill_switch_flag, "DEFAULT_FLAG_PATH", path)
    return path


@contextmanager
def _fake_session(creds):
    yield


# --- close_all_open_positions() ---------------------------------------


def test_close_all_open_positions_returns_empty_list_when_none_open(monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: ())
    assert kill_switch.close_all_open_positions() == []


def test_close_all_open_positions_raises_when_positions_get_fails(monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (1, "no connection"))
    with pytest.raises(RuntimeError, match="positions_get"):
        kill_switch.close_all_open_positions()


def test_close_all_open_positions_closes_buy_with_sell_and_vice_versa(monkeypatch):
    positions = (
        _FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),
        _FakePosition(2, "EURUSD", 0.2, mt5.POSITION_TYPE_SELL),
    )
    captured_requests = []

    monkeypatch.setattr(mt5, "positions_get", lambda: positions)
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())

    def fake_order_send(request):
        captured_requests.append(request)
        return _FakeSendResult(mt5.TRADE_RETCODE_DONE, volume=request["volume"])

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    results = kill_switch.close_all_open_positions()

    assert [r.success for r in results] == [True, True]
    assert captured_requests[0]["type"] == mt5.ORDER_TYPE_SELL  # closes the BUY
    assert captured_requests[0]["position"] == 1
    assert captured_requests[0]["symbol"] == "XAUUSD"
    assert captured_requests[0]["volume"] == 0.1
    assert captured_requests[1]["type"] == mt5.ORDER_TYPE_BUY  # closes the SELL
    assert captured_requests[1]["position"] == 2


def test_close_all_open_positions_reports_order_send_none_as_failure(monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (2, "no connection to trade server"))

    results = kill_switch.close_all_open_positions()

    assert len(results) == 1
    assert results[0].success is False
    assert "returned None" in results[0].message


def test_close_all_open_positions_reports_rejected_retcode_as_failure(monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_SELL),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())
    monkeypatch.setattr(mt5, "order_send", lambda request: _FakeSendResult(mt5.TRADE_RETCODE_REJECT))

    results = kill_switch.close_all_open_positions()

    assert results[0].success is False
    assert "rejected" in results[0].message


def test_close_all_open_positions_reports_missing_tick_as_failure(monkeypatch):
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (3, "symbol not found"))

    results = kill_switch.close_all_open_positions()

    assert results[0].success is False
    assert "symbol_info_tick" in results[0].message


# --- do_activate() ------------------------------------------------------


def test_do_activate_rejects_empty_reason(flag_path):
    assert kill_switch.do_activate("   ") == 1
    assert not flag_path.exists()


def test_do_activate_writes_flag_before_closing_even_if_close_fails(flag_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (2, "no connection"))

    exit_code = kill_switch.do_activate("testing partial failure")

    assert exit_code == 1
    assert kill_switch_flag.is_active(flag_path) is True


def test_do_activate_notifies_manual_intervention_required_when_a_close_fails(flag_path, monkeypatch):
    # A user relying on Telegram alone (not the console/log) must still
    # learn that a position failed to close -- not just that the kill
    # switch was activated.
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(42, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())
    monkeypatch.setattr(mt5, "order_send", lambda request: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (2, "no connection"))
    calls = []
    monkeypatch.setattr(kill_switch, "notify", lambda text: calls.append(text))

    exit_code = kill_switch.do_activate("testing manual intervention notify")

    assert exit_code == 1
    assert len(calls) == 2
    assert "MANUAL INTERVENTION REQUIRED" in calls[1]
    assert "42" in calls[1]


def test_do_activate_notifies_once_with_reason(flag_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(mt5, "positions_get", lambda: ())
    calls = []
    monkeypatch.setattr(kill_switch, "notify", lambda text: calls.append(text))

    exit_code = kill_switch.do_activate("testing notify")

    assert exit_code == 0  # existing behavior unaffected by notify being mocked
    assert len(calls) == 1
    assert "testing notify" in calls[0]


def test_do_activate_notifies_before_attempting_to_close_positions(flag_path, monkeypatch):
    # Same "flag first" fail-safe ordering as the halt flag itself -- notify
    # must fire even when connecting to MT5 to close positions fails. Two
    # notifies now: the initial activation, and a second one reporting that
    # closing positions itself failed (so a user relying only on Telegram
    # -- not the console -- still learns positions may still be open).
    def _raise_creds():
        raise RuntimeError("MT5_LOGIN not set")

    monkeypatch.setattr(kill_switch, "load_mt5_credentials", _raise_creds)
    calls = []
    monkeypatch.setattr(kill_switch, "notify", lambda text: calls.append(text))

    exit_code = kill_switch.do_activate("testing notify before close attempt")

    assert exit_code == 1
    assert len(calls) == 2
    assert "testing notify before close attempt" in calls[0]
    assert "FAILED" in calls[1]
    assert "check the terminal manually" in calls[1]


def test_do_activate_succeeds_with_zero_positions(flag_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(mt5, "positions_get", lambda: ())

    exit_code = kill_switch.do_activate("testing zero positions")

    assert exit_code == 0
    assert kill_switch_flag.is_active(flag_path) is True


def test_do_activate_succeeds_when_all_positions_close(flag_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(mt5, "positions_get", lambda: (_FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),))
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())
    monkeypatch.setattr(mt5, "order_send", lambda request: _FakeSendResult(mt5.TRADE_RETCODE_DONE, volume=0.1))

    exit_code = kill_switch.do_activate("testing success")

    assert exit_code == 0
    assert kill_switch_flag.is_active(flag_path) is True


def test_do_activate_reports_partial_failure_when_some_positions_fail_to_close(flag_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(kill_switch, "mt5_session", _fake_session)
    monkeypatch.setattr(
        mt5, "positions_get",
        lambda: (
            _FakePosition(1, "XAUUSD", 0.1, mt5.POSITION_TYPE_BUY),
            _FakePosition(2, "EURUSD", 0.2, mt5.POSITION_TYPE_SELL),
        ),
    )
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: _FakeTick())

    def fake_order_send(request):
        if request["symbol"] == "XAUUSD":
            return _FakeSendResult(mt5.TRADE_RETCODE_DONE, volume=request["volume"])
        return _FakeSendResult(mt5.TRADE_RETCODE_REJECT, comment="Rejected")

    monkeypatch.setattr(mt5, "order_send", fake_order_send)

    exit_code = kill_switch.do_activate("testing mixed results")

    assert exit_code == 1  # one failure among two must still surface as overall failure
    assert kill_switch_flag.is_active(flag_path) is True


def test_do_activate_reports_connection_failure_loudly_without_pretending_success(flag_path, monkeypatch):
    def _raise_creds():
        raise RuntimeError("MT5_LOGIN not set")

    monkeypatch.setattr(kill_switch, "load_mt5_credentials", _raise_creds)

    exit_code = kill_switch.do_activate("testing connect failure")

    assert exit_code == 1
    assert kill_switch_flag.is_active(flag_path) is True


# --- do_status() / do_deactivate() -------------------------------------


def test_do_status_reports_inactive_without_touching_mt5(flag_path):
    assert kill_switch.do_status() == 0


def test_do_status_reports_active_with_reason(flag_path):
    kill_switch_flag.activate("manual test halt")
    assert kill_switch.do_status() == 0


def test_do_deactivate_requires_confirm(flag_path):
    kill_switch_flag.activate("some reason")
    exit_code = kill_switch.do_deactivate(confirm=False)
    assert exit_code == 1
    assert kill_switch_flag.is_active() is True


def test_do_deactivate_clears_flag_when_confirmed(flag_path):
    kill_switch_flag.activate("some reason")
    exit_code = kill_switch.do_deactivate(confirm=True)
    assert exit_code == 0
    assert kill_switch_flag.is_active() is False


def test_do_deactivate_is_clean_noop_when_not_active(flag_path):
    exit_code = kill_switch.do_deactivate(confirm=True)
    assert exit_code == 0


# --- main() CLI dispatch -------------------------------------------------


def test_main_dispatches_status_flag_to_do_status(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kill_switch.py", "--status"])
    called = {"called": False}

    def fake_do_status():
        called["called"] = True
        return 0

    monkeypatch.setattr(kill_switch, "do_status", fake_do_status)

    assert kill_switch.main() == 0
    assert called["called"] is True


def test_main_dispatches_activate_flag_with_reason_to_do_activate(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kill_switch.py", "--activate", "manual halt reason"])
    captured = {}

    def fake_do_activate(reason):
        captured["reason"] = reason
        return 0

    monkeypatch.setattr(kill_switch, "do_activate", fake_do_activate)

    assert kill_switch.main() == 0
    assert captured["reason"] == "manual halt reason"


def test_main_dispatches_deactivate_with_confirm_to_do_deactivate(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kill_switch.py", "--deactivate", "--confirm"])
    captured = {}

    def fake_do_deactivate(confirm):
        captured["confirm"] = confirm
        return 0

    monkeypatch.setattr(kill_switch, "do_deactivate", fake_do_deactivate)

    assert kill_switch.main() == 0
    assert captured["confirm"] is True


def test_main_requires_one_of_the_mutually_exclusive_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kill_switch.py"])

    with pytest.raises(SystemExit):
        kill_switch.main()


def test_main_rejects_activate_and_status_together(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["kill_switch.py", "--activate", "x", "--status"])

    with pytest.raises(SystemExit):
        kill_switch.main()
