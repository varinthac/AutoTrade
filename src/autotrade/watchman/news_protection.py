"""News protection -- Watchman item 5 (trading_system_summary_v2.md Appendix
A §4.5): "ถ้ามีข่าว high-impact จะมาถึงใน 30 นาที และไม้กำไรอยู่ >= 0.5xR ->
ปิดครึ่งหนึ่ง + ย้าย SL เป็น break-even [adjustable: หรือปิดทั้งไม้]".

Pure decision function, same shape/spirit as `evaluate.evaluate_watchman`:
takes an open position's metadata + current price + a `NewsCalendarProvider`
+ config and returns a decision. The actual `close_position`/
`modify_stop_loss` calls happen in the wiring layer (`watchman/loop.py`),
never here.

**Fail-safe direction -- read before touching this module.** This mirrors
`council/risk_voice.py`'s news-fetch-failure fail-safe
(`council/news_calendar.py`: "ถ้าดึง calendar ไม่ได้ = ถือว่ามีข่าว -> veto"),
but the resulting BEHAVIOR is the opposite polarity, because the two modules
gate opposite KINDS of action:

  - Risk Voice's news condition gates a RISK-INCREASING action (opening a
    brand-new trade). "Can't fetch the calendar" -> assume there IS news ->
    VETO, i.e. skip the risk-increasing action. Erring toward "don't trade"
    is the safe direction there.
  - This module's news protection gates a RISK-REDUCING action (closing
    half of an already-profitable position and moving its stop to
    break-even). "Can't fetch the calendar" -> assume there MIGHT be
    high-impact news -> TRIGGER the protective action (once the position is
    otherwise profitable enough), rather than skip it. Here, erring toward
    "protect the profit" is the safe direction -- SKIPPING protection on a
    fetch failure would be the actually-unsafe choice, since it leaves a
    profitable position fully exposed on an unverifiable chance that real
    high-impact news is imminent. Same underlying instinct ("when in doubt
    about news, assume the worst"), opposite resulting action, because the
    thing being gated is itself opposite in kind (risk-increasing vs.
    risk-reducing).

**Visible, non-buried consequence of the current stub.**
`council/news_calendar.py`'s only shipped implementation,
`StubNewsCalendarProvider`, ALWAYS returns `None` ("couldn't fetch -- no
real calendar connected yet"). Per the fail-safe direction above, that means
this module's protective action fires EVERY SINGLE TIME an open position
clears `profit_threshold_r`, for every symbol, until a real provider
replaces the stub. Unlike Risk Voice's stub-driven "vetoes every trade"
consequence (which blocks ALL new trading), this is NOT catastrophic or
trade-blocking -- it only protects profits earlier than an actually-available
calendar would (a defensible, conservative default, consistent with this
project's "ambiguous = no trade / err toward safety" philosophy). But it is
a real, visible, and constant behavioral consequence worth stating plainly
here rather than leaving as a buried surprise: expect every sufficiently
profitable open position to get its partial-close-and-breakeven treatment
on its very next Watchman cycle, for as long as the stub is in place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from autotrade.council.news_calendar import NewsCalendarProvider
from autotrade.council.risk_voice import get_symbol_currencies
from autotrade.watchman.position_metadata import PositionMetadata


@dataclass(frozen=True)
class NewsProtectionConfig:
    """`config/base.yaml`'s `watchman.news_*` keys (Appendix A §4.5 / §6).
    All values `[adjustable]` per the spec -- `close_mode="all"` is the
    spec's own explicitly-called-out alternative ("หรือปิดทั้งไม้") to the
    default "close half"."""

    news_window_minutes: float = 30.0
    profit_threshold_r: float = 0.5
    close_mode: Literal["half", "all"] = "half"


@dataclass(frozen=True)
class NewsProtectionDecision:
    action: Literal["CLOSE_HALF_AND_BREAKEVEN", "CLOSE_ALL", "NO_ACTION"]
    reason: str


def check_news_protection(
    position_metadata: PositionMetadata,
    current_price: float,
    news_provider: NewsCalendarProvider,
    now: datetime,
    config: NewsProtectionConfig,
) -> NewsProtectionDecision:
    """One decision for one open position. Only ever considers protecting a
    position that is ALREADY profitable by at least `profit_threshold_r` --
    below that, there's nothing worth protecting yet regardless of any
    incoming news, so the (possibly fail-safe-triggered) news check is
    skipped entirely as a cheap short-circuit."""
    if position_metadata.direction == "BUY":
        profit_r = (current_price - position_metadata.entry_price) / position_metadata.initial_stop_distance
    elif position_metadata.direction == "SELL":
        profit_r = (position_metadata.entry_price - current_price) / position_metadata.initial_stop_distance
    else:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {position_metadata.direction!r}")

    if profit_r < config.profit_threshold_r:
        return NewsProtectionDecision(
            action="NO_ACTION",
            reason=f"profit {profit_r:.2f}R below protection threshold {config.profit_threshold_r}R",
        )

    news_incoming, news_reason = _news_incoming(position_metadata.symbol, news_provider, now, config)
    if not news_incoming:
        return NewsProtectionDecision(
            action="NO_ACTION",
            reason=f"profit {profit_r:.2f}R >= {config.profit_threshold_r}R but no high-impact news incoming",
        )

    action = "CLOSE_ALL" if config.close_mode == "all" else "CLOSE_HALF_AND_BREAKEVEN"
    return NewsProtectionDecision(
        action=action,
        reason=f"profit {profit_r:.2f}R >= {config.profit_threshold_r}R and {news_reason}",
    )


def _news_incoming(
    symbol: str, news_provider: NewsCalendarProvider, now: datetime, config: NewsProtectionConfig,
) -> tuple[bool, str]:
    currencies = get_symbol_currencies(symbol)
    if not currencies:
        return True, (
            f"symbol {symbol!r} has no configured news-currency mapping in "
            "risk_voice._SYMBOL_CURRENCIES -- failing safe (assume news may be incoming) "
            "rather than skipping the check (see module docstring's fail-safe direction)"
        )

    window_end = now + timedelta(minutes=config.news_window_minutes)
    for currency in currencies:
        events = news_provider.get_high_impact_events(currency, now, window_end)
        if events is None:
            return True, (
                f"economic calendar unavailable for {currency} -- fail-safe TRIGGERS protection "
                "(see module docstring: the mirror image of Risk Voice's fail-safe veto)"
            )
        if events:
            return True, (
                f"{len(events)} high-impact {currency} news event(s) within "
                f"{config.news_window_minutes:.0f} minutes"
            )

    return False, ""
