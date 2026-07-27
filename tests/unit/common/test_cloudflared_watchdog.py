"""Unit tests for common/cloudflared_watchdog.py -- liveness + auto-restart
for the cloudflared tunnel process (2026-07-28 incident, Cloudflare error
1033, see that module's own docstring)."""
from __future__ import annotations

from autotrade.common import cloudflared_watchdog as watchdog_module
from autotrade.common.cloudflared_watchdog import check_and_restart


def _stub_running(monkeypatch, value: bool):
    monkeypatch.setattr(watchdog_module, "is_running", lambda: value)


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def _capture_schtasks(monkeypatch, returncode: int = 0):
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


def test_first_check_running_records_baseline_silently_and_does_not_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    schtasks_calls = _capture_schtasks(monkeypatch)
    _stub_running(monkeypatch, True)

    result = check_and_restart(state_path=tmp_path / "state.json")

    assert result is True
    assert notify_calls == []
    assert schtasks_calls == []


def test_first_check_down_notifies_and_attempts_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    schtasks_calls = _capture_schtasks(monkeypatch)
    _stub_running(monkeypatch, False)

    result = check_and_restart(state_path=tmp_path / "state.json")

    assert result is False
    assert len(notify_calls) == 1
    assert "DOWN" in notify_calls[0]
    assert len(schtasks_calls) == 1
    assert schtasks_calls[0] == ["schtasks", "/Run", "/TN", "AutoTrade Cloudflared"]


def test_down_to_down_unchanged_does_not_re_notify_but_keeps_retrying_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    schtasks_calls = _capture_schtasks(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    check_and_restart(state_path=state_path)
    notify_calls.clear()
    check_and_restart(state_path=state_path)

    assert notify_calls == []
    assert len(schtasks_calls) == 2


def test_down_to_running_transition_notifies_recovery_and_stops_restarting(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    schtasks_calls = _capture_schtasks(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_running(monkeypatch, False)
    check_and_restart(state_path=state_path)
    _stub_running(monkeypatch, True)
    check_and_restart(state_path=state_path)

    assert len(notify_calls) == 2
    assert "DOWN" in notify_calls[0]
    assert "back up" in notify_calls[1] or "✅" in notify_calls[1]
    assert len(schtasks_calls) == 1


def test_corrupt_state_file_is_treated_as_no_prior_state_not_a_crash(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    _capture_schtasks(monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    _stub_running(monkeypatch, False)

    result = check_and_restart(state_path=state_path)

    assert result is False
    assert len(notify_calls) == 1


def test_restart_failure_is_logged_not_raised(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)

    def _raise(args, **kwargs):
        raise OSError("schtasks.exe not found")

    monkeypatch.setattr(watchdog_module.subprocess, "run", _raise)

    result = check_and_restart(state_path=tmp_path / "state.json")

    assert result is False


def test_unexpected_exception_in_check_is_swallowed_not_raised(monkeypatch, tmp_path):
    # 2026-07-28 audit finding: a failure here must never abort whichever
    # OTHER checks run_health_check.py runs after it in the same cycle.
    def _raise():
        raise RuntimeError("simulated tasklist failure")

    monkeypatch.setattr(watchdog_module, "is_running", _raise)

    result = check_and_restart(state_path=tmp_path / "state.json")

    assert result is False


def test_is_running_matches_process_image_name(monkeypatch):
    class _FakeResult:
        stdout = "cloudflared.exe             1200 Console                    1     45,000 K\r\n"

    monkeypatch.setattr(
        watchdog_module.subprocess, "run", lambda args, **kwargs: _FakeResult(),
    )

    assert watchdog_module.is_running() is True


def test_is_running_false_when_tasklist_empty(monkeypatch):
    class _FakeResult:
        stdout = "INFO: No tasks are running which match the specified criteria.\r\n"

    monkeypatch.setattr(
        watchdog_module.subprocess, "run", lambda args, **kwargs: _FakeResult(),
    )

    assert watchdog_module.is_running() is False
