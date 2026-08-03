"""Unit tests for ops/backup_db.py -- ops/ has no __init__.py and the
script's paths are hardcoded to the VPS layout, so it's loaded directly via
importlib (same convention as tests/unit/test_run_health_check.py) and its
module-level SRC/DEST_DIR/CALENDAR_SRC/KEEP_DAYS constants are monkeypatched
to tmp_path locations for main()-level tests.

Covers the journal backup (SQLite online-backup), the calendar-archive copy
(plain file copy -- see the module docstring for why that's safe here), and
pruning, plus the two failure-isolation guarantees: a missing/failing
journal backup must not prevent the calendar-archive copy from being
attempted (or vice versa), and only the journal path affects the exit
code."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "backup_db.py"
_spec = importlib.util.spec_from_file_location("backup_db_script", SCRIPT_PATH)
backup_db = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = backup_db
_spec.loader.exec_module(backup_db)


def _make_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    conn.close()


def test_backup_journal_produces_a_readable_copy(tmp_path):
    src = tmp_path / "trade_journal_paper.sqlite"
    _make_sqlite_db(src)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    dest = backup_db.backup_journal(src, dest_dir, "20260804_000000")

    assert dest == dest_dir / "trade_journal_paper_20260804_000000.sqlite"
    conn = sqlite3.connect(str(dest))
    assert conn.execute("SELECT x FROM t").fetchone() == (1,)
    conn.close()


def test_copy_calendar_archive_copies_file_contents(tmp_path):
    src = tmp_path / "news_calendar_history.csv"
    src.write_text("first_seen_utc,event_time,currency,importance,event_name\n")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    dest = backup_db.copy_calendar_archive(src, dest_dir, "20260804_000000")

    assert dest == dest_dir / "news_calendar_history_20260804_000000.csv"
    assert dest.read_text() == src.read_text()


def test_prune_old_removes_only_files_older_than_keep_days(tmp_path):
    old = tmp_path / "trade_journal_paper_old.sqlite"
    new = tmp_path / "trade_journal_paper_new.sqlite"
    old.write_text("x")
    new.write_text("x")
    old_time = time.time() - 40 * 86400
    import os

    os.utime(old, (old_time, old_time))

    pruned = backup_db.prune_old(tmp_path, "trade_journal_paper_*.sqlite", keep_days=30)

    assert pruned == 1
    assert not old.exists()
    assert new.exists()


def test_main_backs_up_both_journal_and_calendar(tmp_path, monkeypatch, capsys):
    src = tmp_path / "trade_journal_paper.sqlite"
    _make_sqlite_db(src)
    calendar_src = tmp_path / "news_calendar_history.csv"
    calendar_src.write_text("data\n")
    dest_dir = tmp_path / "dest"

    monkeypatch.setattr(backup_db, "SRC", src)
    monkeypatch.setattr(backup_db, "CALENDAR_SRC", calendar_src)
    monkeypatch.setattr(backup_db, "DEST_DIR", dest_dir)

    exit_code = backup_db.main()

    assert exit_code == 0
    assert list(dest_dir.glob("trade_journal_paper_*.sqlite"))
    assert list(dest_dir.glob("news_calendar_history_*.csv"))


def test_main_missing_journal_exits_1_but_still_backs_up_calendar(tmp_path, monkeypatch, capsys):
    calendar_src = tmp_path / "news_calendar_history.csv"
    calendar_src.write_text("data\n")
    dest_dir = tmp_path / "dest"

    monkeypatch.setattr(backup_db, "SRC", tmp_path / "missing.sqlite")
    monkeypatch.setattr(backup_db, "CALENDAR_SRC", calendar_src)
    monkeypatch.setattr(backup_db, "DEST_DIR", dest_dir)

    exit_code = backup_db.main()

    assert exit_code == 1
    assert "nothing to back up." in capsys.readouterr().out
    assert list(dest_dir.glob("news_calendar_history_*.csv"))


def test_main_missing_calendar_archive_does_not_fail_the_run(tmp_path, monkeypatch, capsys):
    src = tmp_path / "trade_journal_paper.sqlite"
    _make_sqlite_db(src)
    dest_dir = tmp_path / "dest"

    monkeypatch.setattr(backup_db, "SRC", src)
    monkeypatch.setattr(backup_db, "CALENDAR_SRC", tmp_path / "missing.csv")
    monkeypatch.setattr(backup_db, "DEST_DIR", dest_dir)

    exit_code = backup_db.main()

    assert exit_code == 0
    assert "nothing to back up yet." in capsys.readouterr().out
    assert list(dest_dir.glob("trade_journal_paper_*.sqlite"))
    assert not list(dest_dir.glob("news_calendar_history_*.csv"))


def test_main_journal_backup_failure_does_not_block_calendar_copy(tmp_path, monkeypatch, capsys):
    src = tmp_path / "trade_journal_paper.sqlite"
    _make_sqlite_db(src)
    calendar_src = tmp_path / "news_calendar_history.csv"
    calendar_src.write_text("data\n")
    dest_dir = tmp_path / "dest"

    monkeypatch.setattr(backup_db, "SRC", src)
    monkeypatch.setattr(backup_db, "CALENDAR_SRC", calendar_src)
    monkeypatch.setattr(backup_db, "DEST_DIR", dest_dir)

    def _boom(src, dest_dir, stamp):
        raise RuntimeError("boom")

    monkeypatch.setattr(backup_db, "backup_journal", _boom)

    exit_code = backup_db.main()

    assert exit_code == 1
    assert "Journal backup failed" in capsys.readouterr().out
    assert list(dest_dir.glob("news_calendar_history_*.csv"))


def test_main_calendar_copy_failure_does_not_block_journal_backup(tmp_path, monkeypatch, capsys):
    src = tmp_path / "trade_journal_paper.sqlite"
    _make_sqlite_db(src)
    calendar_src = tmp_path / "news_calendar_history.csv"
    calendar_src.write_text("data\n")
    dest_dir = tmp_path / "dest"

    monkeypatch.setattr(backup_db, "SRC", src)
    monkeypatch.setattr(backup_db, "CALENDAR_SRC", calendar_src)
    monkeypatch.setattr(backup_db, "DEST_DIR", dest_dir)

    def _boom(src, dest_dir, stamp):
        raise RuntimeError("boom")

    monkeypatch.setattr(backup_db, "copy_calendar_archive", _boom)

    exit_code = backup_db.main()

    assert exit_code == 0
    assert "Calendar archive backup failed" in capsys.readouterr().out
    assert list(dest_dir.glob("trade_journal_paper_*.sqlite"))
