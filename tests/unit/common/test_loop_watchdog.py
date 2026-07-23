"""Unit tests for common/loop_watchdog.py -- the external shadow-loop
process-liveness watchdog (2026-07-23 incident: the loop died silently and
sat down for ~2 hours with zero alert)."""
from __future__ import annotations

from autotrade.common import loop_watchdog as loop_watchdog_module
from autotrade.common import pid_file as pid_file_module
from autotrade.common.loop_watchdog import check_loop_alive


def _stub_running(monkeypatch, value: bool):
    monkeypatch.setattr(pid_file_module, "is_running", lambda pid_path=None: value)


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(loop_watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def test_first_check_running_records_baseline_silently(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, True)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path)

    assert result is True
    assert calls == []


def test_first_check_down_notifies_immediately(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path)

    assert result is False
    assert len(calls) == 1
    assert "DOWN" in calls[0]


def test_running_to_down_transition_notifies(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_running(monkeypatch, True)
    check_loop_alive(state_path=state_path)  # baseline, no notify
    _stub_running(monkeypatch, False)
    check_loop_alive(state_path=state_path)

    assert len(calls) == 1
    assert "DOWN" in calls[0]


def test_down_to_running_transition_notifies_recovery(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_running(monkeypatch, False)
    check_loop_alive(state_path=state_path)  # first check, already down -- notifies once
    _stub_running(monkeypatch, True)
    check_loop_alive(state_path=state_path)  # recovered -- notifies again

    assert len(calls) == 2
    assert "DOWN" in calls[0]
    assert "back up" in calls[1] or "✅" in calls[1]


def test_running_to_running_unchanged_does_not_notify(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"
    _stub_running(monkeypatch, True)

    check_loop_alive(state_path=state_path)
    check_loop_alive(state_path=state_path)

    assert calls == []


def test_down_to_down_unchanged_does_not_re_notify(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"
    _stub_running(monkeypatch, False)

    check_loop_alive(state_path=state_path)  # first check -- notifies once
    calls.clear()
    check_loop_alive(state_path=state_path)  # still down -- no further notification

    assert calls == []


def test_state_persists_across_separate_calls_simulating_separate_process_invocations(monkeypatch, tmp_path):
    # loop_watchdog is designed to be called from either a long-running poll
    # loop or a fresh CLI invocation each time -- state must survive via the
    # file, not rely on any in-memory object surviving between calls (there
    # is none here; each check_loop_alive() call is already independent).
    state_path = tmp_path / "state.json"
    calls_first = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, True)
    check_loop_alive(state_path=state_path)
    assert calls_first == []

    calls_second = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    check_loop_alive(state_path=state_path)

    assert len(calls_second) == 1
    assert "DOWN" in calls_second[0]


def test_corrupt_state_file_is_treated_as_no_prior_state_not_a_crash(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    _stub_running(monkeypatch, False)

    result = check_loop_alive(state_path=state_path)

    assert result is False
    assert len(calls) == 1  # treated as a fresh first-check-down


def test_missing_state_file_parent_directory_is_created(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _stub_running(monkeypatch, True)
    state_path = tmp_path / "nested" / "does_not_exist_yet" / "state.json"

    check_loop_alive(state_path=state_path)

    assert state_path.exists()
