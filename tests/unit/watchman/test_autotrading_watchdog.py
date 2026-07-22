"""Unit tests for watchman/autotrading_watchdog.py -- the MT5 terminal
AutoTrading-toggle watchdog (2026-07-21 incident, retcode 10027)."""
from __future__ import annotations

from datetime import datetime, timezone

from autotrade.store import journal
from autotrade.watchman import autotrading_watchdog as autotrading_watchdog_module
from autotrade.watchman.autotrading_watchdog import AutoTradingWatchdog

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(autotrading_watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def test_first_check_true_records_baseline_silently(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=tmp_path / "journal.sqlite")

    watchdog.check(True)

    assert calls == []
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=tmp_path / "journal.sqlite")
    assert events == []
    assert watchdog._last_known_state is True


def test_first_check_false_notifies_and_records_anomaly(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(False)

    assert len(calls) == 1
    assert "DISABLED" in calls[0].upper() or "OFF" in calls[0]
    assert "AutoTrading" in calls[0]
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1
    assert events[0].event_type == "autotrading_disabled"


def test_first_check_none_does_not_notify_or_record_and_stays_unset(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(None)

    assert calls == []
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert events == []
    assert watchdog._last_known_state is None

    # Next real reading is still treated as a "first check".
    watchdog.check(False)
    assert len(calls) == 1
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1


def test_true_to_false_transition_notifies_disabled_message(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(True)
    watchdog.check(False)

    assert len(calls) == 1
    assert "OFF" in calls[0]
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1
    assert events[0].event_type == "autotrading_disabled"


def test_false_to_true_transition_notifies_reenabled_message(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(False)  # first check, already disabled -- notifies once
    watchdog.check(True)  # re-enabled -- notifies again

    assert len(calls) == 2
    assert "ON" in calls[1]
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 2
    assert events[1].event_type == "autotrading_enabled"


def test_true_to_true_unchanged_does_not_notify_or_record(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(True)
    watchdog.check(True)

    assert calls == []
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert events == []


def test_false_to_false_unchanged_does_not_notify_or_record_again(monkeypatch, tmp_path):
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(False)  # first check -- notifies once
    calls.clear()
    watchdog.check(False)  # unchanged -- no further notification

    assert calls == []
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1  # still just the one from the first check


def test_none_in_the_middle_of_a_sequence_does_not_reset_the_baseline(monkeypatch, tmp_path):
    # True -> None -> False must still be detected as a True -> False
    # transition -- the None gap must not silently reset what the watchdog
    # is comparing against once real readings resume.
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(True)  # baseline, no notify
    watchdog.check(None)  # hiccup, ignored
    watchdog.check(False)  # still detected as True -> False

    assert len(calls) == 1
    assert "OFF" in calls[0]
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1
    assert events[0].event_type == "autotrading_disabled"


def test_none_in_the_middle_does_not_reset_baseline_to_none_either(monkeypatch, tmp_path):
    # Discriminates the correct behavior (a None reading is skipped without
    # touching _last_known_state at all) from a plausible bug where None
    # resets the baseline to None, which the True -> None -> False sequence
    # alone cannot catch: both the correct path (detects a real True->False
    # transition) and the buggy path (treats the 3rd call as a fresh
    # first-check with trade_allowed=False, which _alert_disabled()s too)
    # produce an identical single "OFF" notification. False -> None -> False
    # tells them apart: correct behavior notifies ONCE (on the very first
    # False) and stays silent for the unchanged 3rd False; the reset bug
    # would notify AGAIN on the 3rd call, since it would look like a fresh
    # first-check landing on False.
    calls = _capture_notify(monkeypatch)
    db_path = tmp_path / "journal.sqlite"
    watchdog = AutoTradingWatchdog(FakeClock(BASE_TIME), journal_db_path=db_path)

    watchdog.check(False)  # first-check-false, notifies once
    watchdog.check(None)  # hiccup, ignored
    watchdog.check(False)  # unchanged from the real baseline (False) -- no-op

    assert len(calls) == 1
    events = journal.get_anomaly_events_for_day(BASE_TIME.date(), db_path=db_path)
    assert len(events) == 1


def test_journal_timestamp_uses_the_injected_journal_clock():
    journal_clock = FakeClock(datetime(2099, 6, 15, tzinfo=timezone.utc))
    watchdog = AutoTradingWatchdog(journal_clock)

    watchdog.check(False)

    events = journal.get_anomaly_events_for_day(datetime(2099, 6, 15).date())
    assert len(events) == 1
    assert events[0].timestamp == journal_clock.now().replace(tzinfo=None)
