"""Unit tests for ops/pull_vps_backups.py -- loaded via importlib like
tests/unit/test_backup_db.py (ops/ has no __init__.py). Network-touching
functions (list_remote_files / pull_one) are monkeypatched for main()-level
tests; the pure selection logic and both failure-isolation guarantees are
covered: a failed remote LISTING must be a hard failure (never "nothing to
pull"), and a failed individual transfer must remove its partial local file
so the same-name-exists skip cannot mask it on the next run."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "pull_vps_backups.py"
_spec = importlib.util.spec_from_file_location("pull_vps_backups_script", SCRIPT_PATH)
pull_vps_backups = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pull_vps_backups
_spec.loader.exec_module(pull_vps_backups)


def test_missing_locally_skips_files_that_already_exist(tmp_path):
    (tmp_path / "a.sqlite").write_bytes(b"x")
    remote = ["a.sqlite", "b.sqlite", "c.csv"]

    assert pull_vps_backups.missing_locally(remote, tmp_path) == ["b.sqlite", "c.csv"]


def test_missing_locally_empty_remote_means_nothing_to_pull(tmp_path):
    assert pull_vps_backups.missing_locally([], tmp_path) == []


def test_main_listing_failure_is_a_hard_failure_not_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(pull_vps_backups, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(pull_vps_backups, "list_remote_files", lambda: None)

    assert pull_vps_backups.main() == 1


def test_main_pulls_only_missing_files_and_succeeds(tmp_path, monkeypatch):
    (tmp_path / "old.sqlite").write_bytes(b"x")
    pulled: list[str] = []
    monkeypatch.setattr(pull_vps_backups, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(pull_vps_backups, "list_remote_files", lambda: ["old.sqlite", "new.csv"])
    monkeypatch.setattr(pull_vps_backups, "pull_one", lambda name, d: pulled.append(name) or True)

    assert pull_vps_backups.main() == 0
    assert pulled == ["new.csv"]


def test_main_partial_pull_failure_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(pull_vps_backups, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(pull_vps_backups, "list_remote_files", lambda: ["a.csv", "b.csv"])
    monkeypatch.setattr(pull_vps_backups, "pull_one", lambda name, d: name == "a.csv")

    assert pull_vps_backups.main() == 1


def test_pull_one_removes_partial_file_on_transfer_failure(tmp_path, monkeypatch):
    class FakeResult:
        returncode = 1
        stderr = "connection lost"
        stdout = ""

    def fake_run(cmd, **kwargs):
        (tmp_path / "torn.sqlite").write_bytes(b"partial")  # simulate a torn transfer
        return FakeResult()

    monkeypatch.setattr(pull_vps_backups.subprocess, "run", fake_run)

    assert pull_vps_backups.pull_one("torn.sqlite", tmp_path) is False
    assert not (tmp_path / "torn.sqlite").exists()
