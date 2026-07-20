"""Tests for council/risk_voice.py -- the Risk Voice's 6 veto conditions,
Appendix A §1.5.

Each single-condition test constructs the minimal state to trip exactly one
condition, and asserts the other 5 do NOT also fire -- same
"clean-by-default, tip one" convention as tests/unit/shield/test_checkpoint.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from autotrade.council.news_calendar import NewsEvent
from autotrade.council.order_construction import OrderPlan
from autotrade.council.risk_voice import RiskVoiceConfig, check_risk_voice, get_symbol_currencies

FIELDS = [
    "spread_blocked", "news_blocked", "stop_distance_blocked",
    "session_blocked", "friday_close_blocked", "atr_panic_blocked",
]


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeNewsProvider:
    """Returns `[]` (fetched fine, nothing found) for every currency by
    default -- pass `events_by_currency` to return specific events, or
    `always_none=True` to simulate a fetch failure."""

    def __init__(self, events_by_currency: dict | None = None, always_none: bool = False):
        self._events = events_by_currency or {}
        self._always_none = always_none
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_high_impact_events(self, currency, window_start, window_end):
        self.calls.append((currency, window_start, window_end))
        if self._always_none:
            return None
        return self._events.get(currency, [])


def _non_friday_at_hour(hour: int) -> datetime:
    """A date guaranteed not to be a Friday, at the given server hour --
    computed rather than hardcoded so it doesn't depend on knowing 2026-07-20's
    actual weekday."""
    d = datetime(2026, 7, 20, hour, 0)
    while d.weekday() == 4:
        d += timedelta(days=1)
    return d


def _friday_at_hour(hour: int) -> datetime:
    d = datetime(2026, 7, 20, hour, 0)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


CLEAN_CLOCK_TIME = _non_friday_at_hour(15)  # within the default [14, 18) session


def _plan(stop_distance: float = 5.0) -> OrderPlan:
    return OrderPlan(direction="BUY", entry=100.0, stop_loss=100.0 - stop_distance,
                      take_profit=110.0, stop_distance=stop_distance)


def _check(
    symbol: str = "EURUSD",
    order_plan: OrderPlan | None = None,
    current_spread_points: float = 10.0,
    avg_spread_points_20d: float = 10.0,
    current_atr: float = 10.0,
    avg_atr_20d: float = 10.0,
    news_provider=None,
    clock=None,
    config: RiskVoiceConfig | None = None,
):
    return check_risk_voice(
        symbol=symbol,
        order_plan=order_plan or _plan(),
        current_spread_points=current_spread_points,
        avg_spread_points_20d=avg_spread_points_20d,
        current_atr=current_atr,
        avg_atr_20d=avg_atr_20d,
        news_provider=news_provider or FakeNewsProvider(),
        clock=clock or FixedClock(CLEAN_CLOCK_TIME),
        config=config or RiskVoiceConfig(),
    )


def _assert_only(decision, blocked_field: str | None):
    for field in FIELDS:
        expected = field == blocked_field
        assert getattr(decision, field) is expected, f"{field} expected {expected}"
    assert decision.vetoed is (blocked_field is not None)


def test_clean_state_is_not_vetoed():
    decision = _check()
    _assert_only(decision, None)
    assert decision.reasons == []


def test_condition1_spread_multiple_breach():
    # 20 > 1.5 * 10 = 15.
    decision = _check(current_spread_points=20.0, avg_spread_points_20d=10.0)
    _assert_only(decision, "spread_blocked")
    assert "1.5x" in decision.spread_reason


def test_condition1_spread_xauusd_absolute_breach():
    # 40 > 35-point XAUUSD ceiling, but 40 is NOT > 1.5*30=45 so the
    # multiple sub-condition alone would not have fired -- isolates the
    # XAUUSD-specific absolute threshold.
    decision = _check(symbol="XAUUSD", current_spread_points=40.0, avg_spread_points_20d=30.0)
    _assert_only(decision, "spread_blocked")
    assert "XAUUSD ceiling" in decision.spread_reason


def test_condition1_xauusd_ceiling_does_not_apply_to_other_symbols():
    # Same absolute spread (40) as the XAUUSD case above, but on EURUSD --
    # the 35-point ceiling is XAUUSD-specific and must not fire here, and
    # 40 is not > 1.5*30=45 either.
    decision = _check(symbol="EURUSD", current_spread_points=40.0, avg_spread_points_20d=30.0)
    _assert_only(decision, None)


def test_condition1_spread_multiple_exactly_at_ceiling_is_not_blocked():
    # 15.0 == 1.5 * 10 exactly -- boundary is strict '>', not '>=', same
    # convention as condition 3's stop-distance ceiling.
    decision = _check(current_spread_points=15.0, avg_spread_points_20d=10.0)
    _assert_only(decision, None)


def test_condition1_xauusd_spread_exactly_at_35_points_is_not_blocked():
    # avg_spread_points_20d is large enough (1000) that the multiple
    # sub-condition (1.5x) can't also fire -- isolates the absolute ceiling.
    decision = _check(
        symbol="XAUUSD", current_spread_points=35.0, avg_spread_points_20d=1000.0,
    )
    _assert_only(decision, None)


def test_condition2_news_blocks_when_high_impact_event_in_window():
    provider = FakeNewsProvider(events_by_currency={
        "USD": [NewsEvent(currency="USD", impact="high", event_time=CLEAN_CLOCK_TIME)],
    })
    decision = _check(symbol="EURUSD", news_provider=provider)
    _assert_only(decision, "news_blocked")
    assert "USD" in decision.news_reason


def test_condition2_news_fetch_failure_is_treated_as_there_is_news_and_vetoes():
    # The explicit fail-safe default: provider returns None (fetch failed),
    # not an empty list -- must veto, not pass through as "no news".
    provider = FakeNewsProvider(always_none=True)
    decision = _check(symbol="EURUSD", news_provider=provider)
    _assert_only(decision, "news_blocked")
    assert "fail-safe" in decision.news_reason


def test_condition2_news_empty_list_is_not_a_fetch_failure_and_does_not_veto():
    # An empty list (fetched fine, nothing found) is a genuinely different
    # signal from None (couldn't fetch) -- must NOT veto.
    provider = FakeNewsProvider(events_by_currency={"USD": []})
    decision = _check(symbol="EURUSD", news_provider=provider)
    _assert_only(decision, None)


def test_condition2_only_queries_currencies_relevant_to_the_symbol():
    provider = FakeNewsProvider()
    _check(symbol="EURUSD", news_provider=provider)
    queried_currencies = {call[0] for call in provider.calls}
    assert queried_currencies == {"EUR", "USD"}


def test_condition3_stop_distance_exceeds_atr_ceiling():
    # stop_distance=30 > 2.5 * atr(10) = 25.
    decision = _check(order_plan=_plan(stop_distance=30.0), current_atr=10.0)
    _assert_only(decision, "stop_distance_blocked")
    assert "ATR" in decision.stop_distance_reason


def test_condition3_stop_distance_at_exactly_the_ceiling_is_not_blocked():
    # stop_distance=25 == 2.5 * atr(10) -- boundary is strict '>', not '>='.
    decision = _check(order_plan=_plan(stop_distance=25.0), current_atr=10.0)
    _assert_only(decision, None)


def test_condition4_outside_session_hours_blocks():
    clock = FixedClock(_non_friday_at_hour(10))  # before the default [14, 18) session
    decision = _check(clock=clock)
    _assert_only(decision, "session_blocked")


def test_condition4_inside_session_hours_does_not_block():
    clock = FixedClock(_non_friday_at_hour(14))  # exactly session_start_hour, inclusive
    decision = _check(clock=clock)
    _assert_only(decision, None)


def test_condition4_session_end_hour_is_exclusive_and_blocks():
    # hour == session_end_hour (18) must NOT be treated as still inside the
    # session -- the interval is half-open [start, end), so 18 itself blocks.
    clock = FixedClock(_non_friday_at_hour(18))
    decision = _check(clock=clock)
    _assert_only(decision, "session_blocked")


def test_condition4_hour_just_before_session_end_does_not_block():
    # hour == session_end_hour - 1 (17) is the last hour still inside the
    # session -- confirms the exclusive boundary is exactly at 18, not 17.
    clock = FixedClock(_non_friday_at_hour(17))
    decision = _check(clock=clock)
    _assert_only(decision, None)


def test_condition5_friday_after_close_hour_blocks():
    clock = FixedClock(_friday_at_hour(21))  # after the default 20:00 close cutoff
    decision = _check(clock=clock)
    # Friday 21:00 is also outside the default [14, 18) session, so session
    # fires too -- use a config that widens the session to isolate condition 5.
    config = RiskVoiceConfig(session_start_hour=0, session_end_hour=24)
    decision = _check(clock=clock, config=config)
    _assert_only(decision, "friday_close_blocked")


def test_condition5_friday_before_close_hour_does_not_block():
    clock = FixedClock(_friday_at_hour(15))  # before the 20:00 cutoff, inside the session
    decision = _check(clock=clock)
    _assert_only(decision, None)


def test_condition5_friday_exactly_at_close_hour_blocks():
    # hour == friday_close_hour (20) exactly -- boundary is inclusive ('>='),
    # unlike conditions 1/3/6's strict '>'. Widen the session so condition 4
    # doesn't also fire at hour 20.
    clock = FixedClock(_friday_at_hour(20))
    config = RiskVoiceConfig(session_start_hour=0, session_end_hour=24)
    decision = _check(clock=clock, config=config)
    _assert_only(decision, "friday_close_blocked")


def test_condition5_friday_hour_just_before_close_does_not_block():
    # hour == friday_close_hour - 1 (19) is the last hour still allowed.
    clock = FixedClock(_friday_at_hour(19))
    config = RiskVoiceConfig(session_start_hour=0, session_end_hour=24)
    decision = _check(clock=clock, config=config)
    _assert_only(decision, None)


def test_condition6_atr_panic_breach():
    # 40 > 3 * avg(10) = 30.
    decision = _check(current_atr=40.0, avg_atr_20d=10.0, order_plan=_plan(stop_distance=5.0))
    _assert_only(decision, "atr_panic_blocked")
    assert "3.0x" in decision.atr_panic_reason


def test_condition6_atr_panic_exactly_at_ceiling_is_not_blocked():
    # 30.0 == 3 * avg(10) exactly -- boundary is strict '>', not '>='.
    # order_plan's stop_distance must stay comfortably under condition 3's
    # OWN ceiling (2.5 * this same current_atr=30.0 -> 75.0), so use a small
    # fixed stop_distance to keep condition 3 isolated out.
    decision = _check(current_atr=30.0, avg_atr_20d=10.0, order_plan=_plan(stop_distance=5.0))
    _assert_only(decision, None)


def test_reasons_lists_every_triggered_condition():
    provider = FakeNewsProvider(events_by_currency={
        "USD": [NewsEvent(currency="USD", impact="high", event_time=CLEAN_CLOCK_TIME)],
    })
    decision = _check(
        symbol="EURUSD", current_spread_points=20.0, avg_spread_points_20d=10.0,
        news_provider=provider,
    )
    assert len(decision.reasons) == 2
    assert decision.vetoed is True


def test_callable_twice_clean_both_times_stays_not_vetoed():
    # Structural requirement: check_risk_voice is cheap enough to call twice
    # (signal-time, then again right before order placement) -- confirms two
    # consecutive calls with identical clean inputs both pass.
    first = _check()
    second = _check()
    assert first.vetoed is False
    assert second.vetoed is False


def test_get_symbol_currencies_returns_empty_tuple_for_unlisted_symbol():
    assert get_symbol_currencies("NZDCAD") == ()


def test_get_symbol_currencies_returns_configured_tuple_for_listed_symbol():
    assert get_symbol_currencies("XAUUSD") == ("USD",)


def test_unlisted_symbol_news_condition_fails_safe_and_vetoes():
    # Safety implication, made explicit: a CONFIGURED symbol fail-safe-vetoes
    # on a fetch failure (see test_condition2_news_fetch_failure_is_treated_
    # as_there_is_news_and_vetoes above) -- an UNLISTED symbol now gets the
    # SAME fail-safe veto (no configured currencies to query at all), rather
    # than silently skipping the news condition. This closes the "unconfigured
    # symbol is less safe than a configured one" gap the module docstring
    # used to document.
    provider = FakeNewsProvider(always_none=True)
    decision = _check(symbol="NZDCAD", news_provider=provider)
    _assert_only(decision, "news_blocked")
    assert provider.calls == []  # never even queried -- veto fires before any fetch
    assert "NZDCAD" in decision.news_reason
    assert "_SYMBOL_CURRENCIES" in decision.news_reason


def test_listed_symbol_with_same_always_failing_provider_does_veto():
    # Direct contrast with the unlisted-symbol case above, same provider
    # instance behavior, same clean inputs otherwise -- isolates that a
    # listed symbol still vetoes via the calendar-fetch-failure path (it
    # actually queries the provider), not the no-mapping path.
    provider = FakeNewsProvider(always_none=True)
    decision = _check(symbol="EURUSD", news_provider=provider)
    _assert_only(decision, "news_blocked")
    assert provider.calls != []


def test_recheck_catches_a_newly_appeared_news_event_between_calls():
    # Simulates the Appendix A §1.5 re-check requirement: the first call (at
    # signal-evaluation time) sees no news; between it and the second call
    # (immediately before order placement) an event appears -- the second
    # call must now veto ("stale_signal" is the caller's job to log).
    provider = FakeNewsProvider(events_by_currency={"USD": []})
    first = _check(symbol="EURUSD", news_provider=provider)
    assert first.vetoed is False

    provider._events["USD"] = [NewsEvent(currency="USD", impact="high", event_time=CLEAN_CLOCK_TIME)]
    second = _check(symbol="EURUSD", news_provider=provider)
    assert second.vetoed is True
    assert second.news_blocked is True
