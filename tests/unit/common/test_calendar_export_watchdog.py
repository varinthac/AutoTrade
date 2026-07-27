"""Unit tests for common/calendar_export_watchdog.py -- the self-heal
recovery for a silently-dead NewsCalendarExporter MQL5 Service (2026-07-28
incident, see that module's own docstring)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from autotrade.common import calendar_export_watchdog as watchdog_module
from autotrade.common import pid_file as pid_file_module
from autotrade.common import stop_request_flag as stop_request_flag_module
from autotrade.common.calendar_export_watchdog import check_and_recover


def _touch(path, age_minutes: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("dummy", encoding="utf-8")
    if age_minutes:
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).timestamp()
        os.utime(path, (stale_time, stale_time))


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def _stub_manual_halt(monkeypatch, value: bool):
    monkeypatch.setattr(watchdog_module.manual_halt_flag, "is_active", lambda flag_path=None: value)


def _capture_stop_request(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(stop_request_flag_module, "request", lambda reason, flag_path=None: calls.append(reason))
    return calls


def _stub_loop_stopped_immediately(monkeypatch):
    monkeypatch.setattr(pid_file_module, "is_running", lambda pid_path=None: False)
    monkeypatch.setattr(watchdog_module.time, "sleep", lambda s: None)


def _capture_taskkill(monkeypatch, returncode: int = 0):
    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self):
            self.returncode = returncode
            self.stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return _FakeResult()

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)
    return calls


def test_fresh_export_does_nothing(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    stop_calls = _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    export_path = tmp_path / "AutoTradeNewsCalendar.csv"
    _touch(export_path, age_minutes=2)

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is False
    assert notify_calls == []
    assert stop_calls == []
    assert kill_calls == []


def test_missing_export_triggers_recovery(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    stop_calls = _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_loop_stopped_immediately(monkeypatch)
    export_path = tmp_path / "does_not_exist.csv"

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is True
    assert len(notify_calls) == 1
    assert len(stop_calls) == 1
    assert len(kill_calls) == 1
    assert kill_calls[0][:2] == ["taskkill", "/IM"]


def test_stale_export_beyond_threshold_triggers_recovery(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_loop_stopped_immediately(monkeypatch)
    export_path = tmp_path / "AutoTradeNewsCalendar.csv"
    _touch(export_path, age_minutes=25)

    result = check_and_recover(
        export_path=export_path, state_path=tmp_path / "state.json", staleness_threshold_minutes=20,
    )

    assert result is True
    assert len(notify_calls) == 1
    assert len(kill_calls) == 1


def test_stale_within_cooldown_skips_second_recovery_attempt(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_loop_stopped_immediately(monkeypatch)
    export_path = tmp_path / "AutoTradeNewsCalendar.csv"
    _touch(export_path, age_minutes=25)
    state_path = tmp_path / "state.json"

    first = check_and_recover(
        export_path=export_path, state_path=state_path, staleness_threshold_minutes=20, cooldown_minutes=30,
    )
    notify_calls.clear()
    kill_calls.clear()
    second = check_and_recover(
        export_path=export_path, state_path=state_path, staleness_threshold_minutes=20, cooldown_minutes=30,
    )

    assert first is True
    assert second is False
    assert notify_calls == []
    assert kill_calls == []


def test_stale_after_cooldown_elapsed_retries(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_loop_stopped_immediately(monkeypatch)
    export_path = tmp_path / "AutoTradeNewsCalendar.csv"
    _touch(export_path, age_minutes=25)
    state_path = tmp_path / "state.json"
    old_attempt = datetime.now(timezone.utc) - timedelta(minutes=31)
    state_path.write_text(json.dumps({"last_restart_attempt": old_attempt.isoformat()}), encoding="utf-8")

    result = check_and_recover(
        export_path=export_path, state_path=state_path, staleness_threshold_minutes=20, cooldown_minutes=30,
    )

    assert result is True
    assert len(kill_calls) == 1


def test_wait_for_loop_stop_returns_as_soon_as_loop_confirms_stopped(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    _capture_taskkill(monkeypatch)
    sleep_calls: list[float] = []
    monkeypatch.setattr(watchdog_module.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(pid_file_module, "is_running", lambda pid_path=None: False)
    export_path = tmp_path / "missing.csv"

    check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert sleep_calls == []


def test_wait_for_loop_stop_times_out_and_still_kills_terminal(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    monkeypatch.setattr(pid_file_module, "is_running", lambda pid_path=None: True)  # never confirms stopped
    monkeypatch.setattr(watchdog_module.time, "sleep", lambda s: None)

    fake_clock = {"t": 0.0}

    def fake_monotonic():
        fake_clock["t"] += 1000.0  # jump straight past any timeout on first check
        return fake_clock["t"]

    monkeypatch.setattr(watchdog_module.time, "monotonic", fake_monotonic)
    export_path = tmp_path / "missing.csv"

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is True
    assert len(kill_calls) == 1  # terminal still gets killed even though the loop never confirmed stopped


def test_check_and_recover_never_raises_on_internal_error(monkeypatch, tmp_path):
    def _boom(text):
        raise RuntimeError("notify is down too")

    monkeypatch.setattr(watchdog_module, "notify", _boom)
    export_path = tmp_path / "missing.csv"

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is False


# --- manual_halt_flag interaction (2026-07-28 code review finding) ---------


def test_manual_halt_active_skips_recovery_even_when_export_stale(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    stop_calls = _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_manual_halt(monkeypatch, True)
    export_path = tmp_path / "missing.csv"

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is False
    assert notify_calls == []
    assert stop_calls == []
    assert kill_calls == []


def test_manual_halt_inactive_recovers_normally(monkeypatch, tmp_path):
    # Regression guard: once `start` clears manual_halt_flag, normal
    # recovery behavior must resume exactly as before.
    notify_calls = _capture_notify(monkeypatch)
    _capture_stop_request(monkeypatch)
    kill_calls = _capture_taskkill(monkeypatch)
    _stub_loop_stopped_immediately(monkeypatch)
    _stub_manual_halt(monkeypatch, False)
    export_path = tmp_path / "missing.csv"

    result = check_and_recover(export_path=export_path, state_path=tmp_path / "state.json")

    assert result is True
    assert len(notify_calls) == 1
    assert len(kill_calls) == 1


def test_default_export_path_uses_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\Someone\AppData\Roaming")

    path = watchdog_module.default_export_path()

    assert str(path) == r"C:\Users\Someone\AppData\Roaming\MetaQuotes\Terminal\Common\Files\AutoTradeNewsCalendar.csv"


def test_default_export_path_raises_without_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)

    try:
        watchdog_module.default_export_path()
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
