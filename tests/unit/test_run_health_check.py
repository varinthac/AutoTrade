"""Unit tests for scripts/run_health_check.py -- thin wiring only (the real
logic lives in common/loop_watchdog.py, common/service_watchdog.py,
common/calendar_export_watchdog.py, and common/cloudflared_watchdog.py, all
independently tested). Confirms this script calls every check with
auto-restart enabled and prints the same status line autotrade_control.py
status already does. scripts/ has no __init__.py, so the script is loaded
directly via importlib, same pattern as tests/unit/test_run_shadow_loop.py.

2026-08-04 (lean-plan P1, docs/vps_lean_plan.md): the dashboard was removed
from this script's check_and_restart cycle -- it is on-demand now, not
always-on, and this heartbeat must never resurrect it (see the script's own
module docstring). `test_main_never_checks_or_restarts_the_dashboard` below
is a standing regression guard for exactly that trap.

Every check other than the one under direct test is stubbed out in every
test here, not just the ones a given test cares about -- `check_calendar_export`,
`check_cloudflared`, `check_kill_switch_reminder`, `check_manual_halt_reminder`,
and `check_scheduled_tasks` in particular must never run for real during a
test (they touch real `data/db/*_watchdog_state.json` files, shell out to
real `tasklist`/`schtasks`/`taskkill`, and on a stale/missing export or a
schtasks query failing against tasks that don't exist on a dev machine
would fire a real Telegram `notify()`)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_health_check.py"
_spec = importlib.util.spec_from_file_location("run_health_check_script", SCRIPT_PATH)
run_health_check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_health_check
_spec.loader.exec_module(run_health_check)


def _stub_common(monkeypatch, loop_running: bool = True, pid: int | None = 4242):
    monkeypatch.setattr(run_health_check, "check_calendar_export", lambda: False)
    monkeypatch.setattr(run_health_check, "check_cloudflared", lambda: True)
    monkeypatch.setattr(run_health_check, "check_kill_switch_reminder", lambda: False)
    monkeypatch.setattr(run_health_check, "check_manual_halt_reminder", lambda: False)
    monkeypatch.setattr(run_health_check, "check_scheduled_tasks", lambda: {})
    monkeypatch.setattr(run_health_check, "check_loop_alive", lambda auto_restart: loop_running)
    monkeypatch.setattr(run_health_check.pid_file, "read", lambda: pid)


def test_main_checks_shadow_loop_and_telegram_control(monkeypatch, capsys):
    _stub_common(monkeypatch, loop_running=True, pid=4242)
    service_calls = []
    monkeypatch.setattr(
        run_health_check, "check_and_restart",
        lambda name, pid_path, state_path, script, cwd: service_calls.append(name) or True,
    )

    exit_code = run_health_check.main()

    assert exit_code == 0
    assert service_calls == ["Telegram control listener"]
    assert "RUNNING (PID 4242)" in capsys.readouterr().out


def test_main_never_checks_or_restarts_the_dashboard(monkeypatch, capsys):
    # 2026-08-04 (lean-plan P1): standing regression guard -- the dashboard
    # is on-demand now, and this heartbeat resurrecting it within one cycle
    # would silently undo the whole point (see this script's own module
    # docstring, and docs/vps_lean_plan.md's P1 section).
    _stub_common(monkeypatch, loop_running=True, pid=4242)
    service_calls = []
    monkeypatch.setattr(
        run_health_check, "check_and_restart",
        lambda name, pid_path, state_path, script, cwd: service_calls.append(name) or True,
    )

    run_health_check.main()

    assert "Dashboard" not in service_calls
    assert not hasattr(run_health_check, "_DASHBOARD_SCRIPT")
    assert not hasattr(run_health_check, "_DASHBOARD_PID_PATH")
    assert not hasattr(run_health_check, "_DASHBOARD_STATE_PATH")


def test_main_prints_not_running_when_loop_is_down(monkeypatch, capsys):
    _stub_common(monkeypatch, loop_running=False, pid=None)
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: False,
    )

    exit_code = run_health_check.main()

    assert exit_code == 0
    assert "NOT running" in capsys.readouterr().out


def test_main_passes_auto_restart_true_to_loop_check(monkeypatch):
    captured = {}
    _stub_common(monkeypatch, loop_running=True, pid=1)
    monkeypatch.setattr(
        run_health_check, "check_loop_alive",
        lambda auto_restart: captured.setdefault("auto_restart", auto_restart) or True,
    )
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: True,
    )

    run_health_check.main()

    assert captured["auto_restart"] is True


def test_main_checks_calendar_export_before_loop_alive_and_cloudflared(monkeypatch, capsys):
    _stub_common(monkeypatch, loop_running=True, pid=4242)
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: True,
    )
    order = []
    monkeypatch.setattr(run_health_check, "check_calendar_export", lambda: order.append("calendar") or False)
    monkeypatch.setattr(run_health_check, "check_loop_alive", lambda auto_restart: order.append("loop") or True)
    monkeypatch.setattr(run_health_check, "check_cloudflared", lambda: order.append("cloudflared") or True)
    monkeypatch.setattr(run_health_check, "check_kill_switch_reminder", lambda: order.append("kill_switch") or False)
    monkeypatch.setattr(run_health_check, "check_manual_halt_reminder", lambda: order.append("manual_halt") or False)
    monkeypatch.setattr(run_health_check, "check_scheduled_tasks", lambda: order.append("scheduled_tasks") or {})

    run_health_check.main()

    assert order == ["calendar", "loop", "cloudflared", "kill_switch", "manual_halt", "scheduled_tasks"]


def test_main_calls_kill_switch_reminder_and_scheduled_task_checks(monkeypatch, capsys):
    _stub_common(monkeypatch, loop_running=True, pid=4242)
    monkeypatch.setattr(
        run_health_check, "check_and_restart", lambda name, pid_path, state_path, script, cwd: True,
    )
    reminder_calls = []
    manual_halt_calls = []
    scheduled_calls = []
    monkeypatch.setattr(
        run_health_check, "check_kill_switch_reminder", lambda: reminder_calls.append(1) or False,
    )
    monkeypatch.setattr(
        run_health_check, "check_manual_halt_reminder", lambda: manual_halt_calls.append(1) or False,
    )
    monkeypatch.setattr(
        run_health_check, "check_scheduled_tasks", lambda: scheduled_calls.append(1) or {},
    )

    run_health_check.main()

    assert len(reminder_calls) == 1
    assert len(manual_halt_calls) == 1
    assert len(scheduled_calls) == 1
