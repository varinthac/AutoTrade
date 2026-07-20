"""Tests for council/finnhub_news_calendar.py -- FinnhubNewsCalendarProvider,
the real `NewsCalendarProvider` implementation backed by Finnhub's economic
calendar. The HTTP call (`urllib.request.urlopen`) is monkeypatched -- no
live network calls, same "mock the boundary" convention as
tests/unit/execution/test_demo_adapter.py mocking `MetaTrader5`.
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

from autotrade.council import finnhub_news_calendar as fnc
from autotrade.council.finnhub_news_calendar import FinnhubNewsCalendarProvider


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


class _FakeResponse:
    """Minimal stand-in for the `http.client.HTTPResponse` context manager
    `urllib.request.urlopen` returns."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _calendar_payload(events: list[dict]) -> bytes:
    return json.dumps({"economicCalendar": events}).encode("utf-8")


def _event(country: str, impact: str, time: str, event: str = "Some Event") -> dict:
    return {"country": country, "impact": impact, "event": event, "time": time}


WINDOW_START = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)


def _provider(monkeypatch, responder, clock=None) -> FinnhubNewsCalendarProvider:
    """`responder(url, timeout)` stands in for `urllib.request.urlopen`."""
    monkeypatch.setattr(fnc.urllib.request, "urlopen", responder)
    return FinnhubNewsCalendarProvider("fake-token", clock=clock or FixedClock(WINDOW_START))


def test_successful_fetch_with_high_impact_events_found(monkeypatch):
    calls = []

    def responder(url, timeout=None):
        calls.append(url)
        body = _calendar_payload([
            _event("US", "high", "2026-07-20 12:30:00", event="US CPI"),
        ])
        return _FakeResponse(body)

    provider = _provider(monkeypatch, responder)
    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].currency == "USD"
    assert events[0].impact == "high"
    assert events[0].event_time == datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    assert len(calls) == 1


def test_successful_fetch_with_no_matching_events_returns_empty_list_not_none(monkeypatch):
    def responder(url, timeout=None):
        return _FakeResponse(_calendar_payload([]))

    provider = _provider(monkeypatch, responder)
    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events == []
    assert events is not None


def test_non_2xx_http_response_returns_none(monkeypatch):
    def responder(url, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_network_timeout_error_returns_none(monkeypatch):
    def responder(url, timeout=None):
        raise TimeoutError("timed out")

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_network_url_error_returns_none(monkeypatch):
    def responder(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_malformed_json_returns_none(monkeypatch):
    def responder(url, timeout=None):
        return _FakeResponse(b"not json at all")

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_unexpected_json_shape_returns_none(monkeypatch):
    def responder(url, timeout=None):
        return _FakeResponse(json.dumps({"somethingElse": []}).encode("utf-8"))

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_currency_filtering_excludes_other_countries(monkeypatch):
    def responder(url, timeout=None):
        body = _calendar_payload([
            _event("GB", "high", "2026-07-20 12:30:00", event="UK event"),
            _event("US", "high", "2026-07-20 12:15:00", event="US event"),
        ])
        return _FakeResponse(body)

    provider = _provider(monkeypatch, responder)
    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].currency == "USD"


def test_high_impact_only_filtering_excludes_low_and_medium(monkeypatch):
    def responder(url, timeout=None):
        body = _calendar_payload([
            _event("US", "low", "2026-07-20 12:10:00"),
            _event("US", "medium", "2026-07-20 12:20:00"),
            _event("US", "high", "2026-07-20 12:30:00"),
        ])
        return _FakeResponse(body)

    provider = _provider(monkeypatch, responder)
    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].impact == "high"


def test_unmapped_currency_fails_safe_without_any_http_call(monkeypatch):
    def responder(url, timeout=None):
        raise AssertionError("should not be called for an unmapped currency")

    provider = _provider(monkeypatch, responder)
    assert provider.get_high_impact_events("CHF", WINDOW_START, WINDOW_END) is None


def test_second_call_within_ttl_does_not_re_hit_http_client(monkeypatch):
    call_count = 0

    def responder(url, timeout=None):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(_calendar_payload([]))

    clock = FixedClock(WINDOW_START)
    provider = _provider(monkeypatch, responder, clock=clock)

    provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)
    clock.advance(minutes=5)
    provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert call_count == 1


def test_call_outside_ttl_re_hits_http_client(monkeypatch):
    call_count = 0

    def responder(url, timeout=None):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(_calendar_payload([]))

    clock = FixedClock(WINDOW_START)
    provider = FinnhubNewsCalendarProvider("fake-token", clock=clock, cache_ttl_minutes=15.0)
    monkeypatch.setattr(fnc.urllib.request, "urlopen", responder)

    provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)
    clock.advance(minutes=16)
    provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert call_count == 2
