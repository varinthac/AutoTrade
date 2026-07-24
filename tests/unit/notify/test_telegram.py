"""Unit tests for notify/telegram.py -- urllib.request.urlopen is mocked, no
real Telegram credentials/network needed. `notify()` must never raise and
must be a no-op (zero HTTP calls) when not configured or disabled."""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse

import pytest

from autotrade.notify import telegram


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": True}})


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return b'{"ok": true}'


def test_notify_posts_correct_url_and_payload(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify("hello world", timeout_sec=2.5)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 2.5
    body = captured["data"].decode("utf-8")
    assert "chat_id=CHAT456" in body
    assert "text=hello" in body


def test_notify_swallows_network_exception_and_returns_false(monkeypatch, configured):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify("hello")

    assert result is False


def test_notify_swallows_http_error_and_returns_false(monkeypatch, configured):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.notify("hello") is False


def test_notify_never_raises_on_unexpected_exception(monkeypatch, configured):
    def fake_urlopen(request, timeout):
        raise ValueError("something unexpected")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.notify("hello") is False


def test_notify_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify("hello")

    assert result is False
    assert called["count"] == 0


def test_notify_makes_zero_http_calls_when_notifications_disabled(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify("hello")

    assert result is False
    assert called["count"] == 0


def test_notify_defaults_timeout_sec_to_4_0(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    telegram.notify("hello")

    assert captured["timeout"] == 4.0


def test_notify_swallows_socket_timeout_and_returns_false(monkeypatch, configured):
    def fake_urlopen(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.notify("hello") is False


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda url: urllib.error.URLError("connection refused"),
        lambda url: urllib.error.HTTPError(url, 401, "Unauthorized", {}, None),
        lambda url: socket.timeout("timed out"),
        lambda url: ValueError("something unexpected"),
    ],
    ids=["URLError", "HTTPError", "socket.timeout", "generic Exception"],
)
def test_notify_failure_log_never_contains_raw_token(monkeypatch, configured, caplog, make_exc):
    # The token lives in the URL path (https://api.telegram.org/bot<TOKEN>/sendMessage).
    # notify() must never let that token end up in a log line even when the
    # send fails, regardless of which exception type urlopen raises.
    def fake_urlopen(request, timeout):
        raise make_exc(request.full_url)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.notify("some message body that should still be logged")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text


def test_notify_swallows_exception_from_load_telegram_credentials(monkeypatch):
    # config/base.yaml or .env I/O hiccups must not escape notify() -- only
    # the network call used to be wrapped; everything before it (credential
    # loading, config loading, URL/payload construction) must be covered too.
    def _raise():
        raise OSError("transient .env read failure")

    monkeypatch.setattr(telegram, "load_telegram_credentials", _raise)

    assert telegram.notify("hello") is False


def test_notify_swallows_exception_from_load_yaml_config(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))

    def _raise(name):
        raise OSError("transient config/base.yaml read failure")

    monkeypatch.setattr(telegram, "load_yaml_config", _raise)

    assert telegram.notify("hello") is False


def test_notify_swallows_attribute_error_when_yaml_config_root_is_not_a_dict(monkeypatch):
    # A malformed config/base.yaml (e.g. root is a list/string, not a dict)
    # makes cfg.get(...) raise AttributeError -- must still be swallowed.
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: ["not", "a", "dict"])

    assert telegram.notify("hello") is False


def test_notify_disabled_short_circuits_even_with_real_valid_looking_env_credentials(
    monkeypatch,
):
    # Uses the REAL load_telegram_credentials (not mocked away) with
    # valid-looking env vars present -- only notifications.enabled=False is
    # mocked. Proves the enabled=False short-circuit holds end-to-end
    # through real credential loading, not merely when
    # load_telegram_credentials itself is stubbed out.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "real-looking-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "real-looking-chat")
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify("hello")

    assert result is False
    assert called["count"] == 0


# --- send_message() ---------------------------------------------------------
# Command replies on the inbound control channel must go through even when
# notifications.enabled is False -- that toggle only governs best-effort
# outbound notify(), not replies to a command the user just sent.


def test_send_message_posts_correct_url_and_payload(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_message("hello world", timeout_sec=2.5)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 2.5
    body = captured["data"].decode("utf-8")
    assert "chat_id=CHAT456" in body
    assert "text=hello" in body


def test_send_message_reply_markup_is_json_encoded_in_the_request_body(monkeypatch, configured):
    captured = {}
    keyboard = {"inline_keyboard": [[{"text": "Positions", "callback_data": "cmd:positions"}]]}

    def fake_urlopen(request, timeout):
        captured["data"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_message("hello world", reply_markup=keyboard)

    assert result is True
    body = urllib.parse.parse_qs(captured["data"].decode("utf-8"))
    assert json.loads(body["reply_markup"][0]) == keyboard


def test_send_message_without_reply_markup_omits_the_field_entirely(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["data"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    telegram.send_message("hello world")

    assert b"reply_markup" not in captured["data"]


def test_send_message_sends_even_when_notifications_disabled(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(request, timeout):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_message("hello")

    assert result is True
    assert called["count"] == 1


def test_notify_still_respects_notifications_disabled_unlike_send_message(monkeypatch):
    # send_message()'s special-case above must not have accidentally
    # loosened notify()'s own behavior -- proven side-by-side here.
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(request, timeout):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.notify("hello") is False
    assert called["count"] == 0


def test_send_message_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_message("hello")

    assert result is False
    assert called["count"] == 0


def test_send_message_swallows_network_exception_and_returns_false(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.send_message("hello") is False


def test_send_message_failure_log_never_contains_raw_token(monkeypatch, caplog):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.send_message("some message body")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text


# --- notify_photo() / send_photo() -------------------------------------------
# Chart-image counterparts to notify()/send_message() -- same
# gate/no-op/never-raises contracts, but posting a multipart/form-data body
# (binary PNG upload) to sendPhoto instead of urlencoded text to sendMessage.


def test_notify_photo_posts_correct_url_and_multipart_body(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["content_type"] = request.get_header("Content-type")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify_photo(b"\x89PNGDATA", caption="Equity curve", timeout_sec=3.0)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/sendPhoto"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 3.0
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    body = captured["data"]
    assert b'name="chat_id"' in body
    assert b"CHAT456" in body
    assert b'name="caption"' in body
    assert b"Equity curve" in body
    assert b'name="photo"; filename="chart.png"' in body
    assert b"Content-Type: image/png" in body
    assert b"\x89PNGDATA" in body


def test_notify_photo_without_caption_omits_caption_field(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["data"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    telegram.notify_photo(b"\x89PNGDATA")

    assert b'name="caption"' not in captured["data"]


def test_notify_photo_swallows_network_exception_and_returns_false(monkeypatch, configured):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.notify_photo(b"\x89PNGDATA") is False


def test_notify_photo_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify_photo(b"\x89PNGDATA")

    assert result is False
    assert called["count"] == 0


def test_notify_photo_makes_zero_http_calls_when_notifications_disabled(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.notify_photo(b"\x89PNGDATA")

    assert result is False
    assert called["count"] == 0


def test_notify_photo_failure_log_never_contains_raw_token(monkeypatch, configured, caplog):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.notify_photo(b"\x89PNGDATA")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text


def test_send_photo_posts_correct_url_and_multipart_body(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_photo(b"\x89PNGDATA", caption="Daily net P/L", timeout_sec=5.0)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/sendPhoto"
    assert captured["timeout"] == 5.0
    assert b"Daily net P/L" in captured["data"]
    assert b"\x89PNGDATA" in captured["data"]


def test_send_photo_sends_even_when_notifications_disabled(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))
    monkeypatch.setattr(telegram, "load_yaml_config", lambda name: {"notifications": {"enabled": False}})
    called = {"count": 0}

    def fake_urlopen(request, timeout):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_photo(b"\x89PNGDATA")

    assert result is True
    assert called["count"] == 1


def test_send_photo_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.send_photo(b"\x89PNGDATA")

    assert result is False
    assert called["count"] == 0


def test_send_photo_swallows_network_exception_and_returns_false(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.send_photo(b"\x89PNGDATA") is False


def test_send_photo_failure_log_never_contains_raw_token(monkeypatch, caplog):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: ("TOKEN123", "CHAT456"))

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.send_photo(b"\x89PNGDATA")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text


# --- answer_callback_query() --------------------------------------------------
# Clears a tapped inline-keyboard button's loading spinner. Same never-raises/
# returns-bool contract and never-log-the-token discipline as every other
# function in this module.


def test_answer_callback_query_posts_correct_url_and_payload_with_explicit_bot_token(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.answer_callback_query("cbq-123", bot_token="TOKEN123", timeout_sec=2.0)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/answerCallbackQuery"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 2.0
    body = captured["data"].decode("utf-8")
    assert "callback_query_id=cbq-123" in body


def test_answer_callback_query_falls_back_to_loaded_credentials_when_bot_token_not_given(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.answer_callback_query("cbq-123")

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/answerCallbackQuery"


def test_answer_callback_query_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.answer_callback_query("cbq-123")

    assert result is False
    assert called["count"] == 0


def test_answer_callback_query_swallows_network_exception_and_returns_false(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.answer_callback_query("cbq-123", bot_token="TOKEN123") is False


def test_answer_callback_query_failure_log_never_contains_raw_token(monkeypatch, caplog):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.answer_callback_query("cbq-123", bot_token="TOKEN123")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text


# --- set_my_commands() --------------------------------------------------------
# Registers Telegram's persistent bottom-of-keyboard command menu. Same
# never-raises/returns-bool contract and never-log-the-token discipline as
# every other function in this module.


def test_set_my_commands_posts_correct_url_and_json_encoded_payload(monkeypatch):
    captured = {}
    commands = [("status", "Report loop state"), ("positions", "Currently open positions")]

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.set_my_commands(commands, bot_token="TOKEN123", timeout_sec=3.0)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/setMyCommands"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 3.0
    body = urllib.parse.parse_qs(captured["data"].decode("utf-8"))
    payload = json.loads(body["commands"][0])
    assert payload == [
        {"command": "status", "description": "Report loop state"},
        {"command": "positions", "description": "Currently open positions"},
    ]


def test_set_my_commands_falls_back_to_loaded_credentials_when_bot_token_not_given(monkeypatch, configured):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.set_my_commands([("status", "Report loop state")])

    assert result is True
    assert captured["url"] == "https://api.telegram.org/botTOKEN123/setMyCommands"


def test_set_my_commands_makes_zero_http_calls_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(telegram, "load_telegram_credentials", lambda: None)
    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    result = telegram.set_my_commands([("status", "Report loop state")])

    assert result is False
    assert called["count"] == 0


def test_set_my_commands_swallows_network_exception_and_returns_false(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert telegram.set_my_commands([("status", "Report loop state")], bot_token="TOKEN123") is False


def test_set_my_commands_failure_log_never_contains_raw_token(monkeypatch, caplog):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING):
        result = telegram.set_my_commands([("status", "Report loop state")], bot_token="TOKEN123")

    assert result is False
    assert "TOKEN123" not in caplog.text
    assert "botTOKEN123" not in caplog.text
