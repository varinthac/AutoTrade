"""Dev-PC-side daily pull of the VPS's backup staging folder -- the
session-independent leg of the off-box backup path.

Why this exists (2026-08-04 incident, docs/vps_lean_plan.md defect D-2):
`ops/backup_db.py` on the VPS writes its nightly snapshots into the Google
Drive for Desktop mirror folder -- but GoogleDriveFS.exe only runs inside a
logged-on interactive session (HKCU Run key). After the 2026-08-04
cloudbase-init reboots broke auto-logon, the whole AutoTrade stack proved it
can run session-independent in Session 0 -- EXCEPT that sync client. The
nightly task kept reporting success while every snapshot (including the
irreplaceable news-calendar archive) piled up in a plain local folder that
never left the box. This script closes that gap from the OTHER side: the
dev PC (which already holds the working SSH key) PULLS the staging folder
daily, so the off-box copy no longer depends on any interactive session on
the VPS. Google Drive sync, when a session does exist, remains a bonus
second copy -- this pull is the one that is guaranteed.

Deployment (dev PC only, mirrors backup_db.py's "ops script with hardcoded
paths" convention -- see that file's docstring):

    python ops\\pull_vps_backups.py

Registered as a daily Task Scheduler task on the dev PC (13:00 local, ~30min
after the VPS's 00:30 VPS-local nightly backup lands). Exit code 0 only if
the remote listing succeeded AND every missing file transferred -- a partial
pull exits 1 so LastTaskResult surfaces it.

Incremental by filename: backup filenames are timestamped and immutable
(never rewritten after creation), so "local file with the same name exists"
is a safe skip. Local retention: everything is kept (the whole folder is
~half a MB per month; pruning can be added if that ever changes).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SSH_KEY = Path.home() / ".ssh" / "autotrade_vps"
VPS = "Administrator@38.247.162.198"
# scp needs forward slashes + quoting for the space in "My Drive".
REMOTE_DIR = "C:/Users/Administrator/My Drive/AutoTrade_Backups"
LOCAL_DIR = Path(r"D:\AutoTrade_Backups")

_SSH_BASE = ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", VPS]


def list_remote_files() -> list[str] | None:
    """Filenames in the VPS staging folder, or `None` if the listing itself
    failed (network/host down) -- callers must treat that as a hard failure,
    never as "nothing to pull"."""
    result = subprocess.run(
        [*_SSH_BASE, f'dir /b "{REMOTE_DIR.replace("/", chr(92))}"'],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"Remote listing failed (exit {result.returncode}): {result.stderr.strip()}")
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def missing_locally(remote_files: list[str], local_dir: Path) -> list[str]:
    """Backup filenames are timestamped-immutable, so same-name-exists is a
    safe skip (see module docstring)."""
    return [name for name in remote_files if not (local_dir / name).exists()]


def pull_one(name: str, local_dir: Path) -> bool:
    result = subprocess.run(
        [
            "scp", "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            f"{VPS}:{REMOTE_DIR}/{name}", str(local_dir / name),
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  FAILED {name}: {result.stderr.strip()}")
        # A partial file from a broken transfer must not satisfy tomorrow's
        # same-name-exists skip -- remove it so the next run retries.
        (local_dir / name).unlink(missing_ok=True)
        return False
    return True


def main() -> int:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    remote_files = list_remote_files()
    if remote_files is None:
        return 1

    to_pull = missing_locally(remote_files, LOCAL_DIR)
    if not to_pull:
        print(f"Up to date -- {len(remote_files)} remote file(s), nothing new.")
        return 0

    failures = 0
    for name in to_pull:
        print(f"Pulling {name} ...")
        if not pull_one(name, LOCAL_DIR):
            failures += 1
    print(f"Pulled {len(to_pull) - failures}/{len(to_pull)} new file(s) -> {LOCAL_DIR} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
