"""Tests for council/news_calendar.py -- trivial by design: the only
implementation shipped is a stub that always returns `None` (see the module
docstring's KNOWN, DELIBERATE LIMITATION section)."""
from __future__ import annotations

from datetime import datetime

from autotrade.council.news_calendar import StubNewsCalendarProvider


def test_stub_always_returns_none():
    provider = StubNewsCalendarProvider()
    result = provider.get_high_impact_events(
        "USD", datetime(2026, 7, 19, 12, 0), datetime(2026, 7, 19, 13, 0)
    )
    assert result is None


def test_stub_returns_none_regardless_of_currency_or_window():
    provider = StubNewsCalendarProvider()
    assert provider.get_high_impact_events("EUR", datetime(2020, 1, 1), datetime(2030, 1, 1)) is None
    assert provider.get_high_impact_events("JPY", datetime(2026, 7, 19), datetime(2026, 7, 19)) is None
