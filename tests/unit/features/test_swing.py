"""Golden-file / boundary tests for features/swing.py.

Fixtures are small and hand-traceable: the swing/pivot location is chosen so
it can be verified by inspection (each candidate's 3-bar-each-side neighbors
are explicitly listed in comments), independent of swing.py's own code.
"""
from __future__ import annotations

import pandas as pd

from autotrade.features.swing import (
    detect_swings,
    is_higher_low,
    is_lower_high,
    latest_confirmed_swing_high,
    latest_confirmed_swing_low,
)


def _df(highs, lows, closes):
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


# A single unambiguous fractal swing low at index 5: lows[2:5] = [8,7,6] are
# all > 3, and lows[6:9] = [7,8,9] are all > 3.
_SWING_LOW_LOWS = [10, 9, 8, 7, 6, 3, 7, 8, 9, 10, 11, 12, 13, 14, 15]
_SWING_LOW_HIGHS = [v + 2 for v in _SWING_LOW_LOWS]
_SWING_LOW_CLOSES = [v + 1 for v in _SWING_LOW_LOWS]
_SWING_INDEX = 5


def test_detect_swings_finds_the_single_known_swing_low_and_nothing_else():
    df = _df(_SWING_LOW_HIGHS, _SWING_LOW_LOWS, _SWING_LOW_CLOSES)

    swings = detect_swings(df, pivot_bars=3)

    assert swings.index[swings["swing_low"]].tolist() == [_SWING_INDEX]


def test_latest_confirmed_swing_low_not_visible_two_bars_after_forming():
    # Confirmation requires bar swing_index + pivot_bars (= 8) to have
    # closed. as_of_index = swing_index + 2 = 7 is one bar too early.
    df = _df(_SWING_LOW_HIGHS, _SWING_LOW_LOWS, _SWING_LOW_CLOSES)

    result = latest_confirmed_swing_low(df, as_of_index=_SWING_INDEX + 2, pivot_bars=3)

    assert result is None


def test_latest_confirmed_swing_low_becomes_visible_exactly_three_bars_after_forming():
    df = _df(_SWING_LOW_HIGHS, _SWING_LOW_LOWS, _SWING_LOW_CLOSES)

    result = latest_confirmed_swing_low(df, as_of_index=_SWING_INDEX + 3, pivot_bars=3)

    assert result == (_SWING_INDEX, 3.0)


def test_latest_confirmed_swing_low_raises_on_out_of_bounds_as_of_index():
    df = _df(_SWING_LOW_HIGHS, _SWING_LOW_LOWS, _SWING_LOW_CLOSES)

    import pytest

    with pytest.raises(ValueError):
        latest_confirmed_swing_low(df, as_of_index=len(df), pivot_bars=3)


# Two confirmed swing lows: index 5 (value 5, deeper) then index 14 (value 8,
# a higher low). Symmetric swing-high fixture (index 5 value 25, then index
# 14 value 22, a lower high) is derived by the linear transform high = 30 -
# low, which preserves the local-max/local-min neighbor relationships.
_HL_LOWS = [20, 18, 16, 14, 12, 5, 12, 14, 16, 18, 20, 18, 16, 14, 8, 14, 16, 18, 20, 22]
_HL_HIGHS = [v + 10 for v in _HL_LOWS]
_HL_CLOSES = [v + 2 for v in _HL_LOWS]

_LH_HIGHS = [30 - v for v in _HL_LOWS]
_LH_LOWS = [v - 10 for v in _LH_HIGHS]
_LH_CLOSES = [v - 2 for v in _LH_HIGHS]


def test_latest_confirmed_swing_low_finds_both_swing_lows_in_order():
    df = _df(_HL_HIGHS, _HL_LOWS, _HL_CLOSES)

    assert latest_confirmed_swing_low(df, as_of_index=9, pivot_bars=3) == (5, 5.0)
    assert latest_confirmed_swing_low(df, as_of_index=17, pivot_bars=3) == (14, 8.0)


def test_is_higher_low_true_once_second_higher_swing_low_confirmed_and_price_holds_above_it():
    df = _df(_HL_HIGHS, _HL_LOWS, _HL_CLOSES)

    # Only the first swing low (index 5) is confirmed yet -- not enough
    # swings to compare "higher than the one before".
    assert is_higher_low(df, as_of_index=9, pivot_bars=3) is False

    # Both swing lows confirmed (5 -> 8, a higher low) and close[17]=20 > 8.
    assert is_higher_low(df, as_of_index=17, pivot_bars=3) is True


def test_is_lower_high_true_once_second_lower_swing_high_confirmed_and_price_holds_below_it():
    df = _df(_LH_HIGHS, _LH_LOWS, _LH_CLOSES)

    assert is_lower_high(df, as_of_index=9, pivot_bars=3) is False
    # Swing highs confirmed: 25 (index5) -> 22 (index14), a lower high, and
    # close[17] = highs[17] - 2 = 12 - 2 = 10 < 22.
    assert is_lower_high(df, as_of_index=17, pivot_bars=3) is True


def test_latest_confirmed_swing_high_symmetric_to_swing_low():
    df = _df(_LH_HIGHS, _LH_LOWS, _LH_CLOSES)

    assert latest_confirmed_swing_high(df, as_of_index=17, pivot_bars=3) == (14, 22.0)
