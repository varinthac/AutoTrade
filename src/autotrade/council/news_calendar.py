"""Economic-calendar access for the Risk Voice's news veto condition, per
trading_system_summary_v2.md Appendix A §1.5:

    "ข่าว high-impact ของสกุลเงินที่เกี่ยวข้อง ภายใน -45 ถึง +30 นาที
    (ดึงจาก economic calendar อัปเดตทุกเช้า; ถ้าดึง calendar ไม่ได้ = ถือว่ามีข่าว -> veto)"

`NewsCalendarProvider` is a `Protocol` (like `common/clock.Clock`) so
`risk_voice.py` depends only on the interface, never a concrete provider --
spec.md §2.3's dependency-direction invariant. `get_high_impact_events`
returns `list[NewsEvent] | None`, and the `None` case is load-bearing, not
an incidental Optional: it means "the calendar could not be fetched",
distinct from `[]` ("fetched successfully, there is genuinely no
high-impact event in this window"). `risk_voice.check_risk_voice` treats
`None` as "there IS news" and vetoes -- the fail-safe default Appendix A
§1.5 explicitly calls for, not something to soften.

**KNOWN, DELIBERATE LIMITATION -- read before wiring this up anywhere:**
`StubNewsCalendarProvider`, the only implementation shipped so far, ALWAYS
returns `None` ("no real calendar connected yet"). Per the fail-safe rule
above, that means Risk Voice's news condition vetoes EVERY SINGLE trade
until a real provider replaces the stub -- i.e. the Council will not
approve any trade at all while this stub is in place. This is the same
honesty-over-convenience placeholder pattern as `backtest/cost_model.py`'s
`commission_per_lot=0.0` and `shield/correlation.py`'s illustrative
correlation table -- a known, conservative gap, not a bug, consistent with
this project's "ambiguous = no trade" philosophy (Appendix A's opening
principle: "ก้ำกึ่ง = ไม่เทรด"). Candidates checked so far, all confirmed
gated behind a paid plan on the currently-configured keys/tokens (none
integrated yet): Finnhub (`council/finnhub_news_calendar.py` -- HTTP 403),
Financial Modeling Prep (`common/config.load_fmp_api_key` -- HTTP 402),
EODHD (`common/config.load_eodhd_api_token` -- HTTP 403), RapidAPI's
"Ultimate Economic Calendar" (`common/config.load_rapidapi_key` -- HTTP 402
"DEPLOYMENT_DISABLED" on the documented `/economic-events/tradingview`
endpoint, confirmed to be the endpoint's own backend being disabled by the
provider rather than a bad key -- other endpoints on the same host return a
normal RapidAPI-gateway 404). Trading Economics remains unchecked. This must
be revisited before Phase 9 (paper trading) for the system to be practically
testable at scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class NewsEvent:
    """One economic-calendar event. `impact` is the calendar's own
    importance rating (e.g. "high"/"medium"/"low") -- Risk Voice only cares
    about high-impact events (Appendix A §1.5), but the shape carries
    whatever the provider reports rather than pre-filtering here, so a
    future caller (e.g. the Auditor) can distinguish "no high-impact event"
    from "no event of any kind"."""

    currency: str
    impact: str
    event_time: datetime


class NewsCalendarProvider(Protocol):
    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent] | None:
        """High-impact events for `currency` with `event_time` in
        `[window_start, window_end]` (server time, both inclusive). Returns
        `None` if the calendar could not be fetched at all -- see module
        docstring for why that distinction is load-bearing."""
        ...


class StubNewsCalendarProvider:
    """The only `NewsCalendarProvider` implementation shipped so far --
    always returns `None`, honestly simulating "no real calendar connected
    yet". See the module docstring's KNOWN, DELIBERATE LIMITATION section:
    this makes Risk Voice's news check always veto until a real provider is
    wired in."""

    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent] | None:
        return None
