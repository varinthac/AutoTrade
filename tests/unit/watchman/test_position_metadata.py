"""Tests for watchman/position_metadata.py -- round-trip and simulated
restart survival, same pattern as tests/unit/risk/test_circuit_breaker.py's
persistence tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from autotrade.watchman.position_metadata import (
    DEFAULT_STATE_PATH,
    CorruptPositionMetadataError,
    get_position_metadata,
    record_position_opened,
    remove_position_metadata,
)


def test_default_state_path_lives_under_data_db():
    assert DEFAULT_STATE_PATH.parent.name == "db"
    assert DEFAULT_STATE_PATH.name == "position_metadata.json"


def test_get_position_metadata_missing_ticket_returns_none(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    assert get_position_metadata(12345, state_path=state_path) is None


def test_record_and_get_round_trips_every_field(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=1001,
        symbol="XAUUSD",
        direction="BUY",
        entry_price=2400.0,
        initial_stop_distance=12.5,
        entry_swing_index=250,
        opened_at=opened_at,
        state_path=state_path,
        entry_swing_level=2387.5,
    )

    meta = get_position_metadata(1001, state_path=state_path)

    assert meta is not None
    assert meta.ticket == 1001
    assert meta.symbol == "XAUUSD"
    assert meta.direction == "BUY"
    assert meta.entry_price == 2400.0
    assert meta.initial_stop_distance == 12.5
    assert meta.entry_swing_index == 250
    assert meta.opened_at == opened_at
    assert meta.entry_swing_level == 2387.5


def test_legacy_record_without_entry_swing_level_loads_as_none(tmp_path):
    # Records written before the 2026-07-29 frame-shift bugfix have no
    # entry_swing_level key at all -- must load as None, not crash.
    import json

    state_path = tmp_path / "position_metadata.json"
    state_path.write_text(json.dumps({
        "1002": {
            "symbol": "XAUUSD", "direction": "SELL", "entry_price": 4045.49,
            "initial_stop_distance": 30.0, "entry_swing_index": 212,
            "opened_at": "2026-07-28T05:00:04", "news_protected_until": None,
            "entry_spread_points": 5.0, "actual_slippage": 0.3,
        }
    }), encoding="utf-8")

    meta = get_position_metadata(1002, state_path=state_path)

    assert meta is not None
    assert meta.entry_swing_level is None
    assert meta.entry_swing_index == 212


def test_state_survives_a_fresh_process_pointed_at_the_same_file(tmp_path):
    # Simulates a process restart: nothing carried over except the file.
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=2002,
        symbol="EURUSD",
        direction="SELL",
        entry_price=1.10,
        initial_stop_distance=0.0025,
        entry_swing_index=99,
        opened_at=opened_at,
        state_path=state_path,
    )

    # No in-memory state at all here -- a fresh call reads straight from disk.
    meta = get_position_metadata(2002, state_path=state_path)

    assert meta.symbol == "EURUSD"
    assert meta.direction == "SELL"
    assert meta.entry_price == 1.10
    assert meta.initial_stop_distance == 0.0025
    assert meta.entry_swing_index == 99
    assert meta.opened_at == opened_at


def test_multiple_tickets_coexist_independently(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=5.0, entry_swing_index=10, opened_at=opened_at,
        state_path=state_path,
    )
    record_position_opened(
        ticket=2, symbol="EURUSD", direction="SELL", entry_price=1.1,
        initial_stop_distance=0.002, entry_swing_index=20, opened_at=opened_at,
        state_path=state_path,
    )

    meta1 = get_position_metadata(1, state_path=state_path)
    meta2 = get_position_metadata(2, state_path=state_path)

    assert meta1.symbol == "XAUUSD"
    assert meta2.symbol == "EURUSD"


def test_record_position_opened_overwrites_existing_ticket(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=5.0, entry_swing_index=10, opened_at=opened_at,
        state_path=state_path,
    )
    record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=105.0,
        initial_stop_distance=6.0, entry_swing_index=15, opened_at=opened_at,
        state_path=state_path,
    )

    meta = get_position_metadata(1, state_path=state_path)
    assert meta.entry_price == 105.0
    assert meta.initial_stop_distance == 6.0
    assert meta.entry_swing_index == 15


def test_remove_position_metadata_deletes_the_record(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=5.0, entry_swing_index=10, opened_at=opened_at,
        state_path=state_path,
    )
    remove_position_metadata(1, state_path=state_path)

    assert get_position_metadata(1, state_path=state_path) is None


def test_remove_position_metadata_missing_ticket_is_a_no_op(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    # No record ever written -- must not raise.
    remove_position_metadata(999, state_path=state_path)
    assert not state_path.exists()


def test_remove_does_not_affect_other_tickets(tmp_path):
    state_path = tmp_path / "position_metadata.json"
    opened_at = datetime(2026, 7, 19, 9, 0)

    record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=5.0, entry_swing_index=10, opened_at=opened_at,
        state_path=state_path,
    )
    record_position_opened(
        ticket=2, symbol="EURUSD", direction="SELL", entry_price=1.1,
        initial_stop_distance=0.002, entry_swing_index=20, opened_at=opened_at,
        state_path=state_path,
    )

    remove_position_metadata(1, state_path=state_path)

    assert get_position_metadata(1, state_path=state_path) is None
    assert get_position_metadata(2, state_path=state_path) is not None


def test_corrupt_state_file_raises_corrupt_position_metadata_error(tmp_path):
    # A present-but-corrupt file must NOT be silently treated as an empty
    # store -- that would make every real open position invisible with no
    # signal anything went wrong. It must raise loudly instead.
    state_path = tmp_path / "position_metadata.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CorruptPositionMetadataError):
        get_position_metadata(1, state_path=state_path)


def test_no_state_path_uses_default_path_argument_is_optional(tmp_path, monkeypatch):
    # record_position_opened/get_position_metadata must accept omitting
    # state_path entirely (falls back to DEFAULT_STATE_PATH) without raising
    # -- redirect DEFAULT_STATE_PATH itself so this test doesn't touch the
    # real repo's data/db directory.
    import autotrade.watchman.position_metadata as pm

    fake_default = tmp_path / "position_metadata.json"
    monkeypatch.setattr(pm, "DEFAULT_STATE_PATH", fake_default)

    opened_at = datetime(2026, 7, 19, 9, 0)
    pm.record_position_opened(
        ticket=1, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=5.0, entry_swing_index=10, opened_at=opened_at,
    )

    assert fake_default.exists()
    meta = pm.get_position_metadata(1)
    assert meta.symbol == "XAUUSD"
