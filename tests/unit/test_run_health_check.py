"""Unit tests for scripts/run_health_check.py -- thin wiring only (the real
logic lives in common/loop_watchdog.py and common/service_watchdog.py, both
independently tested). Confirms this script calls all three checks with
auto-restart enabled and prints the same status line autotrade_control.py
status already does. scripts/ has no __init__.py, so the script is loaded
directly via importlib, same pattern as tests/unit/test_run_shadow_loop.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_health_check.py"
_spec = importlib.util.spec_from_file_location("run_health_check_script", SCRIPT_PATH)
run_health_check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_health_check
_spec.loader.exec_module(run_health_check)


def test_main_checks_shadow_loop_dashboard_and_telegram_control(monkeypatch, capsys):
    monkeypatch.setattr(run_health_check, "check_loop_alive", lambda auto_restart: True)
    monkeypatch.setattr(run_health_check.pid_file, "read", lambda: 4242)
    service_calls = []
    monkeypatch.setattr(
        run_health_check, "check_and_restart",
        lambda name, pid_path, state_path, script, cwd: service_calls.append(name) or True,
    )

    exit_code = run_health_check.main()

    assert exit_code == 0
    assert service_calls == ["Dashboard", "Telegram control listener"]
    assert "RUNNING (PID 4242)" in capsys.readouterr().out


def test_main_prints_not_running_when_loop_is_down(monkeypatch, capsys):
    monkeypatch.setattr(run_health_check, "check_loop_alive", lambda auto_restart: False)
    monkeypatch.setattr(run_health_check.pid_file, "read", lambda: None)
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: False,
    )

    exit_code = run_health_check.main()

    assert exit_code == 0
    assert "NOT running" in capsys.readouterr().out


def test_main_passes_auto_restart_true_to_loop_check(monkeypatch):
    captured = {}
    monkeypatch.setattr(run_health_check, "check_loop_alive", lambda auto_restart: captured.setdefault("auto_restart", auto_restart) or True)
    monkeypatch.setattr(run_health_check.pid_file, "read", lambda: 1)
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: True,
    )

    run_health_check.main()

    assert captured["auto_restart"] is True
