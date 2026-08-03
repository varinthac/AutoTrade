"""Nightly trade-journal backup for the VPS -- see docs/vps_deployment.md
Section 8. Uses SQLite's own online-backup mechanism (Connection.backup())
rather than a raw file copy, since a plain copy while the DB is in WAL mode
can capture an inconsistent snapshot.

Also carries off `news_calendar_history.csv` (see
autotrade/council/calendar_archive.py's module docstring) -- the only data
in the system that can't be re-downloaded if the VPS is lost. Unlike the
journal, this file is append-only and never opened in WAL mode, so a plain
file copy is a consistent snapshot; no Connection.backup() needed.

This is a deployment ops script, not part of the `autotrade` package --
paths are hardcoded to the VPS's real layout (C:\\AutoTrade), matching the
runbook's own convention (this script only ever runs on the VPS, never on
a dev machine).

The journal backup and the calendar-archive copy are independent: either
one missing or failing is logged and does not stop the other from being
attempted. A missing calendar archive (e.g. a fresh VPS install) is not an
error at all -- it just hasn't been created yet.

Prunes backups older than KEEP_DAYS so the backups folder doesn't grow
unbounded -- not critical, just tidy.

    python ops\\backup_db.py
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(r"C:\AutoTrade\data\db\trade_journal_paper.sqlite")
CALENDAR_SRC = Path(r"C:\AutoTrade\data\db\news_calendar_history.csv")

# 2026-07-28 audit finding: this used to point at a folder on the SAME disk
# as the original DB -- backups ran successfully every night (LastTaskResult
# stayed 0) while providing zero actual protection against losing the VPS
# itself. Now points inside the Google Drive for Desktop "My Drive" mirror
# folder (docs/vps_deployment.md Section 8 step 3's own already-written
# plan, just not previously executed) so the sync client carries every
# backup off-box on its own, no extra script needed. The "AutoTrade_Backups"
# subfolder must exist under the signed-in account's My Drive -- create it
# once after Google Drive for Desktop is installed and signed in (an
# interactive step -- see docs/vps_deployment.md Section 8).
DEST_DIR = Path(r"C:\Users\Administrator\My Drive\AutoTrade_Backups")
KEEP_DAYS = 30


def backup_journal(src: Path, dest_dir: Path, stamp: str) -> Path:
    """SQLite online-backup of `src` into `dest_dir` (see module docstring
    for why this can't be a plain file copy)."""
    dest = dest_dir / f"trade_journal_paper_{stamp}.sqlite"
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    with dest_conn:
        src_conn.backup(dest_conn)
    src_conn.close()
    dest_conn.close()
    return dest


def copy_calendar_archive(src: Path, dest_dir: Path, stamp: str) -> Path:
    """Plain file copy of `src` into `dest_dir` (see module docstring for
    why this one doesn't need SQLite's online-backup machinery)."""
    dest = dest_dir / f"news_calendar_history_{stamp}.csv"
    shutil.copy2(src, dest)
    return dest


def prune_old(dest_dir: Path, pattern: str, keep_days: int) -> int:
    cutoff = datetime.now() - timedelta(days=keep_days)
    pruned = 0
    for f in dest_dir.glob(pattern):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            pruned += 1
    return pruned


def main() -> int:
    exit_code = 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not SRC.exists():
        print(f"Source DB not found at {SRC} -- nothing to back up.")
        exit_code = 1
    else:
        try:
            DEST_DIR.mkdir(parents=True, exist_ok=True)
            dest = backup_journal(SRC, DEST_DIR, stamp)
            print(f"Backed up {SRC} -> {dest}")
            pruned = prune_old(DEST_DIR, "trade_journal_paper_*.sqlite", KEEP_DAYS)
            if pruned:
                print(f"Pruned {pruned} backup(s) older than {KEEP_DAYS} days.")
        except Exception as exc:
            print(f"Journal backup failed: {exc}")
            exit_code = 1

    try:
        if not CALENDAR_SRC.exists():
            print(f"Calendar archive not found at {CALENDAR_SRC} -- nothing to back up yet.")
        else:
            DEST_DIR.mkdir(parents=True, exist_ok=True)
            dest = copy_calendar_archive(CALENDAR_SRC, DEST_DIR, stamp)
            print(f"Backed up {CALENDAR_SRC} -> {dest}")
            pruned = prune_old(DEST_DIR, "news_calendar_history_*.csv", KEEP_DAYS)
            if pruned:
                print(f"Pruned {pruned} calendar backup(s) older than {KEEP_DAYS} days.")
    except Exception as exc:
        print(f"Calendar archive backup failed: {exc}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
