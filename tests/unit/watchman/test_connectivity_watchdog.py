"""Unit tests for watchman/connectivity_watchdog.py -- Watchman item 7
(Appendix A §4.7)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from autotrade.store import journal
from autotrade.watchman import connectivity_watchdog as connectivity_watchdog_module
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog, ConnectivityWatchdogConfig


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_check_returns_false_when_no_connection_ever_recorded():
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock)

    assert watchdog.check() is False


def test_check_returns_false_within_timeout_window():
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=4)

    assert watchdog.check() is False


def test_check_returns_false_exactly_at_the_boundary():
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=5)

    assert watchdog.check() is False  # <= timeout is still "connected enough"


def test_check_returns_true_and_logs_critical_once_timeout_exceeded(caplog):
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=5, seconds=1)

    with caplog.at_level(logging.CRITICAL):
        result = watchdog.check()

    assert result is True
    assert any("CONNECTIVITY LOST" in record.message for record in caplog.records)


def test_check_alerts_only_once_per_outage_not_every_call(caplog):
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)

    with caplog.at_level(logging.CRITICAL):
        watchdog.check()
        watchdog.check()
        watchdog.check()

    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1


def test_check_alerts_once_across_checks_at_different_times_during_one_ongoing_outage(caplog):
    # test_check_alerts_only_once_per_outage_not_every_call (above) calls
    # check() three times back-to-back WITHOUT advancing the clock between
    # calls -- that only proves "no re-alert within the same instant", not
    # "no re-alert as a single ongoing outage keeps getting longer". This
    # advances the clock between each check() (never calling
    # record_connected() in between -- still the SAME outage) to confirm the
    # alert really only fires once, not once per check-past-threshold.
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=6)  # 1 minute past threshold
    with caplog.at_level(logging.CRITICAL):
        first = watchdog.check()

    clock.advance(minutes=3)  # 9 minutes into the SAME outage
    with caplog.at_level(logging.CRITICAL):
        second = watchdog.check()

    clock.advance(minutes=10)  # 19 minutes into the SAME outage
    with caplog.at_level(logging.CRITICAL):
        third = watchdog.check()

    assert (first, second, third) == (True, True, True)
    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1  # only the FIRST of the three fired


def test_record_connected_resets_and_rearms_the_alert(caplog):
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    assert watchdog.check() is True

    watchdog.record_connected()  # connection restored
    assert watchdog.check() is False

    caplog.clear()
    clock.advance(minutes=10)
    with caplog.at_level(logging.CRITICAL):
        result = watchdog.check()  # alert re-arms for the new outage

    assert result is True
    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1


def test_check_records_one_anomaly_event_per_outage_not_every_call():
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    watchdog.check()
    watchdog.check()
    watchdog.check()

    events = journal.get_anomaly_events_for_day(BASE_TIME.date())
    assert len(events) == 1
    assert events[0].event_type == "reconnect"


def test_journal_timestamp_uses_journal_clock_not_the_elapsed_duration_clock():
    # Regression test: the journal write must use journal_clock (server
    # time), independently of whatever clock drives the watchdog's own
    # elapsed-duration math -- a reconnect anomaly near server-time midnight
    # must not be filed under the wrong report day just because it shares
    # the wall-clock/UTC clock used for timeout detection.
    elapsed_clock = FakeClock(BASE_TIME)
    journal_clock = FakeClock(datetime(2099, 6, 15, tzinfo=timezone.utc))
    watchdog = ConnectivityWatchdog(
        elapsed_clock, ConnectivityWatchdogConfig(timeout_minutes=5.0), journal_clock=journal_clock,
    )

    watchdog.record_connected()
    elapsed_clock.advance(minutes=10)  # timeout detection still driven by elapsed_clock
    result = watchdog.check()

    assert result is True
    events = journal.get_anomaly_events_for_day(datetime(2099, 6, 15).date())
    assert len(events) == 1
    assert events[0].timestamp == journal_clock.now().replace(tzinfo=None)


def test_journal_clock_defaults_to_the_main_clock_when_not_given():
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    watchdog.check()

    events = journal.get_anomaly_events_for_day(BASE_TIME.date())
    assert len(events) == 1


# --- 2026-07-25: notify() alerting (was log/journal-only before) -------------


def _capture_notify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(connectivity_watchdog_module, "notify", lambda text: calls.append(text))
    return calls


def test_outage_notifies_once_via_telegram(monkeypatch):
    calls = _capture_notify(monkeypatch)
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    watchdog.check()
    watchdog.check()  # same ongoing outage -- must not re-notify

    assert len(calls) == 1
    assert "CONNECTIVITY LOST" in calls[0]


def test_recovery_after_an_alerted_outage_notifies_restored(monkeypatch):
    calls = _capture_notify(monkeypatch)
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    watchdog.check()  # alerts DOWN
    watchdog.record_connected()  # recovers

    assert len(calls) == 2
    assert "CONNECTIVITY LOST" in calls[0]
    assert "restored" in calls[1] or "✅" in calls[1]


def test_record_connected_without_a_prior_alert_does_not_notify(monkeypatch):
    calls = _capture_notify(monkeypatch)
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=2)  # still within timeout, never actually alerted
    watchdog.record_connected()

    assert calls == []


def test_recovery_notification_does_not_re_fire_on_a_later_still_connected_call(monkeypatch):
    calls = _capture_notify(monkeypatch)
    clock = FakeClock(BASE_TIME)
    watchdog = ConnectivityWatchdog(clock, ConnectivityWatchdogConfig(timeout_minutes=5.0))

    watchdog.record_connected()
    clock.advance(minutes=10)
    watchdog.check()  # DOWN
    watchdog.record_connected()  # restored
    watchdog.record_connected()  # still fine -- must stay quiet

    assert len(calls) == 2
