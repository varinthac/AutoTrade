"""Unit tests for backtest/clock.py's SimulatedClock -- the backtest
implementation of common/clock.py's Clock protocol. Unlike RealClock, this
one never reads the OS clock: `.now()` must return exactly whatever `.set()`
last wired in, deterministically (no bounding window needed)."""
from __future__ import annotations

from datetime import datetime, timezone

from autotrade.backtest.clock import SimulatedClock


def test_now_returns_the_constructor_supplied_initial_time():
    initial = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    clock = SimulatedClock(initial)

    assert clock.now() == initial


def test_set_then_now_round_trips_exactly():
    clock = SimulatedClock(datetime(2020, 1, 1))

    new_time = datetime(2026, 7, 19, 23, 59, 1)
    clock.set(new_time)

    assert clock.now() == new_time
    assert clock.now() is new_time  # no copying/coercion, the exact object set


def test_repeated_set_calls_overwrite_not_accumulate():
    clock = SimulatedClock(datetime(2020, 1, 1))

    clock.set(datetime(2021, 6, 1))
    clock.set(datetime(2022, 6, 1))
    clock.set(datetime(2023, 6, 1))

    assert clock.now() == datetime(2023, 6, 1)
