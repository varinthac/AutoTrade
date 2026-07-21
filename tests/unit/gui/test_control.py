"""Unit tests for autotrade.gui.control -- status/action wrappers, no
tkinter involved. subprocess.run is always mocked; nothing here ever shells
out to a real tasklist/autotrade_control.py/kill_switch.py. Mirrors
tests/unit/test_autotrade_control.py's fixture/monkeypatch style."""
from __future__ import annotations

import subprocess
import sys

import pytest

from autotrade.common import kill_switch_flag, pid_file, stop_request_flag
from autotrade.gui import control


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


# --- build_status() ------------------------------------------------------


def test_build_status_nothing_active(kill_flag_path, stop_flag_path, pid_path):
    report = control.build_status()

    assert report.loop_running is False
    assert report.loop_pid is None
    assert report.kill_switch_active is False
    assert report.kill_switch_reason is None
    assert report.stop_pending is False
    assert report.stop_pending_reason is None


def test_build_status_loop_running_only(kill_flag_path, stop_flag_path, pid_path, monkeypatch):
    pid_file.write(1234)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 1234)

    report = control.build_status()

    assert report.loop_running is True
    assert report.loop_pid == 1234
    assert report.kill_switch_active is False
    assert report.stop_pending is False


def test_build_status_kill_switch_active_only(kill_flag_path, stop_flag_path, pid_path):
    kill_switch_flag.activate("daily loss limit breached")

    report = control.build_status()

    assert report.loop_running is False
    assert report.kill_switch_active is True
    assert report.kill_switch_reason == "daily loss limit breached"
    assert report.stop_pending is False


def test_build_status_stop_pending_only(kill_flag_path, stop_flag_path, pid_path):
    stop_request_flag.request("manual stop button")

    report = control.build_status()

    assert report.loop_running is False
    assert report.kill_switch_active is False
    assert report.stop_pending is True
    assert report.stop_pending_reason == "manual stop button"


def test_build_status_running_and_kill_switch_active_combined(
    kill_flag_path, stop_flag_path, pid_path, monkeypatch,
):
    pid_file.write(4242)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 4242)
    kill_switch_flag.activate("daily loss limit breached")

    report = control.build_status()

    assert report.loop_running is True
    assert report.loop_pid == 4242
    assert report.kill_switch_active is True
    assert report.kill_switch_reason == "daily loss limit breached"


def test_build_status_stale_pid_reports_not_running(kill_flag_path, stop_flag_path, pid_path, monkeypatch):
    pid_file.write(9999)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: False)

    report = control.build_status()

    assert report.loop_running is False
    assert report.loop_pid is None


def test_build_status_all_combined(kill_flag_path, stop_flag_path, pid_path, monkeypatch):
    pid_file.write(55)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 55)
    kill_switch_flag.activate("manual halt")
    stop_request_flag.request("manual stop")

    report = control.build_status()

    assert report.loop_running is True
    assert report.loop_pid == 55
    assert report.kill_switch_active is True
    assert report.kill_switch_reason == "manual halt"
    assert report.stop_pending is True
    assert report.stop_pending_reason == "manual stop"


# --- can_start() -----------------------------------------------------------


def test_can_start_true_when_nothing_active():
    report = control.StatusReport(
        loop_running=False, loop_pid=None,
        kill_switch_active=False, kill_switch_reason=None,
        stop_pending=False, stop_pending_reason=None,
    )
    assert control.can_start(report) is True


def test_can_start_false_when_loop_running():
    report = control.StatusReport(
        loop_running=True, loop_pid=123,
        kill_switch_active=False, kill_switch_reason=None,
        stop_pending=False, stop_pending_reason=None,
    )
    assert control.can_start(report) is False


def test_can_start_false_when_kill_switch_active():
    report = control.StatusReport(
        loop_running=False, loop_pid=None,
        kill_switch_active=True, kill_switch_reason="halted",
        stop_pending=False, stop_pending_reason=None,
    )
    assert control.can_start(report) is False


def test_can_start_false_when_both_active():
    report = control.StatusReport(
        loop_running=True, loop_pid=123,
        kill_switch_active=True, kill_switch_reason="halted",
        stop_pending=False, stop_pending_reason=None,
    )
    assert control.can_start(report) is False


def test_can_start_true_when_only_stop_pending():
    report = control.StatusReport(
        loop_running=False, loop_pid=None,
        kill_switch_active=False, kill_switch_reason=None,
        stop_pending=True, stop_pending_reason="manual stop",
    )
    assert control.can_start(report) is True


# --- start_bot() / stop_bot() / emergency_stop_bot() ------------------------


def test_start_bot_invokes_control_script_with_start_argv(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    control.start_bot()

    assert len(calls) == 1
    args = calls[0]
    assert args == [sys.executable, str(control.CONTROL_SCRIPT), "start"]
    assert "--confirm" not in args


def test_stop_bot_invokes_control_script_with_stop_argv(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    control.stop_bot()

    assert len(calls) == 1
    args = calls[0]
    assert args == [sys.executable, str(control.CONTROL_SCRIPT), "stop"]
    assert "--confirm" not in args


def test_emergency_stop_bot_invokes_control_script_with_confirm_flag(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    control.emergency_stop_bot()

    assert len(calls) == 1
    args = calls[0]
    assert args == [sys.executable, str(control.CONTROL_SCRIPT), "emergency-stop", "--confirm"]
    assert "--confirm" in args


# --- format_status() ---------------------------------------------------


def test_format_status_includes_running_pid_and_kill_switch_reason():
    report = control.StatusReport(
        loop_running=True, loop_pid=1234,
        kill_switch_active=True, kill_switch_reason="daily loss limit breached",
        stop_pending=False, stop_pending_reason=None,
    )

    text = control.format_status(report)

    assert "1234" in text
    assert "daily loss limit breached" in text
