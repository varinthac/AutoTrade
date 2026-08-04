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

This provider is `backtest/engine.py::BacktestConfig`'s DEFAULT for RISK
VOICE's news condition -- used whenever `model_risk_voice_news` is `False`
(the default). Risk Voice's news condition CAN now be modeled against a
real historical calendar instead, via `backtest/historical_news_calendar.
HistoricalNewsCalendarProvider` over EXP-024's real calendar dump, by setting
`BacktestConfig.model_risk_voice_news=True` (see `backtest/engine.py`'s
module docstring) -- the same dataset and provider class Watchman's own news
protection (`watchman/news_protection.py`) already uses, but wired to a
DIFFERENT Council persona and window (Risk Voice's own
`news_blackout_before_min`/`news_blackout_after_min`, condition 2 of 6).
This module's `[]`-always behavior remains the DEFAULT: a deliberate choice
to model Risk Voice's other five conditions
(spread/stop-distance/session/Friday-close/ATR-panic) accurately in
backtests while leaving RISK VOICE's news condition NOT modeled unless
explicitly opted into, rather than letting the news condition's fail-safe
veto silently swallow every other condition's signal.
`NoHistoricalNewsDataProvider` always returns `[]` ("fetched successfully,
no high-impact event") rather than `None` for that reason.
`backtest/report.py`'s envelope records `risk_voice_modeled` (and,
independently, `scripts/run_backtest.py`'s envelope records
`risk_voice_news_modeled`) so a promotion-gate reader always knows which
mode a given run used, the same "never silently pretend to model something
you don't" convention `backtest/cost_model.py`'s `commission_per_lot=0.0`
placeholder uses.
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
