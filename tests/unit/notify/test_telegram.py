"""Unit tests for notify/telegram.py -- urllib.request.urlopen is mocked, no
real Telegram credentials/network needed. `notify()` must never raise and
must be a no-op (zero HTTP calls) when not configured or disabled."""
from __future__ import annotations

import logging
import socket
import urllib.error

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
