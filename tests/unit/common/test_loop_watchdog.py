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


def _stub_manual_halt(monkeypatch, value: bool):
    monkeypatch.setattr(loop_watchdog_module.manual_halt_flag, "is_active", lambda flag_path=None: value)


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


def _capture_subprocess_run(monkeypatch, returncode: int = 0):
    calls: list[list[str]] = []

    class _Result:
        def __init__(self):
            self.returncode = returncode
            self.stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(loop_watchdog_module.subprocess, "run", _fake_run)
    return calls


def test_auto_restart_false_by_default_never_attempts_restart(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    check_loop_alive(state_path=state_path)

    assert calls == []


def test_auto_restart_true_and_down_attempts_restart(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    check_loop_alive(state_path=state_path, auto_restart=True)

    assert len(calls) == 1
    assert "autotrade_control.py" in calls[0][1]
    assert calls[0][2] == "start"


def test_auto_restart_true_and_running_does_not_attempt_restart(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, True)
    state_path = tmp_path / "state.json"

    check_loop_alive(state_path=state_path, auto_restart=True)

    assert calls == []


def test_auto_restart_retries_on_every_still_down_check_not_just_transition(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    check_loop_alive(state_path=state_path, auto_restart=True)  # first check, already down
    check_loop_alive(state_path=state_path, auto_restart=True)  # still down -- no transition

    assert len(calls) == 2


def test_auto_restart_nonzero_exit_is_logged_not_raised(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _capture_subprocess_run(monkeypatch, returncode=1)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path, auto_restart=True)

    assert result is False


def test_auto_restart_subprocess_exception_is_swallowed_not_raised(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    state_path = tmp_path / "state.json"

    def _raise(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(loop_watchdog_module.subprocess, "run", _raise)

    result = check_loop_alive(state_path=state_path, auto_restart=True)

    assert result is False


# --- manual_halt_flag interaction (2026-07-28 audit finding) ---------------


def test_manual_halt_active_suppresses_down_alert_on_first_check(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, True)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path)

    assert result is False
    assert calls == []


def test_manual_halt_active_suppresses_transition_alert(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_running(monkeypatch, True)
    _stub_manual_halt(monkeypatch, False)
    check_loop_alive(state_path=state_path)  # baseline running, halt not active yet

    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, True)  # do_stop() just ran
    check_loop_alive(state_path=state_path)

    assert calls == []


def test_manual_halt_active_does_not_attempt_restart(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, True)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path, auto_restart=True)

    assert result is False
    assert calls == []  # the exact bug this fixes: no auto-restart of a deliberate stop


def test_manual_halt_active_does_not_repeatedly_alert_across_cycles(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, True)
    state_path = tmp_path / "state.json"

    check_loop_alive(state_path=state_path)
    check_loop_alive(state_path=state_path)
    check_loop_alive(state_path=state_path)

    assert calls == []


def test_manual_halt_inactive_restores_normal_down_alert_and_restart(monkeypatch, tmp_path):
    # Regression guard: once `start` clears the flag, normal behavior (alert
    # + auto-restart on a genuinely-down loop) must resume exactly as before.
    calls = _capture_notify(monkeypatch)
    restart_calls = _capture_subprocess_run(monkeypatch)
    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, False)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path, auto_restart=True)

    assert result is False
    assert len(calls) == 1
    assert "DOWN" in calls[0]
    assert len(restart_calls) == 1


def test_manual_halt_active_but_loop_somehow_running_does_not_suppress_up_alert(monkeypatch, tmp_path):
    # Edge case: manual_halt_flag active only ever gates the DOWN path (see
    # its own docstring -- do_start() always clears it before launching, so
    # this combination shouldn't arise in practice, but the gate itself is
    # only conditioned on `not currently_running`, confirmed here).
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_running(monkeypatch, False)
    _stub_manual_halt(monkeypatch, True)
    check_loop_alive(state_path=state_path)  # baseline, down, halted -- silent
    calls.clear()

    _stub_running(monkeypatch, True)
    check_loop_alive(state_path=state_path)  # somehow running again

    assert len(calls) == 1
    assert "back up" in calls[0] or "✅" in calls[0]


def test_unexpected_exception_in_check_is_swallowed_not_raised(monkeypatch, tmp_path):
    # 2026-07-28 audit finding: this check running BEFORE others in
    # run_health_check.py's sequence must never abort the checks after it.
    def _raise(pid_path=None):
        raise RuntimeError("simulated tasklist failure")

    monkeypatch.setattr(pid_file_module, "is_running", _raise)
    state_path = tmp_path / "state.json"

    result = check_loop_alive(state_path=state_path)

    assert result is False
