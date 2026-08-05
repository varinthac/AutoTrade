"""Unit tests for common/scheduled_task_watchdog.py -- alert-only monitoring
for the daily Auditor report and nightly DB backup Scheduled Tasks
(2026-07-28 audit finding: neither was ever monitored before this)."""
from __future__ import annotations

from datetime import datetime, timedelta

from autotrade.common import scheduled_task_watchdog as watchdog_module
from autotrade.common.scheduled_task_watchdog import check_all


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def _fake_schtasks_output(last_result: str = "0", last_run: datetime | None = None) -> str:
    last_run = last_run or datetime.now()
    return (
        "Folder: \\\n"
        "TaskName:                             \\AutoTrade Daily Report\n"
        f"Last Run Time:                        {last_run.strftime('%m/%d/%Y %I:%M:%S %p')}\n"
        f"Last Result:                          {last_result}\n"
        "Task To Run:                           C:\\AutoTrade\\.venv\\Scripts\\python.exe ...\n"
    )


def _stub_schtasks(monkeypatch, returncode: int = 0, stdout: str = "", stderr: str = ""):
    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        calls.append(args)
        return _FakeResult()

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)
    return calls


def test_healthy_task_does_not_alert(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="0"))
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert all(results.values())
    assert calls == []


def test_sched_s_task_running_is_healthy_not_a_failure(monkeypatch, tmp_path):
    """2026-08-05 live false alarm: the heartbeat queried the Daily Report
    task while its 09:00 instance was still executing -- Windows reports
    Last Result = 267009 (0x41301, SCHED_S_TASK_RUNNING) during a run, and
    the plain non-zero check alerted on it seconds before the task finished
    with 0. In-progress must read as healthy; a genuinely HUNG run is
    covered by the task's own ExecutionTimeLimit, whose kill produces a
    real non-zero code this check still flags."""
    calls = _capture_notify(monkeypatch)
    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="267009"))
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert all(results.values())
    assert calls == []


def test_nonzero_last_result_is_unhealthy_and_alerts(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="1"))
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert not any(results.values())
    assert len(calls) == 2  # one per monitored task
    assert all("unhealthy" in c for c in calls)


def test_stale_last_run_time_is_unhealthy(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    stale_time = datetime.now() - timedelta(hours=48)
    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="0", last_run=stale_time))
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert not any(results.values())


def test_schtasks_query_failure_is_unhealthy(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_schtasks(monkeypatch, returncode=1, stderr="ERROR: The system cannot find the file specified.")
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert not any(results.values())
    assert len(calls) == 2


def test_unparseable_last_run_time_is_unhealthy_not_a_crash(monkeypatch, tmp_path):
    _capture_notify(monkeypatch)
    bad_output = (
        "Last Run Time:                        not-a-date\n"
        "Last Result:                          0\n"
    )
    _stub_schtasks(monkeypatch, stdout=bad_output)
    state_path = tmp_path / "state.json"

    results = check_all(state_path=state_path)

    assert not any(results.values())


def test_transition_only_alert_does_not_repeat_while_still_unhealthy(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="1"))
    state_path = tmp_path / "state.json"

    check_all(state_path=state_path)
    calls.clear()
    check_all(state_path=state_path)

    assert calls == []


def test_recovery_transition_alerts_healthy_again(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"

    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="1"))
    check_all(state_path=state_path)
    calls.clear()

    _stub_schtasks(monkeypatch, stdout=_fake_schtasks_output(last_result="0"))
    check_all(state_path=state_path)

    assert len(calls) == 2  # one per monitored task
    assert all("healthy again" in c for c in calls)


def test_one_task_failing_does_not_prevent_checking_the_other(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    state_path = tmp_path / "state.json"
    call_count = {"n": 0}

    def fake_run(args, **kwargs):
        call_count["n"] += 1
        task_name = args[args.index("/TN") + 1]

        class _FakeResult:
            pass

        result = _FakeResult()
        if task_name == "AutoTrade Daily Report":
            result.returncode = 1
            result.stdout = ""
            result.stderr = "boom"
        else:
            result.returncode = 0
            result.stdout = _fake_schtasks_output(last_result="0")
            result.stderr = ""
        return result

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    results = check_all(state_path=state_path)

    assert results["AutoTrade Daily Report"] is False
    assert results["AutoTrade DB Backup"] is True
    assert call_count["n"] == 2


def test_check_never_raises_on_internal_error(monkeypatch, tmp_path):
    def _boom(args, **kwargs):
        raise RuntimeError("simulated subprocess failure")

    monkeypatch.setattr(watchdog_module.subprocess, "run", _boom)

    results = check_all(state_path=tmp_path / "state.json")

    assert not any(results.values())
