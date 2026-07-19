"""Tests for features/levels.py."""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.features.levels import (
    daily_pivot,
    is_near_key_level,
    nearest_round_number,
    prior_day_ohlc,
)


def test_daily_pivot_is_average_of_high_low_close():
    assert daily_pivot(prior_day_high=110.0, prior_day_low=100.0, prior_day_close=105.0) == pytest.approx(105.0)


def _two_day_h1_df():
    # Day 1 (2026-07-18): 24 H1 bars, index 0..23. High 110 at 09:00 (idx 9),
    # low 95 at 03:00 (idx 3), last close (23:00, idx 23) = 108.
    day1_times = pd.date_range("2026-07-18 00:00", periods=24, freq="h")
    day1_high = [100.0] * 24
    day1_high[9] = 110.0
    day1_low = [100.0] * 24
    day1_low[3] = 95.0
    day1_close = [100.0] * 24
    day1_close[23] = 108.0

    # Day 2 (2026-07-19): partial, 6 H1 bars, index 24..29 -- still "today"
    # as of the last bar, so it must NOT be included in the prior-day OHLC.
    day2_times = pd.date_range("2026-07-19 00:00", periods=6, freq="h")
    day2_high = [200.0] * 6
    day2_low = [190.0] * 6
    day2_close = [195.0] * 6

    return pd.DataFrame(
        {
            "time": list(day1_times) + list(day2_times),
            "high": day1_high + day2_high,
            "low": day1_low + day2_low,
            "close": day1_close + day2_close,
        }
    )


def test_prior_day_ohlc_uses_the_last_fully_completed_server_day_only():
    df = _two_day_h1_df()

    high, low, close = prior_day_ohlc(df, as_of_index=29)

    assert (high, low, close) == (110.0, 95.0, 108.0)


def test_prior_day_ohlc_raises_when_no_completed_prior_day_exists():
    df = _two_day_h1_df()

    with pytest.raises(ValueError):
        prior_day_ohlc(df, as_of_index=2)  # still within day 1, no prior day yet


def test_nearest_round_number_gold_rounds_to_nearest_half():
    # 4018.17 is 0.17 from 4018.00 and 0.33 from 4018.50 -> nearest is 4018.00.
    assert nearest_round_number(4018.17, symbol_digits=2) == pytest.approx(4018.0)


def test_nearest_round_number_forex_rounds_to_nearest_50_pip_level():
    # 1.10532 is 0.00032 from 1.10500 and 0.00468 from 1.11000 -> nearest is 1.10500.
    assert nearest_round_number(1.10532, symbol_digits=5) == pytest.approx(1.105)


def test_is_near_key_level_true_within_half_atr():
    assert is_near_key_level(4018.3, atr_value=1.0, levels=[4018.0, 4025.0], multiple=0.5) is True


def test_is_near_key_level_false_outside_half_atr():
    assert is_near_key_level(4020.0, atr_value=1.0, levels=[4018.0, 4025.0], multiple=0.5) is False
