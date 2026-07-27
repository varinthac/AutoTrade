"""Unit tests for common/kill_switch_reminder.py -- the periodic "kill
switch has been active for N hours" reminder (2026-07-28 audit finding)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from autotrade.common import kill_switch_flag as kill_switch_flag_module
from autotrade.common import kill_switch_reminder as reminder_module
from autotrade.common.kill_switch_reminder import check_and_remind


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(reminder_module, "notify", lambda text: calls.append(text))
    return calls


def _stub_kill_switch(monkeypatch, status: dict | None):
    monkeypatch.setattr(kill_switch_flag_module, "get_status", lambda flag_path=None: status)
    monkeypatch.setattr(reminder_module.kill_switch_flag, "get_status", lambda flag_path=None: status)


def test_not_active_does_nothing(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_kill_switch(monkeypatch, None)
    state_path = tmp_path / "state.json"

    result = check_and_remind(state_path=state_path)

    assert result is False
    assert calls == []


def test_freshly_activated_does_not_immediately_remind(monkeypatch, tmp_path):
    # do_activate() already sent its own alert -- the first periodic
    # reminder shouldn't fire again on the very next heartbeat cycle.
    calls = _capture_notify(monkeypatch)
    activated_at = datetime.now(timezone.utc).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "daily loss limit breached"})
    state_path = tmp_path / "state.json"

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is False
    assert calls == []


def test_reminds_once_interval_has_passed_since_activation(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    activated_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "daily loss limit breached"})
    state_path = tmp_path / "state.json"

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is True
    assert len(calls) == 1
    assert "ACTIVE" in calls[0]
    assert "daily loss limit breached" in calls[0]


def test_does_not_remind_again_within_interval_of_last_reminder(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    activated_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "test"})
    state_path = tmp_path / "state.json"

    first = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)
    calls.clear()
    second = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert first is True
    assert second is False
    assert calls == []


def test_reminds_again_after_a_second_interval_elapses(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    activated_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "test"})
    state_path = tmp_path / "state.json"
    check_and_remind(state_path=state_path, reminder_interval_hours=24.0)  # first reminder

    # simulate 24h+ passing by backdating the saved last_reminded_at
    stale_reminder = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state_path.write_text(json.dumps({"last_reminded_at": stale_reminder}), encoding="utf-8")
    calls.clear()

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is True
    assert len(calls) == 1


def test_deactivation_clears_state_so_a_future_reactivation_starts_fresh(monkeypatch, tmp_path):
    activated_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "test"})
    state_path = tmp_path / "state.json"
    check_and_remind(state_path=state_path, reminder_interval_hours=24.0)
    assert state_path.exists()

    _stub_kill_switch(monkeypatch, None)  # deactivated
    check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert not state_path.exists()


def test_missing_activated_at_still_reminds_with_unknown_duration(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    _stub_kill_switch(monkeypatch, {"activated_at": None, "reason": "<unreadable flag file>"})
    state_path = tmp_path / "state.json"

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is True
    assert "unknown duration" in calls[0]


def test_unparseable_activated_at_shows_unknown_duration_not_zero_hours(monkeypatch, tmp_path):
    # 2026-07-28 code review finding: a non-empty but unparseable
    # activated_at (corrupt flag file) used to compute duration as
    # now-minus-now (misleadingly "0.0 hour(s)") instead of "an unknown
    # duration".
    calls = _capture_notify(monkeypatch)
    _stub_kill_switch(monkeypatch, {"activated_at": "not-a-real-timestamp", "reason": "test"})
    state_path = tmp_path / "state.json"

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is True
    assert "unknown duration" in calls[0]
    assert "0.0 hour" not in calls[0]


def test_corrupt_state_file_is_treated_as_no_prior_reminder_not_a_crash(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    activated_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _stub_kill_switch(monkeypatch, {"activated_at": activated_at, "reason": "test"})
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    result = check_and_remind(state_path=state_path, reminder_interval_hours=24.0)

    assert result is True
    assert len(calls) == 1


def test_never_raises_on_internal_error(monkeypatch, tmp_path):
    def _boom(flag_path=None):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(reminder_module.kill_switch_flag, "get_status", _boom)

    result = check_and_remind(state_path=tmp_path / "state.json")

    assert result is False
