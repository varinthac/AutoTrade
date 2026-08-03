"""Append-only historical archive of the MQL5 economic-calendar export.

Why this exists (EXP-023, 2026-08-03, `experiments/experiments_log.md`):
Watchman news protection (live "mode A") has NEVER been modeled in any
backtest -- every EXP-001..023 baseline is effectively "no news protection"
("mode C") -- because no historical economic-calendar dataset exists to
reconstruct when the protection would have triggered. The exporter
(`mql5/NewsCalendarExporter.mq5`) only ever writes a rolling [-2h, +48h]
window snapshot that overwrites itself every 5 minutes, so history is lost
unless something accumulates it. This module is that something: each run
folds the current export snapshot into an append-only CSV keyed on
(event_time, currency, importance, event_name), so a future EXP-024 can
replay real trigger windows instead of the always-fires / US-macro-hours
proxies EXP-023 had to settle for.

Deliberately NOT wired into the live loop: `scripts/run_health_check.py`
calls `archive_export_file()` once per heartbeat cycle (~10 min via Task
Scheduler -- any cadence under the exporter's 48h lookahead loses nothing),
so the live shadow-loop process needs no restart and takes no new code
risk, and the archiver inherits the heartbeat's own already-monitored
scheduling instead of adding a new Task Scheduler task to watch.

Time conventions, matching `mql5_calendar_provider.py`'s module docstring:
`event_time` values are naive MT5 broker **server time** exactly as
exported -- never reinterpreted here. `first_seen_utc` is this machine's
UTC wall clock at archive time, metadata only (it says when a row first
appeared in a snapshot, useful for "was this event on the calendar before
it happened?" questions -- it is NOT comparable to `event_time`).

Fail-safety: archiving is best-effort observation, never trading-path code.
A structurally unparseable snapshot returns `None` (mirroring
`parse_export_csv`'s own contract) and archives nothing; malformed
individual rows are skipped by `parse_export_csv` itself. Re-running on an
unchanged snapshot appends nothing (idempotent by key).
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from autotrade.common.config import REPO_ROOT
from autotrade.council.mql5_calendar_provider import parse_export_csv

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_PATH = REPO_ROOT / "data" / "db" / "news_calendar_history.csv"

ARCHIVE_COLUMNS = ("first_seen_utc", "event_time", "currency", "importance", "event_name")

# The subset of ARCHIVE_COLUMNS that identifies an event occurrence -- an
# importance re-grade (rare but real on MQL5's feed) deliberately produces a
# second row rather than silently overwriting history.
_KEY_COLUMNS = ("event_time", "currency", "importance", "event_name")


def _load_existing_keys(archive_path: Path) -> set[tuple[str, ...]] | None:
    """Keys already present in the archive (empty set if the file doesn't
    exist yet). Returns `None` if an archive file exists but can't be read
    or has an unexpected header -- the caller must then refuse to append
    (appending rows to a file we couldn't dedup against would corrupt the
    exact "no duplicates by key" property EXP-024 will rely on)."""
    if not archive_path.exists():
        return set()
    try:
        with archive_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != list(ARCHIVE_COLUMNS):
                logger.warning(
                    "calendar_archive: unexpected archive header %r (expected %r) in %s -- "
                    "refusing to append",
                    reader.fieldnames, ARCHIVE_COLUMNS, archive_path,
                )
                return None
            return {tuple(row[c] for c in _KEY_COLUMNS) for row in reader}
    except (OSError, csv.Error, KeyError, TypeError) as exc:
        logger.warning("calendar_archive: could not read existing archive %s (%s)", archive_path, exc)
        return None


def archive_export_text(csv_text: str, archive_path: Path, first_seen_utc: str) -> int | None:
    """Folds one export snapshot into the archive. Returns the number of
    rows appended (0 when the snapshot held nothing new), or `None` if
    either the snapshot or the existing archive was unusable -- in which
    case the archive file is left untouched."""
    rows = parse_export_csv(csv_text)
    if rows is None:
        return None

    existing = _load_existing_keys(archive_path)
    if existing is None:
        return None

    fresh = [row for row in rows if tuple(row[c] for c in _KEY_COLUMNS) not in existing]
    if not fresh:
        return 0

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not archive_path.exists()
    try:
        with archive_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if is_new_file:
                writer.writerow(ARCHIVE_COLUMNS)
            appended: set[tuple[str, ...]] = set()
            for row in fresh:
                key = tuple(row[c] for c in _KEY_COLUMNS)
                if key in appended:  # duplicate within a single snapshot
                    continue
                appended.add(key)
                writer.writerow((first_seen_utc, *key))
    except OSError as exc:
        logger.warning("calendar_archive: could not append to archive %s (%s)", archive_path, exc)
        return None
    return len(appended)


def archive_export_file(
    export_path: Path, archive_path: Path = DEFAULT_ARCHIVE_PATH
) -> int | None:
    """Reads the current export snapshot and folds it into the archive --
    the one call `scripts/run_health_check.py` makes per heartbeat cycle.
    Returns rows appended, or `None` if the snapshot was missing/unreadable
    (routine while MT5/the exporter Service is down -- the calendar-export
    watchdog owns alerting on that; this stays quiet beyond a log line)."""
    try:
        csv_text = export_path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        logger.warning("calendar_archive: could not read export %s (%s)", export_path, exc)
        return None
    first_seen_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return archive_export_text(csv_text, archive_path, first_seen_utc)
