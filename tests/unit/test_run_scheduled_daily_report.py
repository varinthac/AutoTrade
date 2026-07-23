"""Unit tests for scripts/run_scheduled_daily_report.py -- the Task
Scheduler wrapper that resolves 'yesterday' from local wall-clock date and
passes it explicitly to `run_auditor.py daily --date`, same
importlib-loading convention as tests/unit/test_kill_switch_script.py
(scripts/ has no __init__.py)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_scheduled_daily_report.py"
_spec = importlib.util.spec_from_file_location("run_scheduled_daily_report_script", SCRIPT_PATH)
run_scheduled_daily_report_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_scheduled_daily_report_script
_spec.loader.exec_module(run_scheduled_daily_report_script)


class _FixedDate(date):
    @classmethod
    def today(cls):
        return date(2026, 7, 23)


def test_resolves_yesterday_from_local_date_and_passes_it_explicitly(monkeypatch):
    monkeypatch.setattr(run_scheduled_daily_report_script, "date", _FixedDate)
    captured = {}

    def _fake_call(cmd):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(run_scheduled_daily_report_script.subprocess, "call", _fake_call)

    exit_code = run_scheduled_daily_report_script.main()

    assert exit_code == 0
    cmd = captured["cmd"]
    assert "--date" in cmd
    assert cmd[cmd.index("--date") + 1] == "2026-07-22"  # 2026-07-23 minus one day
    assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "paper"
    assert "--notify" in cmd
    assert "daily" in cmd


def test_month_boundary_rolls_back_correctly(monkeypatch):
    class _AugustFirst(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 1)

    monkeypatch.setattr(run_scheduled_daily_report_script, "date", _AugustFirst)
    captured = {}

    def _fake_call(cmd):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(run_scheduled_daily_report_script.subprocess, "call", _fake_call)

    run_scheduled_daily_report_script.main()

    cmd = captured["cmd"]
    assert cmd[cmd.index("--date") + 1] == "2026-07-31"


def test_propagates_the_subprocess_exit_code(monkeypatch):
    monkeypatch.setattr(run_scheduled_daily_report_script, "date", _FixedDate)
    monkeypatch.setattr(run_scheduled_daily_report_script.subprocess, "call", lambda cmd: 1)

    assert run_scheduled_daily_report_script.main() == 1
