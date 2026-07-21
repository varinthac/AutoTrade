"""Cooperative PID-file liveness tracking -- backs both
`scripts/run_shadow_loop.py`'s double-launch guard (refuse to start a second
instance while one is already running) and `scripts/autotrade_control.py`'s
`status` subcommand (report whether the loop is currently running). Same
simple-file-persistence pattern as `common/kill_switch_flag.py`/
`common/stop_request_flag.py`, but recording a live PID rather than a
boolean/reason flag.

Liveness is checked via a `tasklist /FI "PID eq <pid>"` subprocess call
rather than adding the `psutil` package as a new dependency, or a
Windows-specific `os.kill(pid, 0)`-style probe (no reliable POSIX-style
"does this PID exist" primitive on Windows). `is_pid_running` is kept a
standalone, easily-mockable function for exactly that reason -- tests must
never shell out to a real `tasklist`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from autotrade.common.config import REPO_ROOT

DEFAULT_PID_PATH = REPO_ROOT / "data" / "db" / "shadow_loop.pid"


def is_pid_running(pid: int) -> bool:
    """True if `tasklist` reports a row for `pid`. Matches the PID as its own
    whitespace-separated field (`tasklist /NH`'s default table format is
    `image_name  pid  session_name  session#  mem_usage`, so the PID is
    always the second token of a matching line) rather than a raw substring
    of the whole stdout blob -- a substring check would false-positive on
    PID 123 whenever 1234/9123/etc. also appear anywhere in the output
    (e.g. as another process's PID), which would incorrectly report a dead
    process as alive. `/FI "PID eq <pid>"` already filters server-side to
    at most one matching row, so this is defense-in-depth, not the primary
    filter. Known simplification: this assumes no image name itself
    contains embedded whitespace (true for essentially every real Windows
    executable name) -- documented rather than hidden, same as this
    codebase's other "good enough for now" simplifications."""
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, check=False,
    )
    target = str(pid)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == target:
            return True
    return False


def read(pid_path: Path | None = None) -> int | None:
    """The PID recorded at `pid_path`, or None if the file is absent or
    unreadable -- an unreadable file is treated the same as "no PID file"
    here (unlike kill_switch_flag/stop_request_flag's fail-toward-active
    convention): a PID file this system can't even parse can never be
    confirmed to belong to a genuinely-running process, so there is nothing
    safe to refuse startup over."""
    path = pid_path or DEFAULT_PID_PATH
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write(pid: int, pid_path: Path | None = None) -> None:
    """Claims `pid_path` for `pid` via an exclusive-create open (`"x"` mode
    -- raises `FileExistsError` if the file already exists) rather than a
    plain overwrite. This closes most of the TOCTOU window between a
    caller's own `read()`-then-`is_pid_running()` pre-check and this write:
    two near-simultaneous callers (e.g. an accidental double-click of
    AutoTrade_Start.bat) can no longer both succeed just because both
    observed "no live PID" before either wrote -- only one atomic
    create-or-fail syscall can win.

    If the file already exists, its recorded PID is checked: a genuinely
    running one re-raises `FileExistsError` (refuse to clobber a live
    instance's PID file); a stale/unreadable one is removed and the
    exclusive-create retried once. This is NOT a full file-lock primitive
    (over-engineering for this codebase's simple file-based coordination
    pattern) -- a truly simultaneous OS-level race between two claimants
    both retrying at the same instant is not guaranteed to be caught, but
    this closes the window from "two sequential Python-level read-then-write
    calls" down to "one atomic create-or-fail syscall plus one bounded
    retry", a real, meaningful improvement."""
    path = pid_path or DEFAULT_PID_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _create_exclusive(path, pid)
        return
    except FileExistsError:
        pass

    existing = read(path)
    if existing is not None and is_pid_running(existing):
        raise FileExistsError(
            f"{path} already records a running process (PID {existing}) -- refusing to overwrite it"
        )

    path.unlink(missing_ok=True)
    _create_exclusive(path, pid)


def _create_exclusive(path: Path, pid: int) -> None:
    with path.open("x", encoding="utf-8") as f:
        f.write(str(pid))


def remove(pid_path: Path | None = None) -> None:
    path = pid_path or DEFAULT_PID_PATH
    path.unlink(missing_ok=True)


def is_running(pid_path: Path | None = None) -> bool:
    """True if `pid_path` records a PID that is genuinely still running --
    False for no file, an unreadable file, or a stale (dead) PID."""
    pid = read(pid_path)
    if pid is None:
        return False
    return is_pid_running(pid)
