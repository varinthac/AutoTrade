"""Tests for council/calendar_archive.py -- the append-only historical
archive of the MQL5 calendar export (EXP-024 prerequisite). Everything works
against real temp-directory files with no mocking of the parse/dedup/append
logic itself -- same "mock the boundary, not the logic" convention as
test_mql5_calendar_provider.py. The one boundary (`archive_export_file`
reading the export snapshot) is exercised with real missing/present files,
no mocks needed at all.
"""
from __future__ import annotations

import csv
from pathlib import Path

from autotrade.council.calendar_archive import (
    ARCHIVE_COLUMNS,
    archive_export_file,
    archive_export_text,
)

_HEADER = "event_time,currency,importance,event_name,forecast,previous,actual"
_COMMENT = "# generated_at_server_time=2026-08-03 10:00:00"


def _export(*rows: str) -> str:
    return "\n".join([_COMMENT, _HEADER, *rows]) + "\n"


def _row(
    event_time: str = "2026-08-03 15:30:00", currency: str = "USD", importance: str = "high",
    name: str = "Nonfarm Payrolls", forecast: str = "180k", previous: str = "150k", actual: str = "",
) -> str:
    return f"{event_time},{currency},{importance},{name},{forecast},{previous},{actual}"


def _read_archive(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_first_run_creates_archive_with_header_and_rows(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    added = archive_export_text(
        _export(_row(), _row(event_time="2026-08-03 17:00:00", name="FOMC Statement")),
        archive, "2026-08-03 09:00:00",
    )
    assert added == 2
    rows = _read_archive(archive)
    assert list(rows[0].keys()) == list(ARCHIVE_COLUMNS)
    assert rows[0]["event_time"] == "2026-08-03 15:30:00"
    assert rows[0]["first_seen_utc"] == "2026-08-03 09:00:00"
    assert rows[1]["event_name"] == "FOMC Statement"


def test_rerun_of_identical_snapshot_appends_nothing(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    snapshot = _export(_row())
    assert archive_export_text(snapshot, archive, "2026-08-03 09:00:00") == 1
    assert archive_export_text(snapshot, archive, "2026-08-03 09:30:00") == 0
    assert len(_read_archive(archive)) == 1


def test_overlapping_snapshot_appends_only_new_rows(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    archive_export_text(_export(_row()), archive, "2026-08-03 09:00:00")
    added = archive_export_text(
        _export(_row(), _row(event_time="2026-08-04 01:00:00", currency="EUR", name="ECB Rate")),
        archive, "2026-08-03 10:00:00",
    )
    assert added == 1
    rows = _read_archive(archive)
    assert len(rows) == 2
    # The pre-existing row keeps its ORIGINAL first_seen stamp -- history is
    # append-only, never rewritten.
    assert rows[0]["first_seen_utc"] == "2026-08-03 09:00:00"
    assert rows[1]["currency"] == "EUR"
    assert rows[1]["first_seen_utc"] == "2026-08-03 10:00:00"


def test_duplicate_rows_within_one_snapshot_archived_once(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    added = archive_export_text(_export(_row(), _row()), archive, "2026-08-03 09:00:00")
    assert added == 1
    assert len(_read_archive(archive)) == 1


def test_importance_regrade_becomes_a_second_row(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    archive_export_text(_export(_row(importance="moderate")), archive, "2026-08-03 09:00:00")
    added = archive_export_text(_export(_row(importance="high")), archive, "2026-08-03 10:00:00")
    assert added == 1
    assert [r["importance"] for r in _read_archive(archive)] == ["moderate", "high"]


def test_unparseable_snapshot_returns_none_and_leaves_archive_untouched(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    archive_export_text(_export(_row()), archive, "2026-08-03 09:00:00")
    before = archive.read_text(encoding="utf-8")
    assert archive_export_text("totally,wrong,header\n1,2,3\n", archive, "2026-08-03 10:00:00") is None
    assert archive.read_text(encoding="utf-8") == before


def test_corrupt_existing_archive_header_refuses_to_append(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    archive.write_text("not,the,expected,header\n", encoding="utf-8")
    assert archive_export_text(_export(_row()), archive, "2026-08-03 09:00:00") is None
    assert archive.read_text(encoding="utf-8") == "not,the,expected,header\n"


def test_event_name_with_comma_round_trips_and_dedups(tmp_path: Path) -> None:
    archive = tmp_path / "history.csv"
    named = '"Fed Chair Powell Testifies, Day 1"'
    assert archive_export_text(_export(_row(name=named)), archive, "2026-08-03 09:00:00") == 1
    # csv.DictReader unquotes on read-back, so the dedup key must match on re-run.
    assert archive_export_text(_export(_row(name=named)), archive, "2026-08-03 10:00:00") == 0
    rows = _read_archive(archive)
    assert len(rows) == 1
    assert rows[0]["event_name"] == "Fed Chair Powell Testifies, Day 1"


def test_archive_export_file_missing_snapshot_returns_none(tmp_path: Path) -> None:
    assert archive_export_file(tmp_path / "no_such_export.csv", tmp_path / "history.csv") is None
    assert not (tmp_path / "history.csv").exists()


def test_archive_export_file_reads_real_snapshot(tmp_path: Path) -> None:
    export = tmp_path / "AutoTradeNewsCalendar.csv"
    export.write_text(_export(_row()), encoding="ascii")
    archive = tmp_path / "history.csv"
    assert archive_export_file(export, archive) == 1
    rows = _read_archive(archive)
    assert rows[0]["event_name"] == "Nonfarm Payrolls"
    # first_seen_utc is a real wall-clock stamp -- just pin the format.
    assert len(rows[0]["first_seen_utc"]) == 19
