"""Real `NewsCalendarProvider` implementation backed by Finnhub's economic
calendar (finnhub.io), per `council/news_calendar.py`'s module docstring
"Candidates being considered for a real implementation" list.

Endpoint confirmed against Finnhub's live API reference
(finnhub.io/docs/api/calendar-economic) on 2026-07-20:

    GET https://finnhub.io/api/v1/calendar/economic
        ?from=YYYY-MM-DD&to=YYYY-MM-DD&token=<api_key>

    Response: {"economicCalendar": [
        {"actual": 8.4, "country": "AU", "estimate": 6.9,
         "event": "Australia - Current Account Balance", "impact": "low",
         "prev": 1, "time": "2020-06-02 01:30:00", "unit": "AUD"},
        ...
    ]}

`country` is a 2-letter code (Finnhub's own `/country` endpoint's `code2`
field confirms e.g. `"US"` <-> currency `"USD"`, `"GB"` <-> `"GBP"`) -- see
`_CURRENCY_TO_COUNTRY` below for this provider's currency-to-country mapping.
`impact` is a free-text rating; this provider treats `"high"` (case-
insensitive) as high-impact, everything else as not. `time` is naive
`YYYY-MM-DD HH:MM:SS` with no timezone field in the schema or docs; this
provider assumes it is UTC (the common convention for this kind of feed, and
consistent with `common/clock.RealClock` -- the only `Clock` this codebase's
`council/` ever compares against, per spec.md §2.3) and attaches `timezone.utc`
so it can be compared against `risk_voice.py`'s (tz-aware) window bounds --
**unconfirmed against a live response** (see the premium-gating note below);
re-verify once the endpoint is actually reachable.

**PREMIUM-GATED, UNVERIFIED ON THE FREE TIER -- confirmed live 2026-07-20:**
Finnhub's own API reference marks `/calendar/economic` with
`"premium": "Premium Access Required"` (unlike e.g. `/quote` or `/country`,
which show `"premium": null`). A live call against this project's
currently-configured `FINNHUB_API_KEY` was made to confirm this directly
(not just inferred from the docs metadata) and returned:

    HTTP 403 {"error": "You don't have access to this resource."}

...while the same key succeeded (HTTP 200) against a known free-tier
endpoint (`/quote`), confirming the key itself is valid and the 403 is
specifically about this endpoint's tier gating, not a bad key. This class is
built correctly against the confirmed real request/response shape above and
is ready to use once/if the Finnhub account is upgraded to a plan that
includes economic-calendar access -- but as of today, in real usage,
`get_high_impact_events` will always return `None` here (403 -> the
fail-safe "couldn't fetch" path), the same practical end result as
`StubNewsCalendarProvider`, just for a real, verified reason instead of a
hardcoded stub. Re-verify against a live call once the plan changes.

**Rate-limit mitigation, not a correctness feature:** Finnhub's free/lower
tiers cap requests per minute, and Risk Voice's news condition can call this
provider twice per trade attempt (signal-time + order-send-time re-check,
per `risk_voice.py`'s module docstring) for every currency a symbol touches.
A full day's economic calendar does not change minute-to-minute, so this
provider caches each `(from_date, to_date)` query's raw response in-memory
for `cache_ttl_minutes` (default 15) before re-fetching. This is purely to
avoid hammering the API within a short polling window -- it does not affect
correctness of the "fetch failed -> None" contract (a cached failure is NOT
cached; only a successful fetch is cached, so a transient outage doesn't get
"stuck" as a false all-clear for the TTL window). The cache's own TTL check
reads time via an injected `Clock` (defaulting to `RealClock`), not a direct
`datetime.now()`/`utcnow()` call -- spec.md §2.3's "no direct OS clock reads"
invariant names `council/` as one of the restricted packages, and this class
lives there, so it stays consistent even though the TTL itself is an I/O
optimization rather than trading-decision logic.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from autotrade.common.clock import Clock, RealClock
from autotrade.council.news_calendar import NewsEvent

logger = logging.getLogger(__name__)

_BASE_URL = "https://finnhub.io/api/v1/calendar/economic"

# Finnhub's economic-calendar `country` field uses 2-letter codes (confirmed
# via Finnhub's own `/country` endpoint: code2 "US" <-> currencyCode "USD",
# code2 "GB" <-> currencyCode "GBP"). "EU" for EUR and "JP" for JPY follow
# the same ISO-3166-ish convention but could not be independently confirmed
# against a live `/calendar/economic` response (see module docstring's
# premium-gating note) -- re-verify once that endpoint is actually
# reachable. Only currencies `risk_voice._SYMBOL_CURRENCIES` can request are
# listed; a currency missing here fails safe (see `get_high_impact_events`).
_CURRENCY_TO_COUNTRY: dict[str, str] = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
}

_HIGH_IMPACT = "high"


class FinnhubNewsCalendarProvider:
    """Real `NewsCalendarProvider` backed by Finnhub's economic calendar.
    See module docstring for the confirmed endpoint shape, the premium-tier
    caveat, and the TTL-cache rate-limit mitigation."""

    def __init__(
        self,
        api_key: str,
        clock: Clock | None = None,
        timeout_sec: float = 8.0,
        cache_ttl_minutes: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._clock = clock or RealClock()
        self._timeout_sec = timeout_sec
        self._cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._cache: dict[tuple[str, str], tuple[datetime, list[dict]]] = {}

    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent] | None:
        country = _CURRENCY_TO_COUNTRY.get(currency)
        if country is None:
            logger.warning(
                "FinnhubNewsCalendarProvider: no country mapping for currency %r "
                "(_CURRENCY_TO_COUNTRY) -- failing safe (None)",
                currency,
            )
            return None

        from_date = window_start.date().isoformat()
        to_date = window_end.date().isoformat()

        raw_events = self._fetch_calendar(from_date, to_date)
        if raw_events is None:
            return None

        events: list[NewsEvent] = []
        for item in raw_events:
            try:
                if item.get("country") != country:
                    continue
                impact = item.get("impact")
                if not isinstance(impact, str) or impact.lower() != _HIGH_IMPACT:
                    continue
                event_time = datetime.strptime(
                    item["time"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.warning(
                    "FinnhubNewsCalendarProvider: skipping malformed calendar entry %r (%s)",
                    item, exc,
                )
                continue
            if window_start <= event_time <= window_end:
                events.append(NewsEvent(currency=currency, impact=impact, event_time=event_time))

        logger.info(
            "FinnhubNewsCalendarProvider: %d high-impact %s event(s) in [%s, %s]",
            len(events), currency, window_start, window_end,
        )
        return events

    def _fetch_calendar(self, from_date: str, to_date: str) -> list[dict] | None:
        cache_key = (from_date, to_date)
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_at, cached_events = cached
            if self._clock.now() - cached_at < self._cache_ttl:
                return cached_events

        events = self._fetch_calendar_uncached(from_date, to_date)
        if events is not None:
            self._cache[cache_key] = (self._clock.now(), events)
        return events

    def _fetch_calendar_uncached(self, from_date: str, to_date: str) -> list[dict] | None:
        query = urllib.parse.urlencode({"from": from_date, "to": to_date, "token": self._api_key})
        url = f"{_BASE_URL}?{query}"

        try:
            with urllib.request.urlopen(url, timeout=self._timeout_sec) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            reason = "rate-limited" if exc.code == 429 else "HTTP error"
            logger.warning(
                "FinnhubNewsCalendarProvider: calendar fetch failed (%s): HTTP %d %s -- "
                "failing safe (None)",
                reason, exc.code, exc.reason,
            )
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "FinnhubNewsCalendarProvider: calendar fetch failed (network/timeout): %s -- "
                "failing safe (None)",
                exc,
            )
            return None

        try:
            payload = json.loads(body)
            economic_calendar = payload["economicCalendar"]
            if not isinstance(economic_calendar, list):
                raise TypeError(f"'economicCalendar' is {type(economic_calendar).__name__}, not a list")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "FinnhubNewsCalendarProvider: calendar fetch failed (malformed JSON response): %s -- "
                "failing safe (None)",
                exc,
            )
            return None

        return economic_calendar
