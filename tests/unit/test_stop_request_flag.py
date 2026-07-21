"""Unit tests for common/stop_request_flag.py — pure filesystem logic, no MT5
dependency. Every test uses tmp_path so the real data/db/ flag is never
touched. Mirrors tests/unit/test_kill_switch_flag.py's structure/coverage,
adapted for this flag's request/is_requested/clear/get_status names."""
from __future__ import annotations

import pytest

from autotrade.common import stop_request_flag


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "stop_request.flag"


def test_is_requested_false_when_no_flag_file(flag_path):
    assert stop_request_flag.is_requested(flag_path) is False


def test_request_writes_readable_flag_with_reason(flag_path):
    stop_request_flag.request("manual stop button", flag_path)

    assert flag_path.exists()
    assert stop_request_flag.is_requested(flag_path) is True

    status = stop_request_flag.get_status(flag_path)
    assert status["reason"] == "manual stop button"
    assert status["requested_at"]


def test_request_rejects_empty_reason(flag_path):
    with pytest.raises(ValueError):
        stop_request_flag.request("", flag_path)
    with pytest.raises(ValueError):
        stop_request_flag.request("   ", flag_path)
    assert not flag_path.exists()


def test_request_creates_parent_directory(tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "stop_request.flag"
    stop_request_flag.request("test", nested_path)
    assert nested_path.exists()


def test_clear_clears_flag(flag_path):
    stop_request_flag.request("some reason", flag_path)
    assert stop_request_flag.is_requested(flag_path) is True

    stop_request_flag.clear(flag_path)
    assert stop_request_flag.is_requested(flag_path) is False
    assert stop_request_flag.get_status(flag_path) is None


def test_clear_is_a_noop_when_not_requested(flag_path):
    stop_request_flag.clear(flag_path)  # must not raise
    assert stop_request_flag.is_requested(flag_path) is False


def test_get_status_returns_none_when_not_requested(flag_path):
    assert stop_request_flag.get_status(flag_path) is None


def test_get_status_survives_corrupt_flag_file_as_requested(flag_path):
    flag_path.write_text("not valid json", encoding="utf-8")

    assert stop_request_flag.is_requested(flag_path) is True
    status = stop_request_flag.get_status(flag_path)
    assert status is not None
    assert status["reason"]


def test_default_flag_path_lives_under_data_db():
    assert stop_request_flag.DEFAULT_FLAG_PATH.parent.name == "db"
    assert stop_request_flag.DEFAULT_FLAG_PATH.name == "stop_request.flag"
