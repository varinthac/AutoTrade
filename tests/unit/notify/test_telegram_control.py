"""Unit tests for notify/telegram_control.py -- pure dispatch logic, no
network I/O. gui.control's start_bot/stop_bot/emergency_stop_bot/build_status/
format_status are always mocked (same "never actually launch a process"
discipline as tests/unit/test_autotrade_control.py). notify/charts.py's
build_equity_curve_png/build_daily_pnl_png are likewise always mocked here --
this module only needs to prove /daily ATTACHES whatever charts.py returns,
not that matplotlib itself renders a correct PNG (that's charts.py's own
test_charts.py's job)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.dashboard import views
from autotrade.notify import telegram_control
from autotrade.notify.telegram_control import (
    PendingConfirmation,
    handle_callback_query,
    handle_update,
    has_callback_query,
    has_text_message,
    is_authorized,
    parse_callback_data,
    parse_command,
)
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


def _callback_update(chat_id, data, update_id: int = 1, callback_query_id: str = "cbq1") -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_query_id,
            "data": data,
            "from": {"id": chat_id},
            "message": {"chat": {"id": chat_id}},
        },
    }


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


def test_is_authorized_matches_callback_query_chat_id():
    update = _callback_update(12345, "cmd:positions")

    assert is_authorized(update, "12345") is True


def test_is_authorized_rejects_non_matching_callback_query_chat_id():
    update = _callback_update(99999, "cmd:positions")

    assert is_authorized(update, "12345") is False


def test_is_authorized_never_raises_on_malformed_callback_query():
    assert is_authorized({"callback_query": "not a dict"}, "12345") is False
    assert is_authorized({"callback_query": {"message": "not a dict"}}, "12345") is False


# --- has_text_message() -----------------------------------------------------


def test_has_text_message_true_for_plain_text_message():
    assert has_text_message(_update(12345, "/status")) is True


def test_has_text_message_false_when_no_message():
    assert has_text_message({"update_id": 1, "edited_message": {"text": "hi"}}) is False


def test_has_text_message_false_when_message_has_no_text():
    assert has_text_message({"update_id": 1, "message": {"chat": {"id": 1}, "sticker": {}}}) is False


# --- has_callback_query() -----------------------------------------------------


def test_has_callback_query_true_for_valid_callback_query():
    assert has_callback_query(_callback_update(12345, "cmd:positions")) is True


def test_has_callback_query_false_when_no_callback_query():
    assert has_callback_query(_update(12345, "/status")) is False


def test_has_callback_query_false_when_callback_query_has_no_data():
    assert has_callback_query({"update_id": 1, "callback_query": {"id": "cbq1"}}) is False


def test_has_callback_query_false_when_callback_query_is_not_a_dict():
    assert has_callback_query({"update_id": 1, "callback_query": "not a dict"}) is False


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


# --- parse_callback_data() ----------------------------------------------------


def test_parse_callback_data_positions_maps_to_positions_command():
    assert parse_callback_data("cmd:positions") == "/positions"


def test_parse_callback_data_trades_maps_to_trades_command():
    assert parse_callback_data("cmd:trades") == "/trades"


def test_parse_callback_data_daily_maps_to_daily_command():
    assert parse_callback_data("cmd:daily") == "/daily"


def test_parse_callback_data_unknown_string_returns_unknown_command():
    assert parse_callback_data("cmd:emergency_stop") == telegram_control.UNKNOWN_COMMAND


def test_parse_callback_data_empty_string_returns_unknown_command():
    assert parse_callback_data("") == telegram_control.UNKNOWN_COMMAND


def test_parse_callback_data_never_maps_to_a_consequential_command():
    # /start, /stop, /emergency_stop must never be reachable via a button
    # tap -- a deliberate safety boundary, not an oversight.
    for data in ("cmd:start", "cmd:stop", "cmd:emergency_stop"):
        assert parse_callback_data(data) == telegram_control.UNKNOWN_COMMAND


# --- PendingConfirmation ------------------------------------------------------


def test_emergency_stop_generates_code_and_does_not_call_emergency_stop_bot(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append(1))
    pending = PendingConfirmation()
    clock = FixedClock(NOW)

    reply = handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)

    assert calls == []
    assert pending.code is not None
    assert pending.code in reply.text


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
    assert "executed" in reply.text.lower()


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

    assert "start" in reply.text.lower()
    assert "fail" not in reply.text.lower()


def test_start_command_reports_failure_with_returncode_and_stderr(monkeypatch):
    monkeypatch.setattr(
        telegram_control.gui_control, "start_bot",
        lambda: _FakeCompletedProcess(1, stderr="kill switch is active"),
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/start"), "12345", pending, FixedClock(NOW))

    assert "1" in reply.text
    assert "kill switch is active" in reply.text


def test_stop_command_calls_stop_bot_and_reports_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_control.gui_control, "stop_bot",
        lambda: calls.append(1) or _FakeCompletedProcess(0),
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/stop"), "12345", pending, FixedClock(NOW))

    assert len(calls) == 1
    assert "fail" not in reply.text.lower()


def test_status_command_calls_build_status_and_format_status(monkeypatch):
    sentinel_report = object()
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: sentinel_report)
    monkeypatch.setattr(
        telegram_control.gui_control, "format_status",
        lambda report: "STATUS TEXT" if report is sentinel_report else "wrong",
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    assert reply.text == "STATUS TEXT"


def test_status_command_reply_carries_quick_access_inline_keyboard(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: "STATUS TEXT")
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    buttons = reply.reply_markup["inline_keyboard"][0]
    callback_data = {button["callback_data"] for button in buttons}
    assert callback_data == {"cmd:positions", "cmd:trades", "cmd:daily"}


def test_status_command_reply_omits_webapp_button_when_webapp_url_not_provided(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: "STATUS TEXT")
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    assert len(reply.reply_markup["inline_keyboard"]) == 1


def test_status_command_reply_includes_webapp_button_when_webapp_url_provided(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: "STATUS TEXT")
    pending = PendingConfirmation()

    reply = handle_update(
        _update(12345, "/status"), "12345", pending, FixedClock(NOW), webapp_url="https://trade.kylerlink.com"
    )

    webapp_row = reply.reply_markup["inline_keyboard"][1]
    assert webapp_row == [{"text": "Open Dashboard", "web_app": {"url": "https://trade.kylerlink.com"}}]


def test_help_command_reply_includes_webapp_button_when_webapp_url_provided():
    pending = PendingConfirmation()

    reply = handle_update(
        _update(12345, "/help"), "12345", pending, FixedClock(NOW), webapp_url="https://trade.kylerlink.com"
    )

    webapp_row = reply.reply_markup["inline_keyboard"][1]
    assert webapp_row == [{"text": "Open Dashboard", "web_app": {"url": "https://trade.kylerlink.com"}}]


def test_help_command_returns_usage_text_listing_all_four_commands():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    for cmd in ("/start", "/stop", "/status", "/emergency_stop"):
        assert cmd in reply.text


def test_help_command_reply_carries_quick_access_inline_keyboard():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    buttons = reply.reply_markup["inline_keyboard"][0]
    callback_data = {button["callback_data"] for button in buttons}
    assert callback_data == {"cmd:positions", "cmd:trades", "cmd:daily"}


def test_quick_access_keyboard_never_offers_start_stop_or_emergency_stop_buttons():
    # A deliberate safety boundary -- consequential actions must stay
    # explicit-typed-command-only, never reachable via a one-tap button.
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    buttons = reply.reply_markup["inline_keyboard"][0]
    callback_data = {button["callback_data"] for button in buttons}
    assert callback_data.isdisjoint({"cmd:start", "cmd:stop", "cmd:emergency_stop"})


def test_unknown_command_returns_usage_text():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "gibberish"), "12345", pending, FixedClock(NOW))

    assert "/start" in reply.text


def test_non_status_help_commands_do_not_carry_reply_markup(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: _FakeCompletedProcess(0))
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/start"), "12345", pending, FixedClock(NOW))

    assert reply.reply_markup is None


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

    assert reply.text == "No trades recorded yet."


def test_trades_command_shows_seeded_trade_field_values(db_path):
    _record_trade(
        db_path, symbol="EURUSD", direction="SELL", net_pnl=-12.5,
        exit_reason="stop_loss", broker_ticket=1,
    )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert "EURUSD" in reply.text
    assert "SELL" in reply.text
    assert "-12.5" in reply.text
    assert "stop_loss" in reply.text


def test_trades_command_caps_at_ten_most_recent(db_path):
    for i in range(12):
        _record_trade(
            db_path, symbol=f"SYM{i:02d}", exit_time=datetime(2026, 7, 19, 9, i), broker_ticket=i + 1,
        )
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert "Most recent 10 trade(s)" in reply.text
    assert "SYM11" in reply.text
    assert "SYM02" in reply.text
    assert "SYM01" not in reply.text
    assert "SYM00" not in reply.text


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

    assert reply.text == "No open positions."


def test_positions_command_shows_distinct_message_when_mt5_unreachable(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: None)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "mt5" in reply.text.lower()
    assert reply.text != "No open positions."


def test_positions_command_formats_seeded_position_field_values(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [_open_position_row()])
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "#1" in reply.text  # broker ticket, 2026-07-29 user request
    assert "XAUUSD" in reply.text
    assert "SELL" in reply.text
    assert "0.01" in reply.text
    assert "4051.48" in reply.text
    assert "4048.20" in reply.text
    assert "+15.60" in reply.text
    assert "4091.52" in reply.text
    assert "3971.40" in reply.text


def test_positions_command_lists_multiple_positions(monkeypatch):
    rows = [
        _open_position_row(ticket=1, symbol="XAUUSD"),
        _open_position_row(ticket=2, symbol="EURUSD", direction="BUY"),
    ]
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: rows)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert "2 open position(s)" in reply.text
    assert "XAUUSD" in reply.text
    assert "EURUSD" in reply.text


def test_positions_command_returns_graceful_reply_not_exception_when_display_raises(monkeypatch):
    def _raise():
        raise Exception("MT5 terminal not running")

    monkeypatch.setattr(telegram_control, "get_open_positions_display", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/positions"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch open positions" in reply.text
    assert "MT5 terminal not running" in reply.text


def test_unauthorized_sender_cannot_reach_positions_command(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [_open_position_row()])
    pending = PendingConfirmation()

    reply = handle_update(_update(99999, "/positions"), "12345", pending, FixedClock(NOW))

    assert reply is None


# --- /dashboard ---------------------------------------------------------


def test_parse_command_dashboard():
    assert parse_command("/dashboard") == "/dashboard"


def test_dashboard_command_already_running_reports_pid_and_url(monkeypatch):
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: 555)
    monkeypatch.setattr(telegram_control.pid_file, "is_pid_running", lambda pid: True)
    spawn_calls = []
    monkeypatch.setattr(telegram_control, "spawn_detached", lambda *a, **k: spawn_calls.append(1) or True)
    pending = PendingConfirmation()

    reply = handle_update(
        _update(12345, "/dashboard"), "12345", pending, FixedClock(NOW), webapp_url="https://trade.kylerlink.com",
    )

    assert "555" in reply.text
    assert "already running" in reply.text.lower()
    assert "https://trade.kylerlink.com" in reply.text
    assert spawn_calls == []  # already up -- must never spawn a second instance


def test_dashboard_command_already_running_reports_no_url_when_webapp_not_configured(monkeypatch):
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: 555)
    monkeypatch.setattr(telegram_control.pid_file, "is_pid_running", lambda pid: True)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/dashboard"), "12345", pending, FixedClock(NOW))

    assert "555" in reply.text
    assert "WEBAPP_URL" in reply.text


def test_dashboard_command_spawns_via_the_shared_service_watchdog_helper_when_not_running(monkeypatch):
    pids = iter([None, 777])
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: next(pids))
    monkeypatch.setattr(telegram_control.pid_file, "is_pid_running", lambda pid: True)
    spawn_calls = []
    monkeypatch.setattr(
        telegram_control, "spawn_detached",
        lambda name, script, cwd: spawn_calls.append((name, script, cwd)) or True,
    )
    sleep_calls = []
    monkeypatch.setattr(telegram_control.time, "sleep", lambda sec: sleep_calls.append(sec))
    pending = PendingConfirmation()

    reply = handle_update(
        _update(12345, "/dashboard"), "12345", pending, FixedClock(NOW), webapp_url="https://trade.kylerlink.com",
    )

    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == "Dashboard"
    assert str(spawn_calls[0][1]).endswith("run_dashboard.py")
    assert sleep_calls == [telegram_control._DASHBOARD_SPAWN_CONFIRM_WAIT_SEC]
    assert "started" in reply.text.lower()
    assert "777" in reply.text
    assert "https://trade.kylerlink.com" in reply.text
    assert "idle" in reply.text.lower()


def test_dashboard_command_spawn_launch_failure_reports_failure_without_waiting(monkeypatch):
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: None)
    monkeypatch.setattr(telegram_control, "spawn_detached", lambda *a, **k: False)
    sleep_calls = []
    monkeypatch.setattr(telegram_control.time, "sleep", lambda sec: sleep_calls.append(sec))
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/dashboard"), "12345", pending, FixedClock(NOW))

    assert "failed" in reply.text.lower()
    assert sleep_calls == []  # never waits for a launch that never happened


def test_dashboard_command_spawn_succeeds_but_pid_never_confirmed(monkeypatch):
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: None)
    monkeypatch.setattr(telegram_control, "spawn_detached", lambda *a, **k: True)
    monkeypatch.setattr(telegram_control.time, "sleep", lambda sec: None)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/dashboard"), "12345", pending, FixedClock(NOW))

    assert "could not confirm" in reply.text.lower()


def test_unauthorized_sender_cannot_reach_dashboard_command(monkeypatch):
    monkeypatch.setattr(telegram_control.pid_file, "read", lambda pid_path=None: 555)
    monkeypatch.setattr(telegram_control.pid_file, "is_pid_running", lambda pid: True)
    spawn_calls = []
    monkeypatch.setattr(telegram_control, "spawn_detached", lambda *a, **k: spawn_calls.append(1) or True)
    pending = PendingConfirmation()

    reply = handle_update(_update(99999, "/dashboard"), "12345", pending, FixedClock(NOW))

    assert reply is None
    assert spawn_calls == []


def test_dashboard_command_never_offered_as_inline_button():
    # Grouped with /start/stop/emergency_stop, not the read-only quick-access
    # trio -- see module docstring for why (spawns a process, same as /start).
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    buttons = reply.reply_markup["inline_keyboard"][0]
    callback_data = {button["callback_data"] for button in buttons}
    assert "cmd:dashboard" not in callback_data


def test_help_command_lists_dashboard_command():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    assert "/dashboard" in reply.text


def test_daily_command_empty_db_returns_no_trades_message(db_path):
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply.text == "No trades recorded yet."
    assert reply.photos == []


def _mock_charts(monkeypatch):
    calls = []

    def _fake_equity(trades):
        calls.append(("equity", trades))
        return b"EQUITY-PNG-BYTES"

    def _fake_daily_pnl(trades):
        calls.append(("daily_pnl", trades))
        return b"DAILY-PNL-PNG-BYTES"

    monkeypatch.setattr(telegram_control.charts, "build_equity_curve_png", _fake_equity)
    monkeypatch.setattr(telegram_control.charts, "build_daily_pnl_png", _fake_daily_pnl)
    return calls


def test_daily_command_attaches_equity_and_daily_pnl_charts(db_path, monkeypatch):
    calls = _mock_charts(monkeypatch)
    _record_trade(db_path, broker_ticket=1)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert len(reply.photos) == 2
    assert reply.photos[0].png == b"EQUITY-PNG-BYTES"
    assert reply.photos[0].caption == "Equity curve"
    assert reply.photos[1].png == b"DAILY-PNL-PNG-BYTES"
    assert reply.photos[1].caption == "Daily net P/L"
    assert [name for name, _ in calls] == ["equity", "daily_pnl"]


def test_daily_command_charts_built_from_full_trade_history_not_just_the_day(db_path, monkeypatch):
    # Charts must reflect the WHOLE recorded history, not just the single
    # server_date the text report is scoped to -- an equity curve/daily P/L
    # bar chart with only one day's data would be a near-useless image.
    calls = _mock_charts(monkeypatch)
    _record_trade(db_path, exit_time=datetime(2026, 7, 18, 10, 0), broker_ticket=1)
    _record_trade(db_path, exit_time=datetime(2026, 7, 19, 10, 0), broker_ticket=2)
    pending = PendingConfirmation()

    handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    equity_trades = calls[0][1]
    assert len(equity_trades) == 2


def test_daily_command_degrades_to_text_only_reply_when_chart_rendering_fails(db_path, monkeypatch):
    def _raise(trades):
        raise RuntimeError("matplotlib not available")

    monkeypatch.setattr(telegram_control.charts, "build_equity_curve_png", _raise)
    _record_trade(db_path, broker_ticket=1)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply.photos == []
    assert "Daily report" in reply.text  # the text report itself must still come through


def test_non_daily_commands_never_carry_photos(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: "STATUS TEXT")
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/status"), "12345", pending, FixedClock(NOW))

    assert reply.photos == []


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
    assert reply.text == format_daily_report(expected_report)


def test_trades_command_returns_graceful_reply_not_exception_when_journal_raises(db_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control.journal, "get_trades_in_range", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/trades"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply.text
    assert "database is locked" in reply.text


def test_daily_command_returns_graceful_reply_not_exception_when_journal_raises(db_path, monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control.journal, "get_trades_in_range", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply.text
    assert "database is locked" in reply.text


def test_daily_command_returns_graceful_reply_when_build_daily_report_raises(db_path, monkeypatch):
    _record_trade(db_path, broker_ticket=1)

    def _raise(*args, **kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(telegram_control, "build_daily_report", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/daily"), "12345", pending, FixedClock(NOW))

    assert reply is not None
    assert "Failed to fetch trade data" in reply.text
    assert "database is locked" in reply.text


def test_help_command_lists_trades_and_daily_commands():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/help"), "12345", pending, FixedClock(NOW))

    assert "/trades" in reply.text
    assert "/positions" in reply.text
    assert "/daily" in reply.text


# --- handle_callback_query() (tapped inline-keyboard buttons) ----------------


def test_handle_callback_query_positions_matches_typed_command_reply(monkeypatch):
    rows = [_open_position_row()]
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: rows)

    typed_reply = handle_update(_update(12345, "/positions"), "12345", PendingConfirmation(), FixedClock(NOW))
    tapped_reply = handle_callback_query(_callback_update(12345, "cmd:positions"), "12345")

    assert tapped_reply.text == typed_reply.text
    assert tapped_reply.photos == []


def test_handle_callback_query_trades_matches_typed_command_reply(db_path):
    _record_trade(db_path, symbol="EURUSD", broker_ticket=1)

    typed_reply = handle_update(_update(12345, "/trades"), "12345", PendingConfirmation(), FixedClock(NOW))
    tapped_reply = handle_callback_query(_callback_update(12345, "cmd:trades"), "12345")

    assert tapped_reply.text == typed_reply.text


def test_handle_callback_query_daily_matches_typed_command_reply(db_path, monkeypatch):
    _mock_charts(monkeypatch)
    _record_trade(db_path, broker_ticket=1)

    typed_reply = handle_update(_update(12345, "/daily"), "12345", PendingConfirmation(), FixedClock(NOW))
    tapped_reply = handle_callback_query(_callback_update(12345, "cmd:daily"), "12345")

    assert tapped_reply.text == typed_reply.text
    assert [p.png for p in tapped_reply.photos] == [p.png for p in typed_reply.photos]


def test_handle_callback_query_unknown_data_returns_usage_text():
    reply = handle_callback_query(_callback_update(12345, "cmd:nonsense"), "12345")

    assert "/start" in reply.text


def test_handle_callback_query_unauthorized_sender_returns_none(monkeypatch):
    monkeypatch.setattr(telegram_control, "get_open_positions_display", lambda: [])

    reply = handle_callback_query(_callback_update(99999, "cmd:positions"), "12345")

    assert reply is None


def test_handle_callback_query_never_dispatches_to_gui_control_for_consequential_commands(monkeypatch):
    # cmd:start/cmd:stop/cmd:emergency_stop are never emitted by this
    # project's own keyboard, but a hand-crafted callback data string must
    # still never reach gui_control -- parse_callback_data() maps anything
    # outside the three read-only commands to UNKNOWN_COMMAND.
    calls = []
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: calls.append("start"))
    monkeypatch.setattr(telegram_control.gui_control, "stop_bot", lambda: calls.append("stop"))
    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", lambda: calls.append("estop"))

    reply = handle_callback_query(_callback_update(12345, "cmd:emergency_stop"), "12345")

    assert calls == []
    assert reply is not None


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
    assert "cancelled" in reply.text.lower()
    assert "/emergency_stop" in reply.text


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

    assert "did not match" in reply.text.lower()
    assert "cancelled" not in reply.text.lower()


def test_unrelated_message_with_no_pending_confirmation_gets_usage_text():
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "hello there"), "12345", pending, FixedClock(NOW))

    assert "/start" in reply.text
    assert "cancelled" not in reply.text.lower()


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

    assert "Failed to execute /start" in reply.text
    assert "bad interpreter path" in reply.text


def test_stop_command_gui_control_raising_returns_graceful_reply(monkeypatch):
    def _raise():
        raise OSError("boom")

    monkeypatch.setattr(telegram_control.gui_control, "stop_bot", _raise)
    pending = PendingConfirmation()

    reply = handle_update(_update(12345, "/stop"), "12345", pending, FixedClock(NOW))

    assert "Failed to execute /stop" in reply.text


def test_emergency_stop_confirm_gui_control_raising_returns_graceful_reply(monkeypatch):
    def _raise():
        raise OSError("boom")

    monkeypatch.setattr(telegram_control.gui_control, "emergency_stop_bot", _raise)
    pending = PendingConfirmation()
    clock = FixedClock(NOW)
    handle_update(_update(12345, "/emergency_stop"), "12345", pending, clock)
    code = pending.code

    reply = handle_update(_update(12345, code), "12345", pending, clock)

    assert "Failed to execute /emergency_stop" in reply.text
