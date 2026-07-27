"""Unit tests for common/manual_halt_flag.py — pure filesystem logic, no MT5
dependency. Mirrors tests/unit/test_kill_switch_flag.py's structure. Every
test uses tmp_path so the real data/db/ flag is never touched."""
from __future__ import annotations

import pytest

from autotrade.common import manual_halt_flag


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "manual_halt.flag"


def test_is_active_false_when_no_flag_file(flag_path):
    assert manual_halt_flag.is_active(flag_path) is False


def test_activate_writes_readable_flag_with_reason(flag_path):
    manual_halt_flag.activate("manual stop button", flag_path)

    assert flag_path.exists()
    assert manual_halt_flag.is_active(flag_path) is True

    status = manual_halt_flag.get_status(flag_path)
    assert status["reason"] == "manual stop button"
    assert status["activated_at"]


def test_activate_rejects_empty_reason(flag_path):
    with pytest.raises(ValueError):
        manual_halt_flag.activate("", flag_path)
    with pytest.raises(ValueError):
        manual_halt_flag.activate("   ", flag_path)
    assert not flag_path.exists()


def test_activate_creates_parent_directory(tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "manual_halt.flag"
    manual_halt_flag.activate("test", nested_path)
    assert nested_path.exists()


def test_deactivate_clears_flag(flag_path):
    manual_halt_flag.activate("some reason", flag_path)
    assert manual_halt_flag.is_active(flag_path) is True

    manual_halt_flag.deactivate(flag_path)
    assert manual_halt_flag.is_active(flag_path) is False
    assert manual_halt_flag.get_status(flag_path) is None


def test_deactivate_is_a_noop_when_not_active(flag_path):
    manual_halt_flag.deactivate(flag_path)  # must not raise
    assert manual_halt_flag.is_active(flag_path) is False


def test_get_status_returns_none_when_not_active(flag_path):
    assert manual_halt_flag.get_status(flag_path) is None


def test_get_status_survives_corrupt_flag_file_as_active(flag_path):
    flag_path.write_text("not valid json", encoding="utf-8")

    assert manual_halt_flag.is_active(flag_path) is True
    status = manual_halt_flag.get_status(flag_path)
    assert status is not None
    assert status["reason"]


def test_default_flag_path_lives_under_data_db():
    assert manual_halt_flag.DEFAULT_FLAG_PATH.parent.name == "db"
    assert manual_halt_flag.DEFAULT_FLAG_PATH.name == "manual_halt.flag"
