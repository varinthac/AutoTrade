"""A `NewsCalendarProvider` for offline backtesting ONLY -- must never be
wired into any live/paper/sandbox code path (see spec.md §2.3's dependency-
direction invariant: nothing under `backtest/` is imported by live code, so
placing this here structurally prevents that misuse rather than just
documenting against it).

`council/news_calendar.py`'s `StubNewsCalendarProvider` always returns
`None` ("calendar unavailable"), which `risk_voice.check_risk_voice`'s
fail-safe rule treats as "there IS news" -> veto -- the right, conservative
choice for LIVE trading with no real calendar wired in yet, but useless for
backtesting: it would veto every single historical trade, making Risk Voice
appear in a backtest as "always blocks everything" rather than modeling its
actual conditions.

This provider exists for RISK VOICE's news condition specifically, which is
STILL not wired to a historical calendar (`council/risk_voice.py`'s "Known
gap Phase 6b" note) -- a separate, deliberately still-open gap from
Watchman's own news protection (`watchman/news_protection.py`), which CAN
now be modeled via `backtest/historical_news_calendar.
HistoricalNewsCalendarProvider` over EXP-024's real calendar dump (see
`backtest/engine.py`'s module docstring). This module's `[]`-always
behavior is unrelated to that dataset's existence or absence; it is a
deliberate choice to model Risk Voice's other five conditions
(spread/stop-distance/session/Friday-close/ATR-panic) accurately in
backtests while leaving RISK VOICE's news condition NOT modeled, rather than
letting the news condition's fail-safe veto silently swallow every other
condition's signal. `NoHistoricalNewsDataProvider` always returns `[]`
("fetched successfully, no high-impact event") rather than `None` for that
reason. `backtest/report.py`'s envelope records `risk_voice_modeled` so a
promotion-gate reader always knows this limitation applies, the same
"never silently pretend to model something you don't" convention
`backtest/cost_model.py`'s `commission_per_lot=0.0` placeholder uses.
"""
from __future__ import annotations

from datetime import datetime

from autotrade.council.news_calendar import NewsEvent


class NoHistoricalNewsDataProvider:
    """Always reports "no high-impact event found" -- see module docstring
    for why this is a deliberate, backtest-only limitation, not a claim that
    no news ever actually occurred in the replayed historical window."""

    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent] | None:
        return []
