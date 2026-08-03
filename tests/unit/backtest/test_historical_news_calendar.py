"""Tests for backtest/historical_news_calendar.py -- the historical replay
`NewsCalendarProvider` over a pre-built calendar CSV."""
from __future__ import annotations

from datetime import datetime

import pytest

from autotrade.backtest.historical_news_calendar import HistoricalNewsCalendarProvider

_HEADER = "event_time,currency,importance,event_name,forecast,previous,actual"


def _write_calendar(tmp_path, rows: list[str]):
    path = tmp_path / "calendar.csv"
    path.write_text(
        "# generated_at_server_time=2026-08-04 00:00:00\n" + _HEADER + "\n" + "\n".join(rows) + "\n",
        encoding="ascii",
    )
    return path


def test_window_query_returns_only_events_inside_the_inclusive_window(tmp_path):
    path = _write_calendar(tmp_path, [
        "2026-01-01 14:29:00,USD,high,Event A,,,",
        "2026-01-01 14:30:00,USD,high,Event B,,,",
        "2026-01-01 15:00:00,USD,high,Event C,,,",
        "2026-01-01 15:01:00,USD,high,Event D,,,",
    ])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events(
        "USD", datetime(2026, 1, 1, 14, 30, 0), datetime(2026, 1, 1, 15, 0, 0),
    )

    assert sorted(e.event_time for e in events) == [datetime(2026, 1, 1, 14, 30), datetime(2026, 1, 1, 15, 0)]


def test_window_bounds_are_inclusive_at_both_ends(tmp_path):
    path = _write_calendar(tmp_path, ["2026-01-01 14:30:00,USD,high,Event,,,"])
    provider = HistoricalNewsCalendarProvider(path)

    exact_start = provider.get_high_impact_events("USD", datetime(2026, 1, 1, 14, 30), datetime(2026, 1, 1, 14, 30))
    assert len(exact_start) == 1


def test_never_returns_none_for_no_events_in_window(tmp_path):
    path = _write_calendar(tmp_path, ["2026-01-01 14:30:00,USD,high,Event,,,"])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("USD", datetime(2000, 1, 1), datetime(2000, 1, 2))

    assert events == []
    assert events is not None


def test_never_returns_none_for_an_unmapped_currency(tmp_path):
    path = _write_calendar(tmp_path, ["2026-01-01 14:30:00,USD,high,Event,,,"])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("EUR", datetime(2000, 1, 1), datetime(2100, 1, 1))

    assert events == []


def test_low_and_moderate_importance_rows_are_excluded(tmp_path):
    path = _write_calendar(tmp_path, [
        "2026-01-01 14:30:00,USD,high,High Event,,,",
        "2026-01-01 14:30:00,USD,moderate,Moderate Event,,,",
        "2026-01-01 14:30:00,USD,none,None Event,,,",
    ])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("USD", datetime(2026, 1, 1), datetime(2026, 1, 2))

    assert len(events) == 1
    assert events[0].impact == "high"


def test_importance_match_is_case_insensitive(tmp_path):
    path = _write_calendar(tmp_path, ["2026-01-01 14:30:00,USD,HIGH,Event,,,"])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("USD", datetime(2026, 1, 1), datetime(2026, 1, 2))

    assert len(events) == 1


def test_currency_filters_correctly_when_multiple_currencies_present(tmp_path):
    path = _write_calendar(tmp_path, [
        "2026-01-01 14:30:00,USD,high,USD Event,,,",
        "2026-01-01 14:30:00,EUR,high,EUR Event,,,",
    ])
    provider = HistoricalNewsCalendarProvider(path)

    usd_events = provider.get_high_impact_events("USD", datetime(2026, 1, 1), datetime(2026, 1, 2))
    eur_events = provider.get_high_impact_events("EUR", datetime(2026, 1, 1), datetime(2026, 1, 2))

    assert len(usd_events) == 1
    assert len(eur_events) == 1
    assert usd_events[0].currency == "USD"
    assert eur_events[0].currency == "EUR"


def test_events_are_sorted_regardless_of_input_file_order(tmp_path):
    path = _write_calendar(tmp_path, [
        "2026-01-01 16:00:00,USD,high,Later,,,",
        "2026-01-01 14:00:00,USD,high,Earlier,,,",
    ])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("USD", datetime(2026, 1, 1), datetime(2026, 1, 2))

    assert [e.event_time for e in events] == sorted(e.event_time for e in events)


def test_structurally_unparseable_file_raises_value_error(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("not,the,right,header\n1,2,3,4\n", encoding="ascii")

    with pytest.raises(ValueError):
        HistoricalNewsCalendarProvider(path)


def test_malformed_event_time_row_is_skipped_not_fatal(tmp_path):
    path = _write_calendar(tmp_path, [
        "not-a-timestamp,USD,high,Bad Row,,,",
        "2026-01-01 14:30:00,USD,high,Good Row,,,",
    ])
    provider = HistoricalNewsCalendarProvider(path)

    events = provider.get_high_impact_events("USD", datetime(2026, 1, 1), datetime(2026, 1, 2))

    assert len(events) == 1
    assert events[0].event_time == datetime(2026, 1, 1, 14, 30)
