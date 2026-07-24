"""Unit tests for scripts/run_telegram_control.py -- the Telegram inbound
control listener's thin network/CLI boundary. `urllib.request.urlopen` is
never invoked directly in these tests; `poll_fn`/`send_fn`/`sleep_fn` are
always mocked (same "no live terminal/network needed" pattern as
tests/unit/test_run_shadow_loop.py). scripts/ has no __init__.py, so the
script is loaded directly via importlib."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autotrade.notify import telegram_control

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_telegram_control.py"
_spec = importlib.util.spec_from_file_location("run_telegram_control_script", SCRIPT_PATH)
run_telegram_control = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_telegram_control
_spec.loader.exec_module(run_telegram_control)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _update(update_id: int, chat_id=12345, text: str = "/status") -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# --- main() ------------------------------------------------------------------


def test_main_returns_1_and_prints_error_when_credentials_missing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_telegram_control.py"])
    monkeypatch.setattr(run_telegram_control, "load_telegram_credentials", lambda: None)
    poll_calls = []
    monkeypatch.setattr(run_telegram_control, "_get_updates", lambda *a, **kw: poll_calls.append(a) or [])

    exit_code = run_telegram_control.main()

    assert exit_code == 1
    assert poll_calls == []
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_main_polls_when_credentials_present(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_telegram_control.py", "--max-iterations", "0"])
    monkeypatch.setattr(run_telegram_control, "load_telegram_credentials", lambda: ("TOKEN", "12345"))
    poll_calls = []
    monkeypatch.setattr(run_telegram_control, "_get_updates", lambda token, offset, timeout_sec: poll_calls.append((token, offset, timeout_sec)) or [])

    exit_code = run_telegram_control.main()

    assert exit_code == 0
    assert poll_calls == [("TOKEN", 0, 0)]  # startup backlog-discard call only, 0 loop iterations


# --- run_poll_loop(): startup backlog discard --------------------------------


def test_startup_backlog_is_discarded_and_offset_advances_past_it(monkeypatch):
    poll_calls = []
    backlog_updates = [_update(5, text="/start"), _update(6, text="/start")]

    def fake_poll(token, offset, timeout_sec):
        poll_calls.append((token, offset, timeout_sec))
        if timeout_sec == 0:
            return [u for u in backlog_updates if u["update_id"] >= offset]
        return []

    dispatched = []
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: dispatched.append(1))

    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
    )

    assert dispatched == []  # backlog updates never dispatched
    assert poll_calls[0] == ("TOKEN", 0, 0)  # discard page 1
    assert poll_calls[1] == ("TOKEN", 7, 0)  # discard page 2 -- confirms the page is empty, draining stops
    assert poll_calls[2] == ("TOKEN", 7, run_telegram_control._POLL_TIMEOUT_SEC)  # offset past update_id 6


def test_startup_backlog_discard_drains_multiple_pages(monkeypatch):
    # Simulates a backlog bigger than Telegram's ~100-per-call getUpdates cap
    # -- draining must keep paging until a page comes back empty, not stop
    # after the first call.
    page_1 = [_update(i, text="/start") for i in range(1, 101)]
    page_2 = [_update(i, text="/start") for i in range(101, 121)]
    poll_calls = []

    def fake_poll(token, offset, timeout_sec):
        poll_calls.append((token, offset, timeout_sec))
        if timeout_sec != 0:
            return []
        if offset <= 1:
            return page_1
        if offset <= 101:
            return page_2
        return []

    dispatched = []
    monkeypatch.setattr(telegram_control.gui_control, "start_bot", lambda: dispatched.append(1))

    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=0,
    )

    assert dispatched == []
    discard_offsets = [call[1] for call in poll_calls if call[2] == 0]
    assert discard_offsets == [0, 101, 121]  # 3 pages drained: 100 + 20 + the terminating empty page


def test_fresh_update_after_backlog_skip_produces_a_sent_reply(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: f"STATUS: {report}")

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            return []
        return [_update(10, text="/status")]

    sent = []
    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: sent.append(text),
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
    )

    assert sent == ["STATUS: REPORT"]


# --- run_poll_loop(): photo-carrying replies (/daily's charts) ---------------


def test_reply_with_photos_sends_text_then_each_photo_via_send_photo_fn(monkeypatch):
    photo1 = telegram_control.ChartPhoto(png=b"PNG-EQUITY", caption="Equity curve")
    photo2 = telegram_control.ChartPhoto(png=b"PNG-DAILY", caption="Daily net P/L")
    reply = telegram_control.ControlReply(text="Daily report text", photos=[photo1, photo2])
    monkeypatch.setattr(run_telegram_control, "handle_update", lambda *a, **kw: reply)

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            return []
        return [_update(10, text="/daily")]

    sent_text = []
    sent_photos = []
    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: sent_text.append(text),
        send_photo_fn=lambda png, caption=None: sent_photos.append((png, caption)),
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
    )

    assert sent_text == ["Daily report text"]
    assert sent_photos == [(b"PNG-EQUITY", "Equity curve"), (b"PNG-DAILY", "Daily net P/L")]


def test_reply_with_no_photos_never_calls_send_photo_fn(monkeypatch):
    monkeypatch.setattr(telegram_control.gui_control, "build_status", lambda: "REPORT")
    monkeypatch.setattr(telegram_control.gui_control, "format_status", lambda report: f"STATUS: {report}")

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            return []
        return [_update(10, text="/status")]

    sent_photos = []
    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        send_photo_fn=lambda png, caption=None: sent_photos.append((png, caption)),
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
    )

    assert sent_photos == []


def test_default_send_photo_fn_is_telegram_send_photo_when_not_provided(monkeypatch):
    photo = telegram_control.ChartPhoto(png=b"PNG-DEFAULT", caption="cap")
    reply = telegram_control.ControlReply(text="ok", photos=[photo])
    monkeypatch.setattr(run_telegram_control, "handle_update", lambda *a, **kw: reply)
    calls = []
    monkeypatch.setattr(
        run_telegram_control.telegram, "send_photo",
        lambda png, caption=None: calls.append((png, caption)) or True,
    )

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            return []
        return [_update(10, text="/daily")]

    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
    )

    assert calls == [(b"PNG-DEFAULT", "cap")]


# --- run_poll_loop(): backlog-discard exception handling ---------------------


def test_backlog_discard_retries_on_transient_exception_then_succeeds(monkeypatch):
    call_count = {"n": 0}

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec != 0:
            return []
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient network failure")
        return []

    sleep_calls = []

    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: sleep_calls.append(s), clock=FixedClock(NOW), max_iterations=0,
    )

    assert call_count["n"] == 2  # first attempt failed, retry succeeded
    assert len(sleep_calls) == 1


def test_backlog_discard_gives_up_after_max_retries_and_still_starts_the_main_loop(monkeypatch):
    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            raise RuntimeError("persistent network failure")
        return []

    sleep_calls = []

    # max_iterations=1 -- proves the process doesn't crash: it survives
    # backlog-discard giving up and goes on to run the main loop.
    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: sleep_calls.append(s), clock=FixedClock(NOW), max_iterations=1,
    )

    assert len(sleep_calls) == run_telegram_control._BACKLOG_DISCARD_MAX_ATTEMPTS


def test_backlog_discard_exception_message_never_logged_with_bot_token(monkeypatch, caplog):
    import logging

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            raise RuntimeError("https://api.telegram.org/botSUPERSECRETTOKEN/getUpdates failed")
        return []

    with caplog.at_level(logging.WARNING):
        run_telegram_control.run_poll_loop(
            "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
            sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
        )

    assert "SUPERSECRETTOKEN" not in caplog.text


# --- run_poll_loop(): exception handling -------------------------------------


def test_exception_from_poll_fn_is_swallowed_and_loop_continues(monkeypatch):
    call_count = {"n": 0}

    def fake_poll(token, offset, timeout_sec):
        call_count["n"] += 1
        if timeout_sec == 0:
            return []
        if call_count["n"] == 2:
            raise RuntimeError("simulated transient network failure")
        return []

    sleep_calls = []

    run_telegram_control.run_poll_loop(
        "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
        sleep_fn=lambda s: sleep_calls.append(s), clock=FixedClock(NOW), max_iterations=2,
    )

    assert call_count["n"] == 3  # discard + 2 loop iterations, despite the exception
    assert len(sleep_calls) == 1  # backoff triggered exactly once, for the failing iteration


def test_exception_message_never_logged_with_bot_token(monkeypatch, caplog):
    import logging

    def fake_poll(token, offset, timeout_sec):
        if timeout_sec == 0:
            return []
        raise RuntimeError("https://api.telegram.org/botSUPERSECRETTOKEN/getUpdates failed")

    with caplog.at_level(logging.WARNING):
        run_telegram_control.run_poll_loop(
            "TOKEN", "12345", poll_fn=fake_poll, send_fn=lambda text: None,
            sleep_fn=lambda s: None, clock=FixedClock(NOW), max_iterations=1,
        )

    assert "SUPERSECRETTOKEN" not in caplog.text
