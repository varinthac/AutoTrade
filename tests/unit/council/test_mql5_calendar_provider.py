"""Tests for council/mql5_calendar_provider.py -- MQL5CalendarProvider, the
real `NewsCalendarProvider` implementation backed by
mql5/NewsCalendarExporter.mq5's CSV export. Only `resolve_commondata_path()`
touches MT5 (`mt5.terminal_info()`) and is mocked below; everything else
works against a real temp-directory CSV file with no mocking of the
file-reading/parsing logic itself -- same "mock the boundary, not the
logic" convention as test_finnhub_news_calendar.py mocking
`urllib.request.urlopen`.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autotrade.council import mql5_calendar_provider as mcp
from autotrade.council.mql5_calendar_provider import EXPORT_FILENAME, MQL5CalendarProvider, _CSV_COLUMNS


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 7, 20, 12, 0)  # naive server time -- module's own convention
WINDOW_END = datetime(2026, 7, 20, 13, 0)

_HEADER = "event_time,currency,importance,event_name,forecast,previous,actual"
_COMMENT = "# generated_at_server_time=2026-07-20 10:00:00"


def _row(
    event_time: str = "2026-07-20 12:30:00", currency: str = "USD", importance: str = "high",
    name: str = "US CPI", forecast: str = "3.1", previous: str = "3.0", actual: str = "",
) -> str:
    return f"{event_time},{currency},{importance},{name},{forecast},{previous},{actual}"


def _write_export(tmp_path: Path, rows: list[str], mtime: datetime, text: str | None = None) -> Path:
    files_dir = tmp_path / "Files"
    files_dir.mkdir(parents=True, exist_ok=True)
    path = files_dir / EXPORT_FILENAME
    content = text if text is not None else "\n".join([_COMMENT, _HEADER, *rows]) + "\n"
    path.write_text(content, encoding="ascii")
    epoch = mtime.timestamp()
    os.utime(path, (epoch, epoch))
    return path


def _provider(tmp_path: Path, clock=None, **kwargs) -> MQL5CalendarProvider:
    return MQL5CalendarProvider(tmp_path, clock=clock or FixedClock(NOW), **kwargs)


def test_successful_fetch_with_high_impact_events_found(tmp_path):
    _write_export(tmp_path, [_row()], mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].currency == "USD"
    assert events[0].impact == "high"
    assert events[0].event_time == datetime(2026, 7, 20, 12, 30, 0)


def test_no_matching_events_returns_empty_list_not_none(tmp_path):
    _write_export(tmp_path, [], mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events == []
    assert events is not None


def test_missing_file_returns_none(tmp_path):
    provider = _provider(tmp_path)  # nothing written at all

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_file_just_past_staleness_threshold_returns_none(tmp_path):
    stale_mtime = NOW - timedelta(minutes=10, seconds=1)
    _write_export(tmp_path, [_row()], mtime=stale_mtime)
    provider = _provider(tmp_path, staleness_threshold_minutes=10.0)

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_file_within_staleness_threshold_is_still_used(tmp_path):
    fresh_mtime = NOW - timedelta(minutes=9, seconds=59)
    _write_export(tmp_path, [_row()], mtime=fresh_mtime)
    provider = _provider(tmp_path, staleness_threshold_minutes=10.0)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1


def test_file_exactly_at_staleness_threshold_is_still_used(tmp_path):
    """Pins the boundary itself (not "just past" / "just within" as the
    existing tests do, but age == threshold to the second): the code uses
    `age > threshold` (strict), so an export exactly `staleness_threshold_minutes`
    old is inclusive -- still considered fresh, not stale."""
    boundary_mtime = NOW - timedelta(minutes=10)
    _write_export(tmp_path, [_row()], mtime=boundary_mtime)
    provider = _provider(tmp_path, staleness_threshold_minutes=10.0)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1


def test_long_stale_file_returns_none(tmp_path):
    stale_mtime = NOW - timedelta(days=3)
    _write_export(tmp_path, [_row()], mtime=stale_mtime)
    provider = _provider(tmp_path)

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_malformed_rows_are_skipped_individually_not_fatal(tmp_path):
    rows = [
        _row(event_time="not-a-date"),  # unparseable timestamp
        "USD,high,onlythreefields",  # too few columns (missing trailing fields)
        _row() + ",extra,columns",  # too many columns
        _row(event_time="2026-07-20 12:15:00", name="Good Event"),  # the only valid row
    ]
    _write_export(tmp_path, rows, mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].event_time == datetime(2026, 7, 20, 12, 15, 0)


def test_unparseable_header_returns_none(tmp_path):
    _write_export(tmp_path, [], mtime=NOW, text="this is not a calendar csv at all\n")
    provider = _provider(tmp_path)

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_header_with_columns_in_wrong_order_returns_none(tmp_path):
    """Pins the column-order-drift risk explicitly: `_parse_export_csv`
    compares `reader.fieldnames` against the exact tuple `_CSV_COLUMNS`
    (order-sensitive), so a header carrying the right *names* but in a
    different order than mql5/NewsCalendarExporter.mq5's own
    `FileWrite(handle, "event_time", "currency", ...)` call must be treated
    as unparseable (None), not silently misaligned column-by-column."""
    scrambled_header = "currency,event_time,importance,event_name,forecast,previous,actual"
    text = "\n".join([_COMMENT, scrambled_header, _row()]) + "\n"
    _write_export(tmp_path, [], mtime=NOW, text=text)
    provider = _provider(tmp_path)

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_header_with_missing_column_returns_none(tmp_path):
    """A header that's simply missing a column (e.g. the MQL5 and Python
    sides drift out of sync after an edit to one side only) must also fail
    safe rather than silently reinterpreting later columns."""
    short_header = "event_time,currency,importance,event_name,forecast,previous"  # no 'actual'
    text = "\n".join([_COMMENT, short_header, _row()]) + "\n"
    _write_export(tmp_path, [], mtime=NOW, text=text)
    provider = _provider(tmp_path)

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


def test_currency_filtering_excludes_other_currencies(tmp_path):
    rows = [
        _row(currency="GBP", event_time="2026-07-20 12:30:00"),
        _row(currency="USD", event_time="2026-07-20 12:15:00"),
    ]
    _write_export(tmp_path, rows, mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].currency == "USD"


def test_currency_with_zero_events_in_otherwise_populated_valid_file_returns_empty_list(tmp_path):
    """Distinct from test_no_matching_events_returns_empty_list_not_none (which
    uses a file with zero rows at all): here the export is well-formed,
    fresh, and has real high-impact events in-window -- just none for the
    queried currency (JPY). Must still be [], not None -- "successfully
    fetched, nothing for this currency" is not a fetch failure."""
    rows = [
        _row(currency="GBP", event_time="2026-07-20 12:15:00"),
        _row(currency="EUR", event_time="2026-07-20 12:30:00"),
    ]
    _write_export(tmp_path, rows, mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("JPY", WINDOW_START, WINDOW_END)

    assert events == []
    assert events is not None


def test_high_impact_only_filtering_excludes_low_and_moderate(tmp_path):
    rows = [
        _row(importance="low", event_time="2026-07-20 12:10:00"),
        _row(importance="moderate", event_time="2026-07-20 12:20:00"),
        _row(importance="high", event_time="2026-07-20 12:30:00"),
    ]
    _write_export(tmp_path, rows, mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1
    assert events[0].impact == "high"


def test_events_outside_window_are_excluded(tmp_path):
    rows = [
        _row(event_time="2026-07-20 11:00:00"),  # before window
        _row(event_time="2026-07-20 14:00:00"),  # after window
        _row(event_time="2026-07-20 12:30:00"),  # inside window
    ]
    _write_export(tmp_path, rows, mtime=NOW)
    provider = _provider(tmp_path)

    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)

    assert events is not None
    assert len(events) == 1


# --- constructor path-joining (commondata_path -> Files\ -> filename) -----


def test_constructor_appends_files_subfolder_under_the_parent_commondata_path(tmp_path):
    """Explicitly pins the two-level path structure the constructor's own
    docstring documents: `commondata_path` is the *parent* ".../Terminal/Common"
    folder (exactly what `resolve_commondata_path()`/`mt5.terminal_info().commondata_path`
    return -- see the fake `commondata_path` value used in
    test_resolve_commondata_path_returns_path_when_terminal_info_available
    below, which is a bare ".../Terminal/Common" path with no "Files" segment),
    NOT a pre-joined ".../Common/Files" path. `_provider()`/`_write_export()`
    above already exercise this implicitly on every test (they hand the
    provider the bare tmp_path and write under tmp_path/Files/), but this
    test additionally asserts the resolved internal path directly, so an
    off-by-one-folder-level regression (e.g. forgetting to append "Files",
    or appending it twice) fails here even if some future refactor of the
    other tests' fixtures accidentally started pre-joining "Files" itself."""
    provider = MQL5CalendarProvider(tmp_path, clock=FixedClock(NOW))

    assert provider._export_path == tmp_path / "Files" / EXPORT_FILENAME

    # And end-to-end: a file written at that real nested path is found when
    # the provider is only ever given the bare parent folder.
    _write_export(tmp_path, [_row()], mtime=NOW)
    events = provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END)
    assert events is not None
    assert len(events) == 1


# --- clock/timezone handling of the staleness check ------------------------


def test_naive_clock_rejected_at_construction_with_clear_error(tmp_path):
    """Was `test_staleness_check_raises_typeerror_with_a_naive_clock_SUSPECTED_BUG`,
    which pinned a confirmed bug: the staleness check does
    `self._clock.now() - export.mtime`, where `export.mtime` is always
    built as timezone-AWARE UTC (`_read_export_file`:
    `datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)`). `RealClock`
    (common/clock.py) is also UTC-aware, so today's actual production wiring
    (`build_news_provider(clock)` in scripts/run_shadow_loop.py is always
    called with `adapter_clock = RealClock()`, never the loop's naive
    `ServerClock`) never hits this. But `MQL5CalendarProvider.__init__`
    accepts the generic `Clock` protocol (`common/clock.py`), which also
    includes `common/mt5_time.ServerClock` -- naive by its own docstring,
    and the SAME clock convention this module's own docstring says
    `NewsEvent.event_time`/window comparisons must use. Constructing this
    provider with a naive `Clock` (e.g. if a future refactor unifies clocks
    and passes `ServerClock` here instead of `adapter_clock`) used to make
    every call raise `TypeError: can't subtract offset-naive and
    offset-aware datetimes` -- a hard crash, not the documented "returns
    None, never raises" fail-safe contract.

    Fixed by validating `clock.now().tzinfo` eagerly in `__init__`, so the
    misconfiguration is caught immediately at wiring time with an actionable
    `ValueError` instead of surfacing as a crash (or a silently-permanent
    fail-safe `None`) the first time a real news check fires, possibly much
    later in a live/demo run. See also
    `test_clock_that_turns_naive_after_construction_fails_safe_not_raises`
    below for the belt-and-suspenders runtime catch that still applies if a
    clock implementation is inconsistent across calls."""

    class NaiveClock:
        def now(self) -> datetime:
            return NOW.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-naive"):
        MQL5CalendarProvider(tmp_path, clock=NaiveClock())


def test_clock_that_turns_naive_after_construction_fails_safe_not_raises(tmp_path):
    """Defense-in-depth for the (unlikely, but cheap to guard) case where a
    `Clock` implementation returns an aware datetime on its first call
    (passing `__init__`'s eager validation) but a naive one on a later call
    -- the staleness computation in `get_high_impact_events` itself still
    catches the resulting `TypeError` and fails safe (None, logged) rather
    than raising, matching every other error path in this module."""

    class FlakyClock:
        def __init__(self) -> None:
            self._calls = 0

        def now(self) -> datetime:
            self._calls += 1
            return NOW if self._calls == 1 else NOW.replace(tzinfo=None)

    _write_export(tmp_path, [_row()], mtime=NOW)
    provider = MQL5CalendarProvider(tmp_path, clock=FlakyClock())

    assert provider.get_high_impact_events("USD", WINDOW_START, WINDOW_END) is None


# --- resolve_commondata_path() -- the one MT5-touching function -----------


def test_resolve_commondata_path_returns_path_when_terminal_info_available(monkeypatch):
    class _FakeTerminalInfo:
        commondata_path = r"C:\Users\Someone\AppData\Roaming\MetaQuotes\Terminal\Common"

    monkeypatch.setattr(mcp.mt5, "terminal_info", lambda: _FakeTerminalInfo())

    assert mcp.resolve_commondata_path() == r"C:\Users\Someone\AppData\Roaming\MetaQuotes\Terminal\Common"


def test_resolve_commondata_path_returns_none_when_terminal_info_unavailable(monkeypatch):
    monkeypatch.setattr(mcp.mt5, "terminal_info", lambda: None)
    monkeypatch.setattr(mcp.mt5, "last_error", lambda: (1, "no connection"))

    assert mcp.resolve_commondata_path() is None


# --- MQL5/Python CSV column order stays in sync --------------------------


def test_csv_columns_matches_mql5_exporters_header_write_call():
    """Guards against `_CSV_COLUMNS` and `mql5/NewsCalendarExporter.mq5`'s
    own `FileWrite(handle, "event_time", "currency", ...)` header call
    silently drifting apart if only one side is ever edited in a future
    change -- today the only thing keeping them in sync is a comment saying
    "must match exactly" (see `_CSV_COLUMNS`'s comment above)."""
    mq5_path = Path(__file__).resolve().parents[3] / "mql5" / "NewsCalendarExporter.mq5"
    mq5_text = mq5_path.read_text(encoding="utf-8")

    match = re.search(r'FileWrite\(handle,\s*"event_time".*?\);', mq5_text)
    assert match is not None, "could not find the header FileWrite(handle, \"event_time\", ...) call in NewsCalendarExporter.mq5"

    header_columns = tuple(re.findall(r'"([^"]+)"', match.group(0)))

    assert header_columns == _CSV_COLUMNS
