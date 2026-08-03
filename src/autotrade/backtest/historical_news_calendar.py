"""Historical replay `NewsCalendarProvider` for backtesting Watchman's news
protection (`watchman/news_protection.py`, live "mode A") against the REAL
economic calendar instead of leaving it genuinely unmodeled -- see
`backtest/engine.py`'s module docstring and EXP-024's escalation
(`experiments/experiments_log.md`, `### EXP-024 RESULTS` §8(1): "model news
protection in `backtest/engine.py` permanently... an optional
`NewsCalendarProvider` on `BacktestConfig`").

Backtest-only, same dependency-direction guardrail as `backtest/news_stub.py`
(spec.md §2.3): nothing under `backtest/` is imported by live code, so this
module structurally cannot leak into the live/paper/sandbox path.

Reads `scripts/build_backtest_calendar.py`'s output (default
`data/historical/news_calendar_backtest.csv`, gitignored -- regenerate with
that script) -- the SAME 7-column schema `mql5/NewsCalendarExporter.mq5`
writes (`event_time, currency, importance, event_name, forecast, previous,
actual`, one leading `#`-prefixed comment line), parsed with
`council.mql5_calendar_provider.parse_export_csv` -- the PRODUCTION parser,
so this provider cannot drift from what live reads (same reasoning
`experiments/exp024_real_calendar_harness.py`'s `load_calendar` already
established).

Loaded ONCE into memory at construction (sorted, indexed per currency,
high-impact only -- the only kind `get_high_impact_events` is ever asked
about), so per-bar queries during a backtest replay are fast (`bisect` over
an already-sorted list, not a linear scan or a re-parse).

**`None`-vs-`[]` contract, restated because it is load-bearing here too --
see `council/news_calendar.py`'s module docstring.** `get_high_impact_events`
NEVER returns `None` from this provider: the whole calendar is already
loaded and validated at construction, so there is no per-query "fetch
failure" mode left to report -- any structural problem with the input file
is raised eagerly in `__init__` instead. This means a `backtest/engine.py`
replay wired to this provider can NEVER exercise `news_protection.py`'s
fail-safe branch (`_news_incoming`'s "economic calendar unavailable" case)
-- EXP-024 limitation (iii) (`experiments/experiments_log.md`): live fires
protection whenever ITS calendar read fails (measured there: 17 hourly
evaluations vetoed in 13 days, including one 14-hour outage), so a backtest
run against this provider measures a LOWER BOUND on live's true trigger
frequency, not the whole of it.
"""
from __future__ import annotations

import bisect
import logging
from datetime import datetime
from pathlib import Path

from autotrade.council.mql5_calendar_provider import parse_export_csv
from autotrade.council.news_calendar import NewsEvent

logger = logging.getLogger(__name__)

_HIGH_IMPACT = "high"
_EVENT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class HistoricalNewsCalendarProvider:
    """A `NewsCalendarProvider` backed by a pre-built, normalised historical
    calendar CSV -- see module docstring. Construction reads and indexes the
    whole file once; `get_high_impact_events` never touches disk again."""

    def __init__(self, calendar_csv_path: Path | str) -> None:
        path = Path(calendar_csv_path)
        text = path.read_text(encoding="ascii", errors="replace")
        rows = parse_export_csv(text)
        if rows is None:
            raise ValueError(
                f"{path} is structurally unparseable by parse_export_csv -- was it built by "
                "scripts/build_backtest_calendar.py?"
            )

        by_currency: dict[str, list[datetime]] = {}
        for row in rows:
            if row["importance"].lower() != _HIGH_IMPACT:
                continue
            try:
                event_time = datetime.strptime(row["event_time"], _EVENT_TIME_FORMAT)
            except ValueError:
                logger.warning("HistoricalNewsCalendarProvider: skipping malformed row %r", row)
                continue
            by_currency.setdefault(row["currency"], []).append(event_time)

        self._by_currency: dict[str, list[datetime]] = {
            currency: sorted(times) for currency, times in by_currency.items()
        }

    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent]:
        """High-impact `currency` events with `event_time` in `[window_start,
        window_end]` (both inclusive -- same bounds `MQL5CalendarProvider`
        uses). ALWAYS a list, NEVER `None` -- see module docstring."""
        times = self._by_currency.get(currency, [])
        lo = bisect.bisect_left(times, window_start)
        hi = bisect.bisect_right(times, window_end)
        return [NewsEvent(currency=currency, impact=_HIGH_IMPACT, event_time=t) for t in times[lo:hi]]
