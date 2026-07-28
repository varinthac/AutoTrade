"""Tests for watchman/exit_conditions.py, Appendix A §4 items 4 and 6."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from autotrade.watchman.exit_conditions import check_structure_invalidation, check_time_stop


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- check_structure_invalidation -----------------------------------------


def test_buy_close_below_swing_low_invalidates():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},  # entry_swing_index=0, swing low=95
        {"close": 96.0, "low": 94.0, "high": 100.0},
        {"close": 94.5, "low": 93.0, "high": 96.0},  # close 94.5 < 95 -> invalidated
    ])
    assert check_structure_invalidation("BUY", entry_swing_index=0, df=df, as_of_index=2) is True


def test_buy_close_exactly_at_swing_low_is_not_invalidated_boundary_is_strict():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 96.0, "low": 94.0, "high": 100.0},
        {"close": 95.0, "low": 94.0, "high": 96.0},  # close == swing low exactly
    ])
    assert check_structure_invalidation("BUY", entry_swing_index=0, df=df, as_of_index=2) is False


def test_buy_close_above_swing_low_is_not_invalidated():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 98.0, "low": 96.0, "high": 100.0},
    ])
    assert check_structure_invalidation("BUY", entry_swing_index=0, df=df, as_of_index=1) is False


def test_sell_close_above_swing_high_invalidates():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},  # entry_swing_index=0, swing high=105
        {"close": 104.0, "low": 100.0, "high": 106.0},
        {"close": 106.0, "low": 103.0, "high": 108.0},  # close 106 > 105 -> invalidated
    ])
    assert check_structure_invalidation("SELL", entry_swing_index=0, df=df, as_of_index=2) is True


def test_sell_close_exactly_at_swing_high_is_not_invalidated_boundary_is_strict():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 104.0, "low": 100.0, "high": 106.0},
        {"close": 105.0, "low": 103.0, "high": 106.0},  # close == swing high exactly
    ])
    assert check_structure_invalidation("SELL", entry_swing_index=0, df=df, as_of_index=2) is False


def test_sell_close_below_swing_high_is_not_invalidated():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 102.0, "low": 100.0, "high": 104.0},
    ])
    assert check_structure_invalidation("SELL", entry_swing_index=0, df=df, as_of_index=1) is False


# --- entry_swing_level (2026-07-29 frame-shift bugfix) ----------------------


def test_buy_level_close_below_level_invalidates():
    df = _df([{"close": 94.5, "low": 93.0, "high": 96.0}])
    assert check_structure_invalidation(
        "BUY", entry_swing_index=0, df=df, as_of_index=0, entry_swing_level=95.0,
    ) is True


def test_buy_level_close_at_or_above_level_is_not_invalidated():
    df = _df([{"close": 95.0, "low": 93.0, "high": 96.0}])
    assert check_structure_invalidation(
        "BUY", entry_swing_index=0, df=df, as_of_index=0, entry_swing_level=95.0,
    ) is False


def test_sell_level_close_above_level_invalidates():
    df = _df([{"close": 106.0, "low": 103.0, "high": 108.0}])
    assert check_structure_invalidation(
        "SELL", entry_swing_index=0, df=df, as_of_index=0, entry_swing_level=105.0,
    ) is True


def test_sell_level_close_at_or_below_level_is_not_invalidated():
    df = _df([{"close": 105.0, "low": 103.0, "high": 106.0}])
    assert check_structure_invalidation(
        "SELL", entry_swing_index=0, df=df, as_of_index=0, entry_swing_level=105.0,
    ) is False


def test_level_takes_precedence_over_a_frame_shifted_index():
    # The exact live failure mode (2026-07-29): after a restart/reseed the
    # recorded index points at a DIFFERENT bar whose high (104.0 here) says
    # "invalidated", while the TRUE entry swing level (108.0) says the
    # structure is intact -- the level must win.
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 104.0},   # frame-shifted index target: high=104
        {"close": 106.0, "low": 103.0, "high": 107.0},  # close 106 > 104 but < 108
    ])
    assert check_structure_invalidation(
        "SELL", entry_swing_index=0, df=df, as_of_index=1, entry_swing_level=108.0,
    ) is False
    # And without the level (legacy record), the shifted index wrongly fires:
    assert check_structure_invalidation(
        "SELL", entry_swing_index=0, df=df, as_of_index=1,
    ) is True


def test_level_path_ignores_the_index_bounds_guard():
    # With a level present, entry_swing_index is ignored entirely -- even an
    # out-of-bounds index (the exact symptom the 2026-07-28 restart produced)
    # must not raise, because the level makes the positional lookup moot.
    df = _df([{"close": 100.0, "low": 95.0, "high": 105.0}])
    assert check_structure_invalidation(
        "SELL", entry_swing_index=212, df=df, as_of_index=0, entry_swing_level=105.5,
    ) is False


def test_entry_swing_index_equal_to_as_of_index_is_allowed():
    df = _df([{"close": 94.0, "low": 95.0, "high": 105.0}])
    # as_of_index == entry_swing_index -- allowed boundary, same bar.
    assert check_structure_invalidation("BUY", entry_swing_index=0, df=df, as_of_index=0) is True


def test_entry_swing_index_after_as_of_index_raises():
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 96.0, "low": 94.0, "high": 100.0},
    ])
    with pytest.raises(ValueError):
        check_structure_invalidation("BUY", entry_swing_index=1, df=df, as_of_index=0)


def test_entry_swing_index_after_as_of_index_raises_valueerror_with_both_indices_in_message():
    # Pins the exact exception type AND message content (not just "raises
    # something") -- so a future refactor that silently changes this to a
    # different exception type or a vague message would be caught.
    df = _df([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 96.0, "low": 94.0, "high": 100.0},
        {"close": 94.0, "low": 93.0, "high": 96.0},
    ])
    with pytest.raises(ValueError, match=r"entry_swing_index \(2\) must be <= as_of_index \(1\)"):
        check_structure_invalidation("BUY", entry_swing_index=2, df=df, as_of_index=1)


def test_invalid_direction_raises():
    df = _df([{"close": 100.0, "low": 95.0, "high": 105.0}])
    with pytest.raises(ValueError):
        check_structure_invalidation("HOLD", entry_swing_index=0, df=df, as_of_index=0)


# --- check_time_stop --------------------------------------------------------


ENTRY = 100.0
STOP_DISTANCE = 10.0
TIME_STOP_HOURS = 48.0
DEAD_TRADE_R_BAND = 0.3


def test_before_time_stop_hours_never_closes_even_if_dead():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=47, minutes=59)
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=ENTRY,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is False


def test_exactly_at_time_stop_hours_boundary_is_inclusive():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=48)
    # profit_r = 0 -- within the dead band.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=ENTRY,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is True


def test_after_time_stop_hours_but_outside_r_band_does_not_close():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    # profit_r = (105 - 100) / 10 = 0.5, outside +/-0.3 band.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=105.0,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is False


def test_r_band_upper_boundary_is_inclusive_buy():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    # profit_r = (103 - 100) / 10 = 0.3, exactly at the upper band edge.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=103.0,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is True


def test_r_band_lower_boundary_is_inclusive_buy():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    # profit_r = (97 - 100) / 10 = -0.3, exactly at the lower band edge.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=97.0,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is True


def test_r_band_just_outside_lower_boundary_does_not_close():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    # profit_r = (96.99 - 100) / 10 = -0.301, just outside the -0.3 edge.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=96.99,
        initial_stop_distance=STOP_DISTANCE, direction="BUY",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is False


def test_sell_direction_mirrors_buy():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    # profit_r = (100 - 97) / 10 = 0.3 for SELL, exactly at the boundary.
    assert check_time_stop(
        opened_at, now, entry_price=ENTRY, current_price=97.0,
        initial_stop_distance=STOP_DISTANCE, direction="SELL",
        time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
    ) is True


def test_invalid_direction_raises_for_time_stop():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    with pytest.raises(ValueError):
        check_time_stop(
            opened_at, now, entry_price=ENTRY, current_price=100.0,
            initial_stop_distance=STOP_DISTANCE, direction="HOLD",
            time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
        )


def test_nonpositive_initial_stop_distance_raises_for_time_stop():
    opened_at = datetime(2026, 7, 19, 9, 0)
    now = opened_at + timedelta(hours=49)
    with pytest.raises(ValueError):
        check_time_stop(
            opened_at, now, entry_price=ENTRY, current_price=100.0,
            initial_stop_distance=0.0, direction="BUY",
            time_stop_hours=TIME_STOP_HOURS, dead_trade_r_band=DEAD_TRADE_R_BAND,
        )
