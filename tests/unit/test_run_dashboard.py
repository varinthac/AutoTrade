"""Unit tests for scripts/run_dashboard.py's double-launch guard -- 2026-07-24:
an RDP reconnect re-fires the "At log on" Task Scheduler trigger, and a
second Flask instance racing the first for the same port would otherwise
crash on bind rather than cleanly refusing to start. scripts/ has no
__init__.py, so the script is loaded directly via importlib, same pattern as
tests/unit/test_run_telegram_control.py."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_dashboard.py"
_spec = importlib.util.spec_from_file_location("run_dashboard_script", SCRIPT_PATH)
run_dashboard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_dashboard
_spec.loader.exec_module(run_dashboard)


class _FakeApp:
    def __init__(self, on_run=None):
        self._on_run = on_run

    def run(self, **kwargs):
        if self._on_run is not None:
            self._on_run()


def test_main_refuses_second_instance_while_one_is_genuinely_running(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", tmp_path / "dashboard.pid")
    monkeypatch.setattr(run_dashboard.pid_file, "is_pid_running", lambda pid: True)
    create_app_calls = []
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: create_app_calls.append(kw) or _FakeApp())
    (tmp_path / "dashboard.pid").write_text("999", encoding="utf-8")

    exit_code = run_dashboard.main()

    assert exit_code == 1
    assert create_app_calls == []


def test_main_writes_and_removes_pid_file_around_a_clean_run(monkeypatch, tmp_path):
    pid_path = tmp_path / "dashboard.pid"
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", pid_path)
    written_while_running = {}

    def on_run():
        written_while_running["pid"] = pid_path.read_text(encoding="utf-8")

    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _FakeApp(on_run=on_run))

    exit_code = run_dashboard.main()

    assert exit_code == 0
    assert written_while_running["pid"] == str(os.getpid())
    assert not pid_path.exists()


def test_main_overwrites_stale_pid_file_from_a_no_longer_running_process(monkeypatch, tmp_path):
    pid_path = tmp_path / "dashboard.pid"
    pid_path.write_text("999", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", pid_path)
    monkeypatch.setattr(run_dashboard.pid_file, "is_pid_running", lambda pid: False)
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _FakeApp())

    exit_code = run_dashboard.main()

    assert exit_code == 0


def test_main_removes_pid_file_even_if_app_run_raises(monkeypatch, tmp_path):
    pid_path = tmp_path / "dashboard.pid"
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", pid_path)

    class _RaisingApp:
        def run(self, **kwargs):
            raise OSError("port already in use")

    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _RaisingApp())

    try:
        run_dashboard.main()
    except OSError:
        pass

    assert not pid_path.exists()
