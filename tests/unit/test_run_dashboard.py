"""Unit tests for scripts/run_dashboard.py -- the double-launch guard
(2026-07-24: an RDP reconnect re-fires the "At log on" Task Scheduler
trigger, and a second Flask instance racing the first for the same port
would otherwise crash on bind rather than cleanly refusing to start) and the
idle-TTL auto-shutdown watchdog (2026-08-04, lean-plan P1: the dashboard is
on-demand now, so a forgotten instance must eventually shut itself down --
see scripts/run_health_check.py's own docstring for why nothing auto-restarts
it anymore). The watchdog's THREAD is never actually exercised here (that
would mean real sleeping) -- only its pure decision function
(_should_shut_down) and the tracker's touch()/idle_seconds() plumbing, per
this repo's "test the decision function, not a real N-minute sleep"
convention. scripts/ has no __init__.py, so the script is loaded directly via
importlib, same pattern as tests/unit/test_run_telegram_control.py."""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
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


# --- idle-TTL: _should_shut_down (pure decision function) -------------------


def test_should_shut_down_false_before_ttl_elapsed():
    assert run_dashboard._should_shut_down(idle_seconds=29 * 60, ttl_minutes=30) is False


def test_should_shut_down_true_at_or_past_ttl():
    assert run_dashboard._should_shut_down(idle_seconds=30 * 60, ttl_minutes=30) is True
    assert run_dashboard._should_shut_down(idle_seconds=31 * 60, ttl_minutes=30) is True


def test_should_shut_down_zero_ttl_never_triggers():
    assert run_dashboard._should_shut_down(idle_seconds=10**9, ttl_minutes=0) is False


def test_should_shut_down_negative_ttl_never_triggers():
    assert run_dashboard._should_shut_down(idle_seconds=10**9, ttl_minutes=-5) is False


# --- idle-TTL: _ActivityTracker ---------------------------------------------


def test_activity_tracker_touch_resets_idle_seconds_to_near_zero():
    tracker = run_dashboard._ActivityTracker()

    tracker.touch()

    assert tracker.idle_seconds() < 1.0


def test_activity_tracker_idle_seconds_grows_without_a_touch(monkeypatch):
    times = iter([100.0, 250.0])
    monkeypatch.setattr(run_dashboard.time, "monotonic", lambda: next(times))

    tracker = run_dashboard._ActivityTracker()  # consumes 100.0 at construction

    assert tracker.idle_seconds() == 150.0  # consumes 250.0


# --- idle-TTL: _idle_watchdog (fake stop_event/tracker, never a real sleep) -


class _FakeStopEvent:
    def __init__(self, wait_results):
        self._results = iter(wait_results)

    def wait(self, timeout):
        return next(self._results)


class _FakeTracker:
    def __init__(self, idle_values):
        self._values = iter(idle_values)

    def idle_seconds(self):
        return next(self._values)


def test_idle_watchdog_exits_process_once_idle_threshold_reached():
    exit_calls = []
    stop_event = _FakeStopEvent([False])  # one poll, never stopped externally
    tracker = _FakeTracker([30 * 60])  # already at the 30-minute threshold

    run_dashboard._idle_watchdog(
        tracker, ttl_minutes=30, stop_event=stop_event, exit_fn=lambda code: exit_calls.append(code),
    )

    assert exit_calls == [0]


def test_idle_watchdog_keeps_polling_without_exiting_while_active():
    exit_calls = []
    stop_event = _FakeStopEvent([False, True])  # one poll below threshold, then stopped
    tracker = _FakeTracker([5 * 60])  # well below the 30-minute threshold

    run_dashboard._idle_watchdog(
        tracker, ttl_minutes=30, stop_event=stop_event, exit_fn=lambda code: exit_calls.append(code),
    )

    assert exit_calls == []


def test_idle_watchdog_stops_immediately_when_stop_event_already_set():
    exit_calls = []
    stop_event = _FakeStopEvent([True])
    tracker = _FakeTracker([10**9])  # would trigger shutdown if ever checked

    run_dashboard._idle_watchdog(
        tracker, ttl_minutes=30, stop_event=stop_event, exit_fn=lambda code: exit_calls.append(code),
    )

    assert exit_calls == []


# --- idle-TTL: main() wiring --------------------------------------------------


class _FakeThread:
    calls: list[dict] = []

    def __init__(self, target=None, args=(), daemon=None):
        _FakeThread.calls.append({"target": target, "args": args, "daemon": daemon})

    def start(self):
        pass


def test_main_default_idle_ttl_minutes_is_30_and_starts_a_daemon_watchdog_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", tmp_path / "dashboard.pid")
    _FakeThread.calls = []
    monkeypatch.setattr(run_dashboard.threading, "Thread", _FakeThread)
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _FakeApp())

    run_dashboard.main()

    assert len(_FakeThread.calls) == 1
    assert _FakeThread.calls[0]["daemon"] is True
    assert _FakeThread.calls[0]["target"] is run_dashboard._idle_watchdog
    assert _FakeThread.calls[0]["args"][1] == 30


def test_main_idle_ttl_zero_disables_the_watchdog_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py", "--idle-ttl-minutes", "0"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", tmp_path / "dashboard.pid")

    def _raise(*args, **kwargs):
        raise AssertionError("threading.Thread must not be constructed when the idle TTL is disabled")

    monkeypatch.setattr(run_dashboard.threading, "Thread", _raise)
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _FakeApp())

    exit_code = run_dashboard.main()

    assert exit_code == 0


def test_main_idle_ttl_negative_also_disables_the_watchdog_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py", "--idle-ttl-minutes", "-5"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", tmp_path / "dashboard.pid")

    def _raise(*args, **kwargs):
        raise AssertionError("threading.Thread must not be constructed when the idle TTL is disabled")

    monkeypatch.setattr(run_dashboard.threading, "Thread", _raise)
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: _FakeApp())

    exit_code = run_dashboard.main()

    assert exit_code == 0


def test_main_passes_the_activity_tracker_touch_hook_to_create_app(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_dashboard.py", "--idle-ttl-minutes", "0"])
    monkeypatch.setattr(run_dashboard, "PID_PATH", tmp_path / "dashboard.pid")
    create_app_calls = []
    monkeypatch.setattr(run_dashboard, "create_app", lambda **kw: create_app_calls.append(kw) or _FakeApp())

    run_dashboard.main()

    assert callable(create_app_calls[0]["on_request"])
