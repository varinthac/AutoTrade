"""Unit tests for notify/telegram_control.py -- pure dispatch logic, no
network I/O. gui.control's start_bot/stop_bot/emergency_stop_bot/build_status/
format_status are always mocked (same "never actually launch a process"
discipline as tests/unit/test_autotrade_control.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autotrade.notify import telegram_control
from autotrade.notify.telegram_control import PendingConfirmation, handle_update, has_text_message, is_authorized, parse_command

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
