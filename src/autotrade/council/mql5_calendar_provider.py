"""Real `NewsCalendarProvider` implementation backed by MT5's own built-in,
free economic calendar -- resolves the long-standing blocker documented in
`council/news_calendar.py`'s module docstring (Finnhub/FMP/EODHD/RapidAPI/
Alpha Vantage all confirmed gated or wrong-shaped). MetaTrader 5 itself ships
a real economic calendar (impact level, currency, event time, forecast/
previous/actual) via 6 MQL5-only functions the official `MetaTrader5` Python
package does not expose -- the standard workaround is an MQL5 program
running inside the terminal that exports the calendar to a file, read here
from Python. `mql5/NewsCalendarExporter.mq5` (this repo's version-controlled
source for that MQL5 Service -- see its own module docstring for the MQL5
side, including the exact calendar function signatures used, confirmed
against MQL5's real docs) periodically writes the calendar to a CSV file in
MT5's shared Common Files folder (`TERMINAL_COMMONDATA_PATH\\Files\\`).

**Why this provider needs `mt5_session()`, unlike every other
`NewsCalendarProvider` implementation.** `FinnhubNewsCalendarProvider`
(`council/finnhub_news_calendar.py`) is a pure HTTP client with zero MT5
dependency. This provider only needs MT5 for one narrow reason: resolving
where the shared Common Files folder actually lives on this machine
(`mt5.terminal_info().commondata_path`, the same accessor
`common/mt5_connection.py`'s module docstring already relies on elsewhere in
this codebase) -- there is no portable, MT5-free way to know that path
otherwise. To keep everything else independently testable without a live
MT5 connection, that one MT5-touching step is isolated in
`resolve_commondata_path()` below; `MQL5CalendarProvider` itself takes the
already-resolved path as a plain string in its constructor and never
touches `mt5` again after that -- the actual file-reading/parsing/filtering
logic (`_read_export_file`, `_parse_export_csv`, `_row_to_event`) is pure
Python with no MT5 dependency at all, and is what `tests/unit/council/
test_mql5_calendar_provider.py` exercises directly.

**Time convention -- deliberately NOT UTC-tagged, unlike
`finnhub_news_calendar.py`.** MQL5's calendar functions all report times in
the trade server's own timezone (`TimeTradeServer()`), confirmed against
MQL5's real docs -- see `mql5/NewsCalendarExporter.mq5`'s module docstring.
This repo's actual production clock for anything compared against
`NewsEvent.event_time` (`orchestrator/shadow_loop.ShadowLoop`'s
`clock=ServerClock(...)`, wired in `scripts/run_shadow_loop.py`) is ALSO
naive MT5 broker server time (`common/mt5_time.ServerClock` -- naive, not
UTC, not local, per its own docstring), not wall-clock UTC. So unlike
`FinnhubNewsCalendarProvider` (which attaches `timezone.utc` to its parsed
timestamps), this provider returns naive `datetime`s as-is, matching the
time reference `check_risk_voice`/`check_news_protection`'s `now` argument
actually uses in this codebase's current wiring. Attaching a UTC tzinfo here
would be actively wrong (comparing it against `ServerClock`'s naive `now`
raises `TypeError: can't compare offset-naive and offset-aware datetimes`),
not just an unverified assumption the way it is in Finnhub's provider.

**Fail-safe / staleness.** Same `None`-means-"couldn't fetch" contract as
every other `NewsCalendarProvider` (`council/news_calendar.py`). Returns
`None` if the export file is missing, unreadable, structurally unparseable,
or its own OS last-modified time is older than `staleness_threshold_minutes`
-- the exporting Service not running (never started, or the terminal
restarted without it) must fail safe exactly like a real fetch failure, not
silently serve arbitrarily-old data. Individual malformed CSV rows are
skipped and logged rather than failing the whole read.

**2026-07-25: staleness now alerts, not just logs.** A real VPS incident
(the exporter Service didn't survive an MT5 terminal restart -- it has no
"start when the platform starts" flag set, a one-time MT5-side setting, not
a code fix) silently fail-safe-vetoed every USD signal for hours with
nothing but a log line nobody was watching. Staleness now also fires a
Telegram `notify()`, transition-only (first time crossing stale, and once
more when a fresh read succeeds again) -- same shape as
`common/connectivity_watchdog.py`'s DOWN/UP alerting, deliberately in-memory
(not file-persisted state) since this provider is instantiated once per
`run_shadow_loop.py` process, unlike that watchdog's cross-invocation
design.

**No TTL cache, unlike `finnhub_news_calendar.py`.** Finnhub's cache exists
to dodge a real external rate limit -- there is no such limit here (a local
file read), and `risk_voice.py`'s own module docstring calls out that the
order-send-time re-check's one genuinely-real value today is that
`news_provider` "IS re-queried fresh on each call"; adding a cache here
would blunt that for no real benefit. File I/O is cheap enough that
re-reading/re-parsing on every call is the simpler, more correct choice.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from autotrade.common.clock import Clock, RealClock
from autotrade.council.news_calendar import NewsEvent
from autotrade.notify.telegram import notify

logger = logging.getLogger(__name__)

# Must match mql5/NewsCalendarExporter.mq5's EXPORT_FILENAME exactly.
EXPORT_FILENAME = "AutoTradeNewsCalendar.csv"

_HIGH_IMPACT = "high"
_EVENT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_CSV_COLUMNS = ("event_time", "currency", "importance", "event_name", "forecast", "previous", "actual")


def resolve_commondata_path() -> str | None:
    """MT5's shared Common Files folder (`TERMINAL_COMMONDATA_PATH`), where
    `mql5/NewsCalendarExporter.mq5` writes its export -- requires an active
    `mt5_session()`. Returns `None` (fail-safe) if there is no active
    connection or `terminal_info()` otherwise fails, rather than raising --
    the caller (`scripts/run_shadow_loop.py`'s `build_news_provider`) treats
    that as "MQL5 calendar unavailable, fall back to the next candidate"."""
    terminal_info = mt5.terminal_info()
    if terminal_info is None:
        code, desc = mt5.last_error()
        logger.warning(
            "MQL5CalendarProvider: mt5.terminal_info() failed: [%s] %s -- MQL5 calendar unavailable",
            code, desc,
        )
        return None
    return terminal_info.commondata_path


@dataclass(frozen=True)
class _ExportFile:
    text: str
    mtime: datetime


def _read_export_file(path: Path) -> _ExportFile | None:
    """Reads the export file's text and OS last-modified time (used for the
    staleness check -- see module docstring for why the file's own mtime,
    not an embedded timestamp, is the source of truth). Returns `None`
    (fail-safe) if the file is missing or unreadable -- never raises."""
    try:
        mtime_epoch = path.stat().st_mtime
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        logger.warning("MQL5CalendarProvider: could not read export file %s (%s)", path, exc)
        return None
    return _ExportFile(text=text, mtime=datetime.fromtimestamp(mtime_epoch, tz=timezone.utc))


def _parse_export_csv(csv_text: str) -> list[dict[str, str]] | None:
    """Pure, MT5-free: parses the exporter's CSV text into raw row dicts.
    The first line is a `# generated_at_server_time=...` comment (see
    `mql5/NewsCalendarExporter.mq5`), skipped here.

    Returns `None` if the file's structure itself is unparseable (missing
    or unexpected header -- `NewsCalendarExporter.mq5` always writes the
    comment + header lines, even when zero events fall in its export
    window, so a missing/wrong header means something is actually broken,
    not "no events"). Individual malformed rows (wrong column count) within
    an otherwise-valid file are skipped and logged, not treated as a
    whole-file failure -- the returned list can legitimately be empty."""
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]

    reader = csv.DictReader(lines)
    if reader.fieldnames != list(_CSV_COLUMNS):
        logger.warning(
            "MQL5CalendarProvider: unexpected/missing CSV header %r (expected %r) -- "
            "treating export file as unparseable",
            reader.fieldnames, _CSV_COLUMNS,
        )
        return None

    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            logger.warning("MQL5CalendarProvider: skipping malformed CSV row (wrong column count): %r", row)
            continue
        rows.append(row)
    return rows


def _row_to_event(row: dict[str, str]) -> NewsEvent | None:
    """One raw CSV row -> `NewsEvent`, or `None` if the row can't be parsed
    (logged here, skipped by the caller -- never raises)."""
    try:
        event_time = datetime.strptime(row["event_time"], _EVENT_TIME_FORMAT)
        currency = row["currency"]
        importance = row["importance"]
        if not currency or not importance:
            raise ValueError("empty currency/importance field")
    except (KeyError, ValueError) as exc:
        logger.warning("MQL5CalendarProvider: skipping malformed calendar row %r (%s)", row, exc)
        return None
    return NewsEvent(currency=currency, impact=importance, event_time=event_time)


class MQL5CalendarProvider:
    """Real `NewsCalendarProvider` backed by `mql5/NewsCalendarExporter.mq5`'s
    CSV export. See module docstring for the fail-safe/staleness contract,
    the naive-server-time convention, and why there is no TTL cache."""

    def __init__(
        self,
        commondata_path: str | Path,
        clock: Clock | None = None,
        filename: str = EXPORT_FILENAME,
        staleness_threshold_minutes: float = 10.0,
    ) -> None:
        """`commondata_path` is MT5's shared Common Files *parent* folder
        (i.e. `mt5.terminal_info().commondata_path` / `resolve_commondata_path()`'s
        return value -- the ".../Terminal/Common" folder, NOT
        ".../Common/Files"); the exporter's `FILE_COMMON` flag places files
        under that folder's `Files\\` subfolder, appended here.
        `staleness_threshold_minutes` defaults to 10 -- 2x
        `NewsCalendarExporter.mq5`'s own default 5-minute
        `InpExportIntervalMinutes` -- if that input is changed on the MQL5
        side, update this default (or pass an explicit value) to match.

        Raises `ValueError` if `clock.now()` is timezone-naive: the
        staleness check below compares it against `export.mtime`, which is
        always timezone-aware UTC (`_read_export_file`), so a naive clock
        (e.g. `common/mt5_time.ServerClock`, unlike the aware `RealClock`)
        would otherwise only surface as a `TypeError` the first time
        `get_high_impact_events()` is actually called -- validating eagerly
        here surfaces the misconfiguration immediately at wiring time
        instead."""
        self._export_path = Path(commondata_path) / "Files" / filename
        self._clock = clock or RealClock()
        self._staleness_threshold = timedelta(minutes=staleness_threshold_minutes)
        self._alerted_stale = False

        if self._clock.now().tzinfo is None:
            raise ValueError(
                "MQL5CalendarProvider: clock.now() returned a timezone-naive datetime -- "
                "this provider compares it against the export file's timezone-aware UTC "
                "mtime, so the clock must be timezone-aware too. Was a naive Clock like "
                "ServerClock passed here instead of an aware one like RealClock?"
            )

    def get_high_impact_events(
        self, currency: str, window_start: datetime, window_end: datetime
    ) -> list[NewsEvent] | None:
        export = _read_export_file(self._export_path)
        if export is None:
            return None

        try:
            age = self._clock.now() - export.mtime
        except TypeError as exc:
            logger.warning(
                "MQL5CalendarProvider: could not compute export file staleness (%s) -- "
                "likely a clock/mtime timezone-awareness mismatch (was a naive Clock like "
                "ServerClock passed to this provider instead of an aware one like "
                "RealClock?) -- failing safe (None)",
                exc,
            )
            return None
        if age > self._staleness_threshold:
            logger.warning(
                "MQL5CalendarProvider: export file %s is stale (last written %s ago, threshold %s) -- "
                "failing safe (None). Is the NewsCalendarExporter MQL5 Service running in the terminal?",
                self._export_path, age, self._staleness_threshold,
            )
            if not self._alerted_stale:
                notify(
                    f"[AutoTrade] \U0001F6A8 Economic calendar export is STALE (last written {age} ago) -- "
                    "every news-blackout check is now fail-safe VETOING signals for ALL currencies until "
                    "this recovers. Is the NewsCalendarExporter Service still running in the MT5 terminal's "
                    "Navigator panel?"
                )
                self._alerted_stale = True
            return None

        if self._alerted_stale:
            logger.warning("MQL5CalendarProvider: export file is fresh again -- calendar recovered.")
            notify("[AutoTrade] ✅ Economic calendar export is fresh again -- news-blackout checks resumed normally.")
            self._alerted_stale = False

        rows = _parse_export_csv(export.text)
        if rows is None:
            return None

        events: list[NewsEvent] = []
        for row in rows:
            event = _row_to_event(row)
            if event is None:
                continue
            if event.currency != currency:
                continue
            if event.impact.lower() != _HIGH_IMPACT:
                continue
            if window_start <= event.event_time <= window_end:
                events.append(event)

        logger.info(
            "MQL5CalendarProvider: %d high-impact %s event(s) in [%s, %s]",
            len(events), currency, window_start, window_end,
        )
        return events
