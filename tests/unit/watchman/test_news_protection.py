"""Unit tests for watchman/news_protection.py -- Watchman item 5 (Appendix
A §4.5), including the documented fail-safe-TRIGGERS-protection direction
(the mirror image of Risk Voice's fail-safe-vetoes)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autotrade.council.news_calendar import NewsEvent
from autotrade.watchman.news_protection import NewsProtectionConfig, check_news_protection
from autotrade.watchman.position_metadata import PositionMetadata

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _metadata(direction="BUY", entry_price=100.0, initial_stop_distance=1.0, symbol="XAUUSD"):
    return PositionMetadata(
        ticket=1, symbol=symbol, direction=direction, entry_price=entry_price,
        initial_stop_distance=initial_stop_distance, entry_swing_index=0, opened_at=NOW,
    )


class AllClearProvider:
    def get_high_impact_events(self, currency, window_start, window_end):
        return []


class AlwaysNewsProvider:
    def get_high_impact_events(self, currency, window_start, window_end):
        return [NewsEvent(currency=currency, impact="high", event_time=window_start)]


class UnavailableProvider:
    """The real `StubNewsCalendarProvider`'s behavior -- always "couldn't fetch"."""

    def get_high_impact_events(self, currency, window_start, window_end):
        return None


def test_below_profit_threshold_is_no_action_even_with_news_incoming():
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    decision = check_news_protection(
        metadata, current_price=100.3, news_provider=AlwaysNewsProvider(),  # only 0.3R
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "NO_ACTION"
    assert "below protection threshold" in decision.reason


def test_profitable_but_no_news_is_no_action():
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    decision = check_news_protection(
        metadata, current_price=101.0, news_provider=AllClearProvider(),  # 1.0R
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "NO_ACTION"
    assert "no high-impact news" in decision.reason


def test_profitable_with_news_closes_half_and_breakeven_by_default():
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    decision = check_news_protection(
        metadata, current_price=101.0, news_provider=AlwaysNewsProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "CLOSE_HALF_AND_BREAKEVEN"


def test_profitable_with_news_and_close_mode_all_closes_everything():
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    decision = check_news_protection(
        metadata, current_price=101.0, news_provider=AlwaysNewsProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5, close_mode="all"),
    )
    assert decision.action == "CLOSE_ALL"


def test_sell_direction_profit_computed_correctly():
    metadata = _metadata(direction="SELL", entry_price=100.0, initial_stop_distance=1.0)
    # price fell to 99.0 -- 1.0R profit for a SELL.
    decision = check_news_protection(
        metadata, current_price=99.0, news_provider=AllClearProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "NO_ACTION"
    assert "no high-impact news" in decision.reason  # confirms threshold WAS cleared


def test_invalid_direction_raises_value_error():
    import pytest

    metadata = _metadata(direction="HOLD")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        check_news_protection(
            metadata, current_price=101.0, news_provider=AllClearProvider(),
            now=NOW, config=NewsProtectionConfig(),
        )


# --- Fail-safe direction: the mirror image of Risk Voice's fail-safe veto --


def test_calendar_unavailable_triggers_protection_not_skips_it():
    # StubNewsCalendarProvider's real-world behavior: always None ("couldn't
    # fetch"). Per the documented fail-safe direction, this must TRIGGER
    # protection once profitable, not silently skip it.
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    decision = check_news_protection(
        metadata, current_price=101.0, news_provider=UnavailableProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "CLOSE_HALF_AND_BREAKEVEN"
    assert "fail-safe" in decision.reason.lower()


def test_stub_always_protects_once_profitable_documented_consequence():
    # Explicit test of the module docstring's stated consequence: with the
    # stub wired in, EVERY sufficiently profitable position gets protected,
    # for every symbol -- not a corner case, the default behavior.
    for symbol in ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY"):
        metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0, symbol=symbol)
        decision = check_news_protection(
            metadata, current_price=100.6, news_provider=UnavailableProvider(),
            now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
        )
        assert decision.action == "CLOSE_HALF_AND_BREAKEVEN", symbol


def test_unmapped_symbol_currency_fails_safe_to_protection():
    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0, symbol="UNKNOWNSYMBOL")
    decision = check_news_protection(
        metadata, current_price=101.0, news_provider=AllClearProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5),
    )
    assert decision.action == "CLOSE_HALF_AND_BREAKEVEN"
    assert "no configured news-currency mapping" in decision.reason


def test_news_window_uses_configured_minutes_not_risk_voice_window():
    # News protection's window is centered on "now -> now+window_minutes"
    # (news arriving soon), unlike Risk Voice's centered blackout window --
    # confirm the exact window bounds passed to the provider.
    captured = {}

    class SpyProvider:
        def get_high_impact_events(self, currency, window_start, window_end):
            captured["window_start"] = window_start
            captured["window_end"] = window_end
            return []

    metadata = _metadata(entry_price=100.0, initial_stop_distance=1.0)
    check_news_protection(
        metadata, current_price=101.0, news_provider=SpyProvider(),
        now=NOW, config=NewsProtectionConfig(profit_threshold_r=0.5, news_window_minutes=30.0),
    )

    assert captured["window_start"] == NOW
    assert captured["window_end"] == NOW + timedelta(minutes=30.0)
