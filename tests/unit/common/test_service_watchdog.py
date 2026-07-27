"""Unit tests for common/service_watchdog.py -- the generic detached-service
liveness watchdog + auto-restart shared by scripts/run_health_check.py for
the dashboard and Telegram control listener (2026-07-24, once each gained
its own PID file). Same transition-only-alert / always-retry-while-down
shape as tests/unit/common/test_loop_watchdog.py, generalized."""
from __future__ import annotations

from autotrade.common import pid_file as pid_file_module
from autotrade.common import service_watchdog as service_watchdog_module
from autotrade.common.service_watchdog import check_and_restart


def _stub_running(monkeypatch, value: bool):
    monkeypatch.setattr(pid_file_module, "is_running", lambda pid_path=None: value)


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(service_watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def _capture_popen(monkeypatch):
    calls: list[tuple] = []

    class _FakeProcess:
        pass

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(service_watchdog_module.subprocess, "Popen", fake_popen)
    return calls


def test_first_check_running_records_baseline_silently_and_does_not_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    popen_calls = _capture_popen(monkeypatch)
    _stub_running(monkeypatch, True)

    result = check_and_restart(
        "Dashboard", tmp_path / "d.pid", tmp_path / "state.json", tmp_path / "run_dashboard.py", tmp_path,
    )

    assert result is True
    assert notify_calls == []
    assert popen_calls == []


def test_first_check_down_notifies_and_attempts_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    popen_calls = _capture_popen(monkeypatch)
    _stub_running(monkeypatch, False)
    script = tmp_path / "run_dashboard.py"

    result = check_and_restart("Dashboard", tmp_path / "d.pid", tmp_path / "state.json", script, tmp_path)

    assert result is False
    assert len(notify_calls) == 1
    assert "DOWN" in notify_calls[0]
    assert "Dashboard" in notify_calls[0]
    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert str(script) in args
    assert kwargs["cwd"] == str(tmp_path)


def test_down_to_down_unchanged_does_not_re_notify_but_keeps_retrying_restart(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    popen_calls = _capture_popen(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"
    script = tmp_path / "run_dashboard.py"

    check_and_restart("Dashboard", tmp_path / "d.pid", state_path, script, tmp_path)  # first check
    notify_calls.clear()
    check_and_restart("Dashboard", tmp_path / "d.pid", state_path, script, tmp_path)  # still down

    assert notify_calls == []
    assert len(popen_calls) == 2  # retried the restart both times


def test_down_to_running_transition_notifies_recovery_and_stops_restarting(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    popen_calls = _capture_popen(monkeypatch)
    state_path = tmp_path / "state.json"
    script = tmp_path / "run_dashboard.py"

    _stub_running(monkeypatch, False)
    check_and_restart("Dashboard", tmp_path / "d.pid", state_path, script, tmp_path)
    _stub_running(monkeypatch, True)
    check_and_restart("Dashboard", tmp_path / "d.pid", state_path, script, tmp_path)

    assert len(notify_calls) == 2
    assert "DOWN" in notify_calls[0]
    assert "back up" in notify_calls[1] or "✅" in notify_calls[1]
    assert len(popen_calls) == 1  # only the first (down) check restarted


def test_restart_passes_extra_args_through(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    popen_calls = _capture_popen(monkeypatch)
    _stub_running(monkeypatch, False)
    script = tmp_path / "run_shadow_loop.py"

    check_and_restart(
        "Shadow loop", tmp_path / "s.pid", tmp_path / "state.json", script, tmp_path,
        extra_args=["--adapter", "demo"],
    )

    args, _kwargs = popen_calls[0]
    assert "--adapter" in args and "demo" in args


def test_restart_falls_back_without_breakaway_flag_if_caller_job_disallows_it(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    calls = []

    class _FakeProcess:
        pass

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("creationflags", 0) & service_watchdog_module.subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise OSError("Access is denied")
        return _FakeProcess()

    monkeypatch.setattr(service_watchdog_module.subprocess, "Popen", fake_popen)

    result = check_and_restart(
        "Dashboard", tmp_path / "d.pid", tmp_path / "state.json", tmp_path / "run_dashboard.py", tmp_path,
    )

    assert result is False
    assert len(calls) == 2
    assert calls[0]["creationflags"] & service_watchdog_module.subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert calls[1]["creationflags"] == service_watchdog_module.subprocess.CREATE_NEW_CONSOLE


def test_restart_exception_is_logged_not_raised(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)

    def _raise(args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(service_watchdog_module.subprocess, "Popen", _raise)

    result = check_and_restart(
        "Dashboard", tmp_path / "d.pid", tmp_path / "state.json", tmp_path / "run_dashboard.py", tmp_path,
    )

    assert result is False


def test_unexpected_exception_in_check_is_swallowed_not_raised(monkeypatch, tmp_path):
    # 2026-07-28 audit finding: a failure checking/restarting ONE service
    # must never abort whichever OTHER services run_health_check.py checks
    # after it in the same cycle.
    def _raise(pid_path=None):
        raise RuntimeError("simulated tasklist failure")

    monkeypatch.setattr(pid_file_module, "is_running", _raise)

    result = check_and_restart(
        "Dashboard", tmp_path / "d.pid", tmp_path / "state.json", tmp_path / "run_dashboard.py", tmp_path,
    )

    assert result is False


def test_corrupt_state_file_is_treated_as_no_prior_state_not_a_crash(monkeypatch, tmp_path):
    notify_calls = _capture_notify(monkeypatch)
    _capture_popen(monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    _stub_running(monkeypatch, False)

    result = check_and_restart(
        "Dashboard", tmp_path / "d.pid", state_path, tmp_path / "run_dashboard.py", tmp_path,
    )

    assert result is False
    assert len(notify_calls) == 1
