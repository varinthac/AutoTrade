"""Unit tests for common/pid_file.py — filesystem logic uses tmp_path so the
real data/db/shadow_loop.pid is never touched; is_pid_running's own
subprocess call is monkeypatched everywhere except its own dedicated test
(which uses this process's own real, genuinely-running PID -- no need to
shell out to a fake process to prove the real tasklist call works)."""
from __future__ import annotations

import os
import subprocess

import pytest

from autotrade.common import pid_file


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shadow_loop.pid"


def test_read_returns_none_when_no_file(path):
    assert pid_file.read(path) is None


def test_write_then_read_round_trips(path):
    pid_file.write(12345, path)
    assert pid_file.read(path) == 12345


def test_write_creates_parent_directory(tmp_path):
    nested = tmp_path / "nested" / "dir" / "shadow_loop.pid"
    pid_file.write(999, nested)
    assert pid_file.read(nested) == 999


def test_read_returns_none_for_corrupt_file(path):
    path.write_text("not a pid", encoding="utf-8")
    assert pid_file.read(path) is None


# --- write()'s exclusive-create / TOCTOU-narrowing behavior ----------------


def test_write_raises_and_does_not_overwrite_when_existing_pid_is_alive(path, monkeypatch):
    pid_file.write(111, path)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 111)

    with pytest.raises(FileExistsError):
        pid_file.write(222, path)

    assert pid_file.read(path) == 111  # untouched -- refused rather than clobbered


def test_write_removes_stale_pid_file_and_retries_successfully(path, monkeypatch):
    pid_file.write(111, path)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: False)  # 111 is stale

    pid_file.write(222, path)  # must not raise -- stale file is replaced

    assert pid_file.read(path) == 222


def test_write_treats_unreadable_existing_file_as_stale_and_overwrites(path, monkeypatch):
    path.write_text("not a pid", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: calls.append(pid) or True)

    pid_file.write(333, path)  # unreadable existing PID -- nothing safe to refuse over

    assert pid_file.read(path) == 333
    assert calls == []  # is_pid_running never called -- read() itself returned None


def test_write_does_not_shell_out_to_tasklist_when_no_pre_existing_file(path, monkeypatch):
    calls = []
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: calls.append(pid) or True)

    pid_file.write(444, path)

    assert calls == []  # exclusive-create succeeded on the first attempt -- no liveness check needed
    assert pid_file.read(path) == 444


def test_remove_deletes_file(path):
    pid_file.write(1, path)
    pid_file.remove(path)
    assert not path.exists()
    assert pid_file.read(path) is None


def test_remove_is_a_noop_when_no_file(path):
    pid_file.remove(path)  # must not raise
    assert not path.exists()


def test_is_running_false_when_no_file(path, monkeypatch):
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: True)
    assert pid_file.is_running(path) is False


def test_is_running_true_when_pid_alive(path, monkeypatch):
    pid_file.write(555, path)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 555)
    assert pid_file.is_running(path) is True


def test_is_running_false_when_pid_stale(path, monkeypatch):
    pid_file.write(555, path)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: False)
    assert pid_file.is_running(path) is False


def test_default_pid_path_lives_under_data_db():
    assert pid_file.DEFAULT_PID_PATH.parent.name == "db"
    assert pid_file.DEFAULT_PID_PATH.name == "shadow_loop.pid"


def test_is_pid_running_true_for_this_process_real_tasklist_call():
    assert pid_file.is_pid_running(os.getpid()) is True


def test_is_pid_running_false_for_an_implausibly_large_pid_real_tasklist_call():
    # PIDs this large cannot exist -- a genuine, deterministic "not running"
    # case for the real (unmocked) tasklist subprocess call.
    assert pid_file.is_pid_running(999_999_999) is False


def _fake_tasklist_run(stdout: str):
    class _FakeCompletedProcess:
        pass

    def _fake_run(args, capture_output, text, check):
        result = _FakeCompletedProcess()
        result.stdout = stdout
        result.stderr = ""
        result.returncode = 0
        return result

    return _fake_run


def test_is_pid_running_true_when_stdout_contains_an_exact_matching_row(monkeypatch):
    # Realistic `tasklist /FI "PID eq 1234" /NH` output for a genuine match.
    stdout = "python.exe                   1234 Console                    1     54,321 K\n"
    monkeypatch.setattr(subprocess, "run", _fake_tasklist_run(stdout))

    assert pid_file.is_pid_running(1234) is True


def test_is_pid_running_false_when_target_pid_is_only_a_substring_of_other_running_pids(monkeypatch):
    """Regression test for a false-positive risk in is_pid_running's
    `str(pid) in result.stdout` check: PID 123 must NOT be reported as
    running just because it is a numeric substring of other PIDs (1234,
    9123) that happen to appear in the tasklist output. A correct
    implementation must match the PID as its own field/token, not as a
    raw substring anywhere in the output blob."""
    stdout = (
        "python.exe                   1234 Console                    1     54,321 K\n"
        "node.exe                     9123 Console                    1     12,345 K\n"
    )
    monkeypatch.setattr(subprocess, "run", _fake_tasklist_run(stdout))

    assert pid_file.is_pid_running(123) is False


def test_is_pid_running_false_when_tasklist_reports_no_matching_tasks(monkeypatch):
    stdout = "INFO: No tasks are running which match the specified criteria.\n"
    monkeypatch.setattr(subprocess, "run", _fake_tasklist_run(stdout))

    assert pid_file.is_pid_running(1234) is False
