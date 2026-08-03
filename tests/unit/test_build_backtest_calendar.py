"""Tests for scripts/build_backtest_calendar.py -- the canonical historical
news-calendar CSV builder (dedup, US-DST normalisation, NFP self-check,
UTC-skew quarantine). Same importlib-loading convention as
tests/unit/test_run_backtest.py (scripts/ has no __init__.py)."""
from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_backtest_calendar.py"
_spec = importlib.util.spec_from_file_location("build_backtest_calendar", SCRIPT_PATH)
builder = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = builder
_spec.loader.exec_module(builder)

_DUMP_HEADER = "event_time,currency,importance,event_name,forecast,previous,actual"
_ARCHIVE_HEADER = "first_seen_utc,event_time,currency,importance,event_name"


def _write_dump(tmp_path: Path, rows: list[str], name: str = "dump.csv") -> Path:
    path = tmp_path / name
    path.write_text(
        "# generated_at_server_time=2026-08-04 00:00:00\n" + _DUMP_HEADER + "\n" + "\n".join(rows) + "\n",
        encoding="ascii",
    )
    return path


def _write_archive(tmp_path: Path, rows: list[str], name: str = "archive.csv") -> Path:
    path = tmp_path / name
    path.write_text(_ARCHIVE_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _read_out(path: Path) -> list[dict]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    return list(csv.DictReader(lines))


# --- DST normalisation -----------------------------------------------------


def test_us_dst_on_true_in_july():
    assert builder.us_dst_on(datetime(2026, 7, 15)) is True


def test_us_dst_on_false_in_january():
    assert builder.us_dst_on(datetime(2026, 1, 15)) is False


def test_us_dst_second_sunday_march_boundary_2026():
    # 2026's second Sunday of March is the 8th.
    assert builder.us_dst_on(datetime(2026, 3, 7)) is False
    assert builder.us_dst_on(datetime(2026, 3, 8)) is True


def test_us_dst_first_sunday_november_boundary_2026():
    # 2026's first Sunday of November is the 1st.
    assert builder.us_dst_on(datetime(2026, 10, 31)) is True
    assert builder.us_dst_on(datetime(2026, 11, 1)) is False


def test_normalise_event_time_unchanged_in_dst_summer():
    assert builder.normalise_event_time(datetime(2026, 7, 2, 15, 30)) == datetime(2026, 7, 2, 15, 30)


def test_normalise_event_time_shifted_back_one_hour_in_dst_off_winter():
    assert builder.normalise_event_time(datetime(2026, 1, 2, 16, 30)) == datetime(2026, 1, 2, 15, 30)


# --- dump loading + normalisation applied end-to-end ------------------------


def test_dump_rows_are_normalised_and_forecast_previous_actual_preserved(tmp_path):
    dump = _write_dump(tmp_path, ["2026-01-02 16:30:00,USD,high,Nonfarm Payrolls,3.0,559.0,850.0"])
    rows = builder._load_dump(dump)

    assert len(rows) == 1
    assert rows[0].event_time == "2026-01-02 15:30:00"
    assert rows[0].forecast == "3.0"
    assert rows[0].previous == "559.0"
    assert rows[0].actual == "850.0"


def test_dump_structurally_unparseable_refuses(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("not,the,right,header\n1,2,3,4\n", encoding="ascii")

    with pytest.raises(SystemExit):
        builder._load_dump(bad)


# --- dedup -------------------------------------------------------------


def test_dedup_collapses_exact_key_duplicates():
    rows = [
        builder._Row(event_time="2026-01-01 15:30:00", currency="USD", importance="high", event_name="Event"),
        builder._Row(event_time="2026-01-01 15:30:00", currency="USD", importance="high", event_name="Event"),
    ]
    assert len(builder._dedup(rows)) == 1


def test_dedup_keeps_an_importance_regrade_as_a_separate_row():
    rows = [
        builder._Row(event_time="2026-01-01 15:30:00", currency="USD", importance="high", event_name="Event"),
        builder._Row(event_time="2026-01-01 15:30:00", currency="USD", importance="moderate", event_name="Event"),
    ]
    assert len(builder._dedup(rows)) == 2


# --- NFP self-check ------------------------------------------------------


def test_nfp_gate_passes_when_all_in_range_rows_land_at_15_30():
    rows = [
        builder._Row(event_time="2022-01-07 15:30:00", currency="USD", importance="high",
                     event_name="Nonfarm Payrolls"),
    ]
    ok, bad_in, bad_out = builder.check_nfp_gate(rows)
    assert ok is True
    assert bad_in == []
    assert bad_out == []


def test_nfp_gate_fails_on_an_in_range_violation():
    rows = [
        builder._Row(event_time="2022-01-07 16:30:00", currency="USD", importance="high",
                     event_name="Nonfarm Payrolls"),
    ]
    ok, bad_in, bad_out = builder.check_nfp_gate(rows)
    assert ok is False
    assert bad_in == ["2022-01-07 16:30:00"]


def test_nfp_gate_reports_but_does_not_fail_an_out_of_range_violation():
    # Mirrors the real known-bad row: 2025-11-20, outside IN_RANGE_END.
    rows = [
        builder._Row(event_time="2025-11-20 14:30:00", currency="USD", importance="high",
                     event_name="Nonfarm Payrolls"),
    ]
    ok, bad_in, bad_out = builder.check_nfp_gate(rows)
    assert ok is True
    assert bad_in == []
    assert bad_out == ["2025-11-20 14:30:00"]


def test_nfp_gate_ignores_non_nfp_or_non_high_or_non_usd_rows():
    rows = [
        builder._Row(event_time="2022-01-07 16:30:00", currency="EUR", importance="high",
                     event_name="Nonfarm Payrolls"),
        builder._Row(event_time="2022-01-07 16:30:00", currency="USD", importance="moderate",
                     event_name="Nonfarm Payrolls"),
        builder._Row(event_time="2022-01-07 16:30:00", currency="USD", importance="high",
                     event_name="Some Other Event"),
    ]
    ok, bad_in, bad_out = builder.check_nfp_gate(rows)
    assert ok is True


def test_build_refuses_to_write_when_in_range_nfp_gate_fails(tmp_path):
    dump = _write_dump(tmp_path, [
        # +3h-all-history normalisation would put this at 16:30, but it's
        # already 16:30 in the raw dump for a DST-OFF (winter) date, so after
        # the -1h shift it lands at 15:30 -- correct. To force a genuine
        # in-range violation, use a summer (DST-on) date where NO shift is
        # applied, stamped wrong in the raw dump.
        "2022-07-08 16:30:00,USD,high,Nonfarm Payrolls,,,",
    ])
    out = tmp_path / "out.csv"

    result = builder.build(dump, tmp_path / "missing_archive.csv", out)

    assert result == 1
    assert not out.exists()


# --- UTC-skew archive quarantine -----------------------------------------


def test_utc_skew_pair_quarantines_the_earlier_collected_row():
    raw_rows = [
        {"first_seen_utc": "2026-08-03 16:54:27", "event_time": "2026-08-04 13:00:00",
         "currency": "USD", "importance": "high", "event_name": "FOMC Statement"},
        {"first_seen_utc": "2026-08-03 18:10:00", "event_time": "2026-08-04 16:00:00",
         "currency": "USD", "importance": "high", "event_name": "FOMC Statement"},
    ]
    kept, quarantined = builder._quarantine_utc_skew(raw_rows)

    assert quarantined == 1
    assert len(kept) == 1
    assert kept[0]["event_time"] == "2026-08-04 16:00:00"


def test_non_matching_rows_are_never_quarantined():
    raw_rows = [
        {"first_seen_utc": "2026-08-03 16:54:27", "event_time": "2026-08-04 13:00:00",
         "currency": "USD", "importance": "high", "event_name": "Event A"},
        {"first_seen_utc": "2026-08-03 18:10:00", "event_time": "2026-08-11 13:00:00",
         "currency": "USD", "importance": "high", "event_name": "Event A"},
    ]
    kept, quarantined = builder._quarantine_utc_skew(raw_rows)

    assert quarantined == 0
    assert len(kept) == 2


def test_load_archive_missing_file_is_optional_not_fatal(tmp_path):
    rows, quarantined = builder._load_archive(tmp_path / "does_not_exist.csv")
    assert rows == []
    assert quarantined == 0


def test_load_archive_unexpected_header_refuses(tmp_path):
    path = tmp_path / "archive.csv"
    path.write_text("wrong,header\n1,2\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        builder._load_archive(path)


def test_load_archive_applies_quarantine_and_normal_rows_pass_through(tmp_path):
    archive = _write_archive(tmp_path, [
        "2026-08-03 16:54:27,2026-08-04 13:00:00,USD,high,FOMC Statement",
        "2026-08-03 18:10:00,2026-08-04 16:00:00,USD,high,FOMC Statement",
        "2026-08-03 18:10:00,2026-08-05 09:00:00,EUR,high,ECB Rate Decision",
    ])
    rows, quarantined = builder._load_archive(archive)

    assert quarantined == 1
    assert len(rows) == 2
    assert {r.event_time for r in rows} == {"2026-08-04 16:00:00", "2026-08-05 09:00:00"}


# --- end-to-end build ------------------------------------------------------


def test_build_writes_merged_deduped_sorted_output_with_the_expected_header(tmp_path):
    dump = _write_dump(tmp_path, [
        "2026-07-02 15:30:00,USD,high,Nonfarm Payrolls,3.0,559.0,850.0",  # July, DST-on -> unchanged
    ])
    archive = _write_archive(tmp_path, [
        "2026-08-03 18:10:00,2026-07-02 20:00:00,USD,high,FOMC Statement",
    ])
    out = tmp_path / "out.csv"

    result = builder.build(dump, archive, out)

    assert result == 0
    assert out.exists()
    rows = _read_out(out)
    assert [r["event_time"] for r in rows] == ["2026-07-02 15:30:00", "2026-07-02 20:00:00"]
    assert rows[0]["forecast"] == "3.0"
    assert rows[1]["forecast"] == ""


def test_build_dedups_across_dump_and_archive_on_the_same_key(tmp_path):
    dump = _write_dump(tmp_path, ["2026-07-02 15:30:00,USD,high,Nonfarm Payrolls,3.0,559.0,850.0"])
    archive = _write_archive(tmp_path, ["2026-08-03 18:10:00,2026-07-02 15:30:00,USD,high,Nonfarm Payrolls"])
    out = tmp_path / "out.csv"

    result = builder.build(dump, archive, out)

    assert result == 0
    assert len(_read_out(out)) == 1


def test_build_with_no_archive_file_still_succeeds(tmp_path):
    dump = _write_dump(tmp_path, ["2026-07-02 15:30:00,USD,high,Nonfarm Payrolls,,,"])
    out = tmp_path / "out.csv"

    result = builder.build(dump, tmp_path / "missing.csv", out)

    assert result == 0
    assert len(_read_out(out)) == 1
