"""Unit tests for scripts/autotrade_control.py -- the thin CLI wrapping the
start/stop workflow. subprocess.Popen/subprocess.run are always mocked (no
real process is ever launched, and kill_switch.py is never actually
invoked); scripts/ has no __init__.py, so the script is loaded directly via
importlib, same pattern as tests/unit/test_run_shadow_loop.py /
tests/unit/test_kill_switch_script.py."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from autotrade.common import kill_switch_flag, pid_file, stop_request_flag

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "autotrade_control.py"
_spec = importlib.util.spec_from_file_location("autotrade_control_script", SCRIPT_PATH)
autotrade_control = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = autotrade_control
_spec.loader.exec_module(autotrade_control)


@pytest.fixture
def kill_flag_path(tmp_path, monkeypatch):
    path = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(kill_switch_flag, "DEFAULT_FLAG_PATH", path)
    return path


@pytest.fixture
def stop_flag_path(tmp_path, monkeypatch):
    path = tmp_path / "stop_request.flag"
    monkeypatch.setattr(stop_request_flag, "DEFAULT_FLAG_PATH", path)
    return path


@pytest.fixture
def pid_path(tmp_path, monkeypatch):
    path = tmp_path / "shadow_loop.pid"
    monkeypatch.setattr(pid_file, "DEFAULT_PID_PATH", path)
    return path


# --- do_start() --------------------------------------------------------


def test_do_start_refuses_when_kill_switch_active(kill_flag_path, monkeypatch):
    kill_switch_flag.activate("daily loss limit breached")
    popen_calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw)))

    exit_code = autotrade_control.do_start()

    assert exit_code == 1
    assert popen_calls == []


def test_do_start_launches_run_shadow_loop_in_new_console_when_not_halted(kill_flag_path, monkeypatch):
    popen_calls = []

    class _FakeProcess:
        pass

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    exit_code = autotrade_control.do_start()

    assert exit_code == 0
    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert str(autotrade_control.RUN_SHADOW_LOOP_PATH) in args
    assert "--adapter" in args and "demo" in args
    assert kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE


# --- do_stop() -----------------------------------------------------------


def test_do_stop_requests_stop_flag_with_reason(stop_flag_path):
    exit_code = autotrade_control.do_stop()

    assert exit_code == 0
    assert stop_request_flag.is_requested() is True
    status = stop_request_flag.get_status()
    assert status["reason"] == "manual stop button"


# --- do_emergency_stop() --------------------------------------------------


def test_do_emergency_stop_without_confirm_refuses_and_does_not_shell_out(monkeypatch):
    run_calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: run_calls.append((a, kw)))

    exit_code = autotrade_control.do_emergency_stop(confirm=False)

    assert exit_code == 1
    assert run_calls == []


def test_do_emergency_stop_with_confirm_invokes_kill_switch_script(monkeypatch):
    run_calls = []

    class _FakeCompletedProcess:
        returncode = 0

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = autotrade_control.do_emergency_stop(confirm=True)

    assert exit_code == 0
    assert len(run_calls) == 1
    args = run_calls[0]
    assert str(autotrade_control.KILL_SWITCH_PATH) in args
    assert "--activate" in args


def test_do_emergency_stop_relays_nonzero_exit_code(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompletedProcess())

    assert autotrade_control.do_emergency_stop(confirm=True) == 1


# --- do_status() -----------------------------------------------------------


def test_do_status_reports_not_running_no_kill_switch_no_stop_flag(
    kill_flag_path, stop_flag_path, pid_path, capsys,
):
    exit_code = autotrade_control.do_status()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "NOT running" in out
    assert "not active" in out
    assert "not pending" in out


def test_do_status_reports_running_when_pid_alive(kill_flag_path, stop_flag_path, pid_path, monkeypatch, capsys):
    pid_file.write(1234)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 1234)

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "RUNNING" in out
    assert "1234" in out


def test_do_status_reports_not_running_when_pid_stale(kill_flag_path, stop_flag_path, pid_path, monkeypatch, capsys):
    pid_file.write(9999)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: False)

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "NOT running" in out


def test_do_status_reports_kill_switch_active(kill_flag_path, stop_flag_path, pid_path, capsys):
    kill_switch_flag.activate("manual test halt")

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "ACTIVE" in out
    assert "manual test halt" in out


def test_do_status_reports_pending_stop_flag(kill_flag_path, stop_flag_path, pid_path, capsys):
    stop_request_flag.request("manual stop button")

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "PENDING" in out
    assert "manual stop button" in out


def test_do_status_reports_running_and_kill_switch_active_simultaneously(
    kill_flag_path, stop_flag_path, pid_path, monkeypatch, capsys,
):
    # A running loop plus an active kill switch is a real, meaningful
    # combination (e.g. the kill switch just fired but the loop's own
    # process hasn't exited yet) -- both facts must be reported together,
    # not have one mask the other.
    pid_file.write(4242)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 4242)
    kill_switch_flag.activate("daily loss limit breached")

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "RUNNING" in out
    assert "4242" in out
    assert "ACTIVE" in out
    assert "daily loss limit breached" in out


def test_do_status_reports_lingering_stop_flag_while_loop_not_actually_running(
    kill_flag_path, stop_flag_path, pid_path, capsys,
):
    # The "orphaned flag" case: a stop was requested but nothing is running
    # to consume/clear it (e.g. the loop crashed right after the flag was
    # set, or it was requested with no loop running at all).
    stop_request_flag.request("manual stop button")

    autotrade_control.do_status()

    out = capsys.readouterr().out
    assert "NOT running" in out
    assert "PENDING" in out


# --- main() CLI dispatch ---------------------------------------------------


def test_main_requires_a_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py"])

    with pytest.raises(SystemExit):
        autotrade_control.main()


def test_main_dispatches_start(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py", "start"])
    called = {"called": False}
    monkeypatch.setattr(autotrade_control, "do_start", lambda: called.__setitem__("called", True) or 0)

    assert autotrade_control.main() == 0
    assert called["called"] is True


def test_main_dispatches_stop(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py", "stop"])
    called = {"called": False}
    monkeypatch.setattr(autotrade_control, "do_stop", lambda: called.__setitem__("called", True) or 0)

    assert autotrade_control.main() == 0
    assert called["called"] is True


def test_main_dispatches_status(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py", "status"])
    called = {"called": False}
    monkeypatch.setattr(autotrade_control, "do_status", lambda: called.__setitem__("called", True) or 0)

    assert autotrade_control.main() == 0
    assert called["called"] is True


def _capturing_do_emergency_stop(captured):
    def _fake(confirm):
        captured["confirm"] = confirm
        return 0
    return _fake


def test_main_dispatches_emergency_stop_with_confirm_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py", "emergency-stop", "--confirm"])
    captured = {}
    monkeypatch.setattr(autotrade_control, "do_emergency_stop", _capturing_do_emergency_stop(captured))

    assert autotrade_control.main() == 0
    assert captured["confirm"] is True


def test_main_dispatches_emergency_stop_without_confirm_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["autotrade_control.py", "emergency-stop"])
    captured = {}
    monkeypatch.setattr(autotrade_control, "do_emergency_stop", _capturing_do_emergency_stop(captured))

    assert autotrade_control.main() == 0
    assert captured["confirm"] is False
