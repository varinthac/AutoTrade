"""Nightly trade-journal backup for the VPS -- see docs/vps_deployment.md
Section 8. Uses SQLite's own online-backup mechanism (Connection.backup())
rather than a raw file copy, since a plain copy while the DB is in WAL mode
can capture an inconsistent snapshot.

This is a deployment ops script, not part of the `autotrade` package --
paths are hardcoded to the VPS's real layout (C:\\AutoTrade), matching the
runbook's own convention (this script only ever runs on the VPS, never on
a dev machine).

Prunes backups older than KEEP_DAYS so the backups folder doesn't grow
unbounded -- not critical, just tidy.

    python ops\\backup_db.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(r"C:\AutoTrade\data\db\trade_journal_paper.sqlite")

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


def main() -> int:
    if not SRC.exists():
        print(f"Source DB not found at {SRC} -- nothing to back up.")
        return 1

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DEST_DIR / f"trade_journal_paper_{stamp}.sqlite"

    src_conn = sqlite3.connect(str(SRC))
    dest_conn = sqlite3.connect(str(dest))
    with dest_conn:
        src_conn.backup(dest_conn)
    src_conn.close()
    dest_conn.close()
    print(f"Backed up {SRC} -> {dest}")

    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    pruned = 0
    for f in DEST_DIR.glob("trade_journal_paper_*.sqlite"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            pruned += 1
    if pruned:
        print(f"Pruned {pruned} backup(s) older than {KEEP_DAYS} days.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
