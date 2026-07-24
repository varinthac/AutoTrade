"""Unit tests for notify/telegram_control.py -- pure dispatch logic, no
network I/O. gui.control's start_bot/stop_bot/emergency_stop_bot/build_status/
format_status are always mocked (same "never actually launch a process"
discipline as tests/unit/test_autotrade_control.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.dashboard import views
from autotrade.notify import telegram_control
from autotrade.notify.telegram_control import PendingConfirmation, handle_update, has_text_message, is_authorized, parse_command
from autotrade.store import journal

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def _update(chat_id, text, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# --- is_authorized() --------------------------------------------------------


def test_is_authorized_matches_int_chat_id_against_str_config():
    update = _update(12345, "/status")

    assert is_authorized(update, "12345") is True


def test_is_authorized_rejects_non_matching_chat_id():
    update = _update(99999, "/status")

    assert is_authorized(update, "12345") is False


def test_is_authorized_false_when_no_message_key():
    assert is_authorized({}, "12345") is False


def test_is_authorized_false_when_no_chat_key():
    assert is_authorized({"message": {"text": "hi"}}, "12345") is False


def test_is_authorized_false_when_no_id_key():
    assert is_authorized({"message": {"chat": {}}}, "12345") is False


def test_is_authorized_never_raises_on_completely_malformed_update():
    assert is_authorized({"message": "not a dict"}, "12345") is False


# --- has_text_message() -----------------------------------------------------


def test_has_text_message_true_for_plain_text_message():
    assert has_text_message(_update(12345, "/status")) is True


def test_has_text_message_false_when_no_message():
    assert has_text_message({"update_id": 1, "edited_message": {"text": "hi"}}) is False


def test_has_text_message_false_when_message_has_no_text():
    assert has_text_message({"update_id": 1, "message": {"chat": {"id": 1}, "sticker": {}}}) is False


# --- parse_command() ---------------------------------------------------------


def test_parse_command_start():
    assert parse_command("/start") == "/start"


def test_parse_command_case_insensitive():
    assert parse_command("/STOP") == "/stop"


def test_parse_command_strips_botname_suffix():
    assert parse_command("/start@SomeBot") == "/start"


def test_parse_command_unknown_text():
    assert parse_command("hello there") == telegram_control.UNKNOWN_COMMAND


def test_parse_command_empty_string():
    assert parse_command("") == telegram_control.UNKNOWN_COMMAND


# --- PendingConfirmation ------------------------------------------------------


def test_emergency_stop_generates_code_and_does_not_call_emergency_stop_bot(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)

    reply = handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)

    assert calls == []
    assert pending.code is not None
    assert pending.code in reply


def test_correct_code_within_window_calls_emergency_stop_bot_exactly_once_and_clears_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_control.gui_control, "emergency_stop_bot",
        lambda: calls.append(1) or _FakeCompletedProcess(0),
    )
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    code = pending.code

    reply = handle_update(_update(12345, code), "12345", pending, clock)

    assert len(calls) == 1
    assert pending.code is None
    assert pending.expires_at is None
    assert "executed" in reply.lower()


def test_wrong_code_does_not_call_emergency_stop_bot(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)

    handle_update(_update(12345, "0000"), "12345", pending, clock)

    assert calls == []


def test_code_presented_after_expiry_does_not_call_emergency_stop_bot(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    code = pending.code

    clock._now = NOW + timedelta(seconds=61)
    handle_update(_update(12345, code), "12345", pending, clock)

    assert calls == []


def _distinct_codes(monkeypatch):
    # Forces distinct codes deterministically -- secrets.randbelow could
    # otherwise (rarely) produce the same 4-digit code twice and mask a bug.
    code_values = iter([1111, 2222])
    monkeypatch.setattr(telegram_control.secrets, "randbelow", lambda n: next(code_values))


def test_second_emergency_stop_invalidates_first_code(monkeypatch):
    _distinct_codes(monkeypatch)
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    first_code = pending.code
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    second_code = pending.code
    assert first_code != second_code

    handle_update(_update(12345, first_code), "12345", pending, clock)

    assert calls == []  # old code no longer works


def test_second_emergency_stop_leaves_the_new_code_working(monkeypatch):
    _distinct_codes(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_control.gui_control, "emergency_stop_bot",
        lambda: calls.append(1) or _FakeCompletedProcess(0),
    )
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    second_code = pending.code

    handle_update(_update(12345, second_code), "12345", pending, clock)

    assert len(calls) == 1


# --- dispatch: /start, /stop, /status --------------------------------------


def test_start_command_calls_start_bot_and_reports_success(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: _FakeCompletedProcess(0))
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/start"), "12345", pending, FixedClock(NOW))

    assert "start" in reply.lower()
    assert "fail" not in reply.lower()


def test_start_command_reports_failure_with_returncode_and_stderr(monkeypatch):
    monkeypatch.setattr(
        telegram_control.gui_control, "start_bot",
        lambda: _FakeCompletedProcess(1, stderr="kill switch is active"),
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/start"), "12345", pending, FixedClock(NOW))

    assert "1" in reply
    assert "kill switch is active" in reply


def test_stop_command_calls_stop_bot_and_reports_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_control.gui_control, "stop_bot",
        lambda: calls.append(1) or _FakeCompletedProcess(0),
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/stop"), "12345", pending, FixedClock(NOW))

    assert len(calls) == 1
    assert "fail" not in reply.lower()


def test_status_command_calls_build_status_and_format_status(monkeypatch):
    sentinel_report = object()
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: sentinel_report)
    monkeypatch.setattr(
        telegram_control.gui_control, "format_status",
        lambda report: "STATUS TEXT" if report is sentinel_report else "wrong",
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    assert reply == "STATUS TEXT"


def test_help_command_returns_usage_text_listing_all_four_commands():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    for cmd in ("/start", "/stop", "/status", "/emergency_stop"):
        assert cmd in reply


def test_unknown_command_returns_usage_text():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "gibberish"), "12345", pending, FixedClock(NOW))

    assert "/start" in reply


# --- /trades, /daily ---------------------------------------------------------


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "paper.sqlite"
    monkeypatch.setattr(telegram_control, "DEFAULT_PAPER_DB_PATH", path)
    return path


def _record_trade(db_path, **overrides):
    kwargs = dict(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), db_path=db_path,
    )
    kwargs.update(overrides)
    journal.record_closed_trade(**kwargs)


def test_trades_command_empty_db_returns_no_trades_message(db_path):
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert reply == "No trades recorded yet."


def test_trades_command_shows_seeded_trade_field_values(db_path):
    _record_trade(
        db_path, symbol="EURUSD", direction="SELL", net_pnl=-12.5,
        exit_reason="stop_loss", broker_ticket=1,
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert "EURUSD" in reply
    assert "SELL" in reply
    assert "-12.5" in reply
    assert "stop_loss" in reply


def test_trades_command_caps_at_ten_most_recent(db_path):
    for i in range(12):
        _record_trade(
            db_path, symbol=f"SYM{i:02d}", exit_time=datetime(2026, 7, 19, 9, i), broker_ticket=i + 1,
        )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert "Most recent 10 trade(s)" in reply
    assert "SYM11" in reply
    assert "SYM02" in reply
    assert "SYM01" not in reply
    assert "SYM00" not in reply


# --- /positions ---------------------------------------------------------


def _open_position_row(**overrides):
    kwargs = dict(
        ticket=1, symbol="XAUUSD", direction="SELL", volume=0.01, price_open=4051.48,
        price_current=4048.20, sl=4091.52, tp=3971.40, profit=15.60,
    )
    kwargs.update(overrides)
    return views.OpenPositionRow(**kwargs)


def test_positions_command_shows_no_open_positions_message_for_empty_list(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [])
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert reply == "No open positions."


def test_positions_command_shows_distinct_message_when_mt5_unreachable(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: None)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "mt5" in reply.lower()
    assert reply != "No open positions."


def test_positions_command_formats_seeded_position_field_values(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [_open_position_row()])
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "XAUUSD" in reply
    assert "SELL" in reply
    assert "0.01" in reply
    assert "4051.48" in reply
    assert "4048.20" in reply
    assert "+15.60" in reply
    assert "4091.52" in reply
    assert "3971.40" in reply


def test_positions_command_lists_multiple_positions(monkeypatch):
    rows = [
        _open_position_row(ticket=1, symbol="XAUUSD"),
        _open_position_row(ticket=2, symbol="EURUSD", direction="BUY"),
    ]
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: rows)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "2 open position(s)" in reply
    assert "XAUUSD" in reply
    assert "EURUSD" in reply


def test_positions_command_returns_graceful_reply_not_exception_when_display_raises(monkeypatch):
    def _raise():
        raise Exception("MT5 terminal not running")

    monkeypatch.setattr(telegram_control, "get_open_positions_display", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch open positions" in reply
    assert "MT5 terminal not running" in reply


def test_unauthorized_sender_cannot_reach_positions_command(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [_open_position_row()])
    pending = PendingConfirmation()

    reply = handle_update(_update(99999, "/positions"), "12345", pending, FixedClock(NOW))

    assert reply is None


def test_daily_command_empty_db_returns_no_trades_message(db_path):
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply == "No trades recorded yet."


def test_daily_command_cross_checks_against_build_daily_report_directly(db_path):
    _record_trade(
        db_path, symbol="XAUUSD", exit_time=datetime(2026, 7, 19, 10, 0), net_pnl=98.0, broker_ticket=1,
    )
    _record_trade(
        db_path, symbol="XAUUSD", exit_time=datetime(2026, 7, 19, 16, 0), net_pnl=-40.0,
        exit_reason="stop_loss", broker_ticket=2,
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    expected_report = build_daily_report(date(2026, 7, 19), db_path=db_path)
    assert reply == format_daily_report(expected_report)


def test_trades_command_returns_graceful_reply_not_exception_when_journal_raises(db_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control.journal, "get_trades_in_range", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply
    assert "database is locked" in reply


def test_daily_command_returns_graceful_reply_not_exception_when_journal_raises(db_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control.journal, "get_trades_in_range", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply
    assert "database is locked" in reply


def test_daily_command_returns_graceful_reply_when_build_daily_report_raises(db_path, monkeypatch):
    _record_trade(db_path, broker_ticket=1)

    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control, "build_daily_report", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply
    assert "database is locked" in reply


def test_help_command_lists_trades_and_daily_commands():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    assert "/trades" in reply
    assert "/positions" in reply
    assert "/daily" in reply


# --- unauthorized sender ------------------------------------------------------


def test_unauthorized_sender_never_dispatches_to_any_gui_control_function(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: calls.append("start"))
    monkeypatch.setattr(telegram_control.gui_control, "stop_bot", lambda: calls.append("stop"))
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append("estop"))
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: calls.append("status"))
    pending = PendingConfirmation()

    reply = handle_update(_update(99999, "/start"), "12345", pending, FixedClock(NOW))

    assert reply is None
    assert calls == []


def test_unauthorized_sender_cannot_reach_trades_or_daily_commands(db_path):
    _record_trade(db_path, broker_ticket=1)
    pending = PendingConfirmation()

    trades_reply = handle_update(_update(99999, "/trades"), "12345", pending, FixedClock(NOW))
    daily_reply = handle_update(_update(99999, "/daily"), "12345", pending, FixedClock(NOW))

    assert trades_reply is None
    assert daily_reply is None


# --- interleaved message cancels a pending confirmation -----------------------


def test_unrelated_message_while_pending_clears_state_with_explicit_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)

    reply = handle_update(_update(12345, "oops wrong chat"), "12345", pending, clock)

    assert pending.code is None
    assert "cancelled" in reply.lower()
    assert "/emergency_stop" in reply


def test_correct_code_after_interleaved_message_no_longer_works(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    code = pending.code
    handle_update(_update(12345, "an unrelated message"), "12345", pending, clock)

    handle_update(_update(12345, code), "12345", pending, clock)

    assert calls == []


def test_wrong_code_guess_still_gets_the_original_did_not_match_reply():
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)

    reply = handle_update(_update(12345, "0000"), "12345", pending, clock)

    assert "did not match" in reply.lower()
    assert "cancelled" not in reply.lower()


def test_unrelated_message_with_no_pending_confirmation_gets_usage_text():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "hello there"), "12345", pending, FixedClock(NOW))

    assert "/start" in reply
    assert "cancelled" not in reply.lower()


# --- confirmation code generation (secrets) ------------------------------------


def test_confirmation_code_is_always_a_zero_padded_4_digit_string(monkeypatch):
    monkeypatch.setattr(telegram_control.secrets, "randbelow", lambda n: 7)
    pending = PendingConfirmation()

    code = pending.request(FixedClock(NOW))

    assert code == "0007"
    assert len(code) == 4
    assert code.isdigit()


def test_confirmation_code_generation_uses_secrets_randbelow_of_10000(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.secrets, "randbelow", lambda n: calls.append(n) or 42)
    pending = PendingConfirmation()

    pending.request(FixedClock(NOW))

    assert calls == [10000]


# --- gui_control action raises unexpectedly ------------------------------------


def test_start_command_gui_control_raising_returns_graceful_reply_not_exception(monkeypatch):
    def _raise():
        raise OSError("bad interpreter path")

    monkeypatch.setattr(telegram_control.gui_control, "start_bot", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/start"), "12345", pending, FixedClock(NOW))

    assert "Failed to execute /start" in reply
    assert "bad interpreter path" in reply


def test_stop_command_gui_control_raising_returns_graceful_reply(monkeypatch):
    def _raise():
        raise OSError("boom")

    monkeypatch.setattr(telegram_control.gui_control, "stop_bot", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/stop"), "12345", pending, FixedClock(NOW))

    assert "Failed to execute /stop" in reply


def test_emergency_stop_confirm_gui_control_raising_returns_graceful_reply(monkeypatch):
    def _raise():
        raise OSError("boom")

    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", _raise)
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    code = pending.code

    reply = handle_update(_update(12345, code), "12345", pending, clock)

    assert "Failed to execute /emergency_stop" in reply
