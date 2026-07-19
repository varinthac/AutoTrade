"""Unit tests for common/kill_switch_flag.py — pure filesystem logic, no MT5
dependency. Every test uses tmp_path so the real data/db/ flag is never
touched."""
from __future__ import annotations

import pytest

from autotrade.common import kill_switch_flag


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "kill_switch.flag"


def test_is_active_false_when_no_flag_file(flag_path):
    assert kill_switch_flag.is_active(flag_path) is False


def test_activate_writes_readable_flag_with_reason(flag_path):
    kill_switch_flag.activate("daily loss limit breached", flag_path)

    assert flag_path.exists()
    assert kill_switch_flag.is_active(flag_path) is True

    status = kill_switch_flag.get_status(flag_path)
    assert status["reason"] == "daily loss limit breached"
    assert status["activated_at"]


def test_activate_rejects_empty_reason(flag_path):
    with pytest.raises(ValueError):
        kill_switch_flag.activate("", flag_path)
    with pytest.raises(ValueError):
        kill_switch_flag.activate("   ", flag_path)
    assert not flag_path.exists()


def test_activate_creates_parent_directory(tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "kill_switch.flag"
    kill_switch_flag.activate("test", nested_path)
    assert nested_path.exists()


def test_deactivate_clears_flag(flag_path):
    kill_switch_flag.activate("some reason", flag_path)
    assert kill_switch_flag.is_active(flag_path) is True

    kill_switch_flag.deactivate(flag_path)
    assert kill_switch_flag.is_active(flag_path) is False
    assert kill_switch_flag.get_status(flag_path) is None


def test_deactivate_is_a_noop_when_not_active(flag_path):
    kill_switch_flag.deactivate(flag_path)  # must not raise
    assert kill_switch_flag.is_active(flag_path) is False


def test_get_status_returns_none_when_not_active(flag_path):
    assert kill_switch_flag.get_status(flag_path) is None


def test_get_status_survives_corrupt_flag_file_as_active(flag_path):
    flag_path.write_text("not valid json", encoding="utf-8")

    assert kill_switch_flag.is_active(flag_path) is True
    status = kill_switch_flag.get_status(flag_path)
    assert status is not None
    assert status["reason"]


def test_default_flag_path_lives_under_data_db():
    assert kill_switch_flag.DEFAULT_FLAG_PATH.parent.name == "db"
    assert kill_switch_flag.DEFAULT_FLAG_PATH.name == "kill_switch.flag"
