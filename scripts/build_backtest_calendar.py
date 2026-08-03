#!/usr/bin/env python3
"""Builds the canonical, normalised historical news-calendar CSV that
`backtest/historical_news_calendar.HistoricalNewsCalendarProvider` replays
against: `data/historical/news_calendar_backtest.csv` -- gitignored (matches
`data/historical/*` in `.gitignore`), exactly like every other historical CSV
this project regenerates rather than commits.

    python scripts/build_backtest_calendar.py [--dump PATH] [--archive PATH] [--out PATH]

Regeneration: re-run this whenever the MT5 dump (`mql5/CalendarHistoryDump.mq5`'s
one-off export, default `C:\\Users\\Varintha\\AppData\\Roaming\\MetaQuotes\\
Terminal\\Common\\Files\\AutoTradeNewsCalendarHistory.csv`) is refreshed, or
whenever `data/db/news_calendar_history.csv` (the live archive; VPS-owned --
may be absent or stale on this dev PC) picks up new rows. There is no
scheduled task for this -- it is a manual backtest-data-prep step, same as
`scripts/download_historical.py`.

Inputs, merge rule, and why (EXP-024, `experiments/experiments_log.md`'s
2026-08-03/2026-08-04 NOTEs + `### EXP-024 RESULTS` §10):

  1. The MT5 DUMP (`mql5/CalendarHistoryDump.mq5`'s one-off export,
     `parse_export_csv`-compatible, 7 columns) -- deep history (2021-07-01
     onward) but timestamped **UTC + the CURRENT server offset (+3) applied
     to ALL history**, not the true per-date server clock. This script
     applies the mandatory -1h US-DST-off normalisation to every DUMP row
     (`normalise_event_time` below) -- NEVER to archive rows, which are
     already "stamped correctly in-season" (collected with the
     then-current offset each cycle).
  2. The live ARCHIVE (`council/calendar_archive.py`'s append-only output,
     `data/db/news_calendar_history.csv`, 5 columns -- no forecast/previous/
     actual). OPTIONAL: on this dev PC it may be absent or stale (the VPS
     owns the live one); when present, its rows are folded in UNNORMALISED,
     after quarantining a known exporter-restart bug (§10): its first two
     collection cycles (2026-08-03) recorded event times exactly 3 hours
     BEHIND the correct value (the plausible mechanism: an MQL5 terminal
     reporting a zero server offset before its first post-restart sync).
     Detected here generically, not by hardcoding those two timestamps: an
     archive row is quarantined if another row shares its (currency,
     importance, event_name) and its event_time is exactly 3 hours earlier
     -- the earlier-`first_seen_utc` (i.e. earlier-collected, therefore the
     skewed one) of the pair is dropped, the later-seen, correct one kept.

Dedup key (both sources, matching `council/calendar_archive.py`'s own key):
(event_time, currency, importance, event_name) -- an importance re-grade
deliberately produces a second row (see that module's docstring), so this
merge doesn't collapse those either.

**Self-check (refuses to write the output otherwise).** After
normalisation, every IN-RANGE (`IN_RANGE_START`/`IN_RANGE_END` below -- this
project's own Train+Val experiment windows) high-impact-USD "Nonfarm
Payrolls" row must land at exactly 15:30 server -- the EXP-024
pre-registration's G3 gate. One known-bad row exists in the full dump at
2025-11-20 (normalises to 14:30); it is OUTSIDE every window this project's
experiments have ever used, so it is reported but NOT fatal, exactly how
`experiments/exp024_real_calendar_harness.py`'s `check_nfp_gate` handles it.
An IN-RANGE violation aborts the build (nothing is written).
"""
from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from autotrade.common.config import REPO_ROOT
from autotrade.council.mql5_calendar_provider import parse_export_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DUMP = Path(
    r"C:\Users\Varintha\AppData\Roaming\MetaQuotes\Terminal\Common\Files\AutoTradeNewsCalendarHistory.csv"
)
DEFAULT_ARCHIVE = REPO_ROOT / "data" / "db" / "news_calendar_history.csv"
DEFAULT_OUT = REPO_ROOT / "data" / "historical" / "news_calendar_backtest.csv"

_EVENT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_OUT_COLUMNS = ("event_time", "currency", "importance", "event_name", "forecast", "previous", "actual")
_ARCHIVE_COLUMNS = ("first_seen_utc", "event_time", "currency", "importance", "event_name")

# EXP-024's pre-registered G3 window -- this project's own Train+Val
# experiment range, the only range an in-range NFP violation could affect.
IN_RANGE_START = datetime(2021, 7, 22)
IN_RANGE_END = datetime(2025, 7, 22)

_UTC_SKEW_DELTA = timedelta(hours=3)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def us_dst_on(ts: datetime) -> bool:
    """US DST: second Sunday of March (inclusive) -> first Sunday of
    November (exclusive). Sunday == weekday 6. Same rule
    `experiments/exp024_real_calendar_harness.py`'s `us_dst_on` uses --
    date granularity; the residual error is confined to the two transition
    Sundays, on which the US high-impact release calendar is empty."""
    return _nth_weekday(ts.year, 3, 6, 2) <= ts.date() < _nth_weekday(ts.year, 11, 6, 1)


def normalise_event_time(raw: datetime) -> datetime:
    """DUMP rows only (see module docstring): the dump stamps ALL history as
    UTC + the CURRENT server offset (+3), while the true per-date server
    clock is +2 in US-DST-off, +3 in US-DST-on -- so DST-off rows are 1h
    LATE relative to the bar clock and must be shifted back."""
    return raw if us_dst_on(raw) else raw - timedelta(hours=1)


@dataclass
class _Row:
    event_time: str  # normalised (dump) or as-archived (archive), "%Y-%m-%d %H:%M:%S"
    currency: str
    importance: str
    event_name: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""


def _load_dump(path: Path) -> list[_Row]:
    text = path.read_text(encoding="ascii", errors="replace")
    rows = parse_export_csv(text)
    if rows is None:
        raise SystemExit(f"REFUSED: dump {path} is structurally unparseable by parse_export_csv.")
    out = []
    for r in rows:
        raw = datetime.strptime(r["event_time"], _EVENT_TIME_FORMAT)
        norm = normalise_event_time(raw)
        out.append(_Row(
            event_time=norm.strftime(_EVENT_TIME_FORMAT), currency=r["currency"], importance=r["importance"],
            event_name=r["event_name"], forecast=r["forecast"], previous=r["previous"], actual=r["actual"],
        ))
    return out


def _quarantine_utc_skew(raw_rows: list[dict]) -> tuple[list[dict], int]:
    """Drops the skewed half of any (currency, importance, event_name) pair
    whose event_times differ by exactly 3 hours -- see module docstring's
    §10 explanation. Returns (kept rows, number quarantined)."""
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for r in raw_rows:
        by_key.setdefault((r["currency"], r["importance"], r["event_name"]), []).append(r)

    quarantined_ids: set[int] = set()
    for group in by_key.values():
        for i, a in enumerate(group):
            a_time = datetime.strptime(a["event_time"], _EVENT_TIME_FORMAT)
            for b in group[i + 1:]:
                b_time = datetime.strptime(b["event_time"], _EVENT_TIME_FORMAT)
                if abs(b_time - a_time) != _UTC_SKEW_DELTA:
                    continue
                # The earlier-collected (smaller first_seen_utc) of the pair
                # was written before the exporter's post-restart server-time
                # sync -- drop it, keep the later-seen, correct one.
                skewed = a if a["first_seen_utc"] < b["first_seen_utc"] else b
                quarantined_ids.add(id(skewed))

    kept = [r for r in raw_rows if id(r) not in quarantined_ids]
    return kept, len(quarantined_ids)


def _load_archive(path: Path) -> tuple[list[_Row], int]:
    """Returns (rows, quarantined_count). `([], 0)` if `path` doesn't exist
    -- the archive input is OPTIONAL (module docstring)."""
    if not path.exists():
        logger.info("build_backtest_calendar: archive %s not found -- proceeding with the dump only.", path)
        return [], 0

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != list(_ARCHIVE_COLUMNS):
            raise SystemExit(
                f"REFUSED: archive {path} has an unexpected header {reader.fieldnames!r} "
                f"(expected {_ARCHIVE_COLUMNS!r})."
            )
        raw_rows = list(reader)

    kept, quarantined = _quarantine_utc_skew(raw_rows)
    out = [
        _Row(event_time=r["event_time"], currency=r["currency"], importance=r["importance"], event_name=r["event_name"])
        for r in kept
    ]
    return out, quarantined


def _dedup(rows: list[_Row]) -> list[_Row]:
    seen: set[tuple[str, str, str, str]] = set()
    out = []
    for r in rows:
        key = (r.event_time, r.currency, r.importance, r.event_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def check_nfp_gate(rows: list[_Row]) -> tuple[bool, list[str], list[str]]:
    """EXP-024 pre-registration G3: every IN-RANGE high-impact-USD "Nonfarm
    Payrolls" row must normalise to exactly 15:30 server. Handled exactly
    like `experiments/exp024_real_calendar_harness.py`'s `check_nfp_gate`: an
    out-of-range violation (the one known-bad row, 2025-11-20) is reported
    but NOT fatal; an in-range violation is. Returns (passed, in_range_bad,
    out_of_range_bad)."""
    bad_in_range: list[str] = []
    bad_out_of_range: list[str] = []
    for r in rows:
        if r.currency != "USD" or r.importance.lower() != "high" or r.event_name != "Nonfarm Payrolls":
            continue
        t = datetime.strptime(r.event_time, _EVENT_TIME_FORMAT)
        if t.strftime("%H:%M") == "15:30":
            continue
        bucket = bad_in_range if IN_RANGE_START <= t < IN_RANGE_END else bad_out_of_range
        bucket.append(r.event_time)
    return not bad_in_range, bad_in_range, bad_out_of_range


def build(dump_path: Path, archive_path: Path, out_path: Path) -> int:
    dump_rows = _load_dump(dump_path)
    archive_rows, quarantined = _load_archive(archive_path)
    logger.info(
        "build_backtest_calendar: dump=%d rows, archive=%d rows (%d quarantined for the 3h UTC-skew bug)",
        len(dump_rows), len(archive_rows), quarantined,
    )

    merged = _dedup(dump_rows + archive_rows)

    ok, bad_in_range, bad_out_of_range = check_nfp_gate(merged)
    if bad_out_of_range:
        logger.warning(
            "build_backtest_calendar: %d out-of-range Nonfarm Payrolls row(s) do not normalise to 15:30 "
            "(known: 2025-11-20 in the full dump) -- reported, not fatal: %s",
            len(bad_out_of_range), bad_out_of_range,
        )
    if not ok:
        logger.error(
            "REFUSED: %d in-range (%s to %s) Nonfarm Payrolls row(s) do not normalise to 15:30 server -- "
            "refusing to write %s: %s",
            len(bad_in_range), IN_RANGE_START.date(), IN_RANGE_END.date(), out_path, bad_in_range,
        )
        return 1

    merged.sort(key=lambda r: r.event_time)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# generated_at_server_time={datetime.now().strftime(_EVENT_TIME_FORMAT)}\n")
        writer = csv.writer(f)
        writer.writerow(_OUT_COLUMNS)
        for r in merged:
            writer.writerow((r.event_time, r.currency, r.importance, r.event_name, r.forecast, r.previous, r.actual))

    logger.info("build_backtest_calendar: wrote %d rows to %s", len(merged), out_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.dump.exists():
        logger.error("No MT5 calendar dump at %s -- run mql5/CalendarHistoryDump.mq5 first.", args.dump)
        return 1

    return build(args.dump, args.archive, args.out)


if __name__ == "__main__":
    import sys
    sys.exit(main())
