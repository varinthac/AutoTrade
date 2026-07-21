"""Golden-file tests for features/indicators.py.

Every expected value here is produced by an INDEPENDENT calculation path --
either exact fraction arithmetic traced by hand (EMA/RSI/ATR small fixtures)
or a from-scratch plain-Python recursive loop (MACD, a longer fixture) --
never by re-deriving indicators.py's own pandas formula. This is specifically
to catch a subtly-wrong-but-plausible formula that a test merely re-asserting
the same code would never catch.
"""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.features.indicators import BARS_PER_DAY_H1, ROLLING_AVG_DAYS, atr, ema, macd_histogram, rolling_average, rsi


def test_ema_matches_hand_traced_recursive_formula():
    # closes = 1..10, period=3 -> alpha = 2/(3+1) = 0.5.
    # Hand trace (alpha=0.5, seeded by the first close):
    #   ema[0] = 1
    #   ema[1] = 0.5*2 + 0.5*1     = 1.5
    #   ema[2] = 0.5*3 + 0.5*1.5   = 2.25
    #   ema[3] = 0.5*4 + 0.5*2.25  = 3.125
    #   ema[4] = 0.5*5 + 0.5*3.125 = 4.0625
    closes = pd.Series(range(1, 11), dtype=float)
    result = ema(closes, period=3)

    expected = [1.0, 1.5, 2.25, 3.125, 4.0625, 5.03125, 6.015625, 7.0078125, 8.00390625, 9.001953125]
    assert result.tolist() == pytest.approx(expected)


def test_rsi_matches_independent_exact_fraction_trace():
    # closes chosen small enough to trace the gain/loss EWM(alpha=1/3, no SMA
    # seed) recursion by hand using exact fractions (see scratchpad derivation);
    # reproduced here as hardcoded expected values.
    closes = pd.Series([10, 12, 11, 13, 14, 13, 15, 16, 14, 17], dtype=float)
    result = rsi(closes, period=3)

    expected = [
        None,
        100.0,
        80.0,
        87.5,
        90.2439024390244,
        67.88990825688073,
        81.57894736842105,
        86.04187437686939,
        49.826789838337184,
        74.23085477055379,
    ]
    assert result.iloc[0] != result.iloc[0]  # NaN (no prior close to diff against)
    assert result.iloc[1:].tolist() == pytest.approx(expected[1:])


def test_atr_matches_independent_exact_fraction_trace():
    # True range computed independently per-bar, then EWM(alpha=1/3) traced
    # with exact fractions (see scratchpad derivation).
    highs = pd.Series([10, 11, 10.5, 12, 12.5, 13, 12.8, 13.5, 14, 14.2])
    lows = pd.Series([8, 9, 9.5, 10, 11, 11.5, 11.8, 12, 12.5, 13])
    closes = pd.Series([9, 10.5, 10, 11.5, 12, 12.5, 12, 13, 13.5, 14])

    result = atr(highs, lows, closes, period=3)

    expected = [
        2.0,
        2.0,
        1.6666666666666667,
        1.7777777777777777,
        1.6851851851851851,
        1.623456790123457,
        1.4156378600823045,
        1.443758573388203,
        1.4625057155921353,
        1.3750038103947568,
    ]
    assert result.tolist() == pytest.approx(expected)


def test_rsi_is_100_when_every_bar_is_a_gain_no_losses_at_all():
    # avg_loss stays exactly 0 throughout -- rs = avg_gain/0 = inf,
    # 100 - 100/(1+inf) must evaluate to exactly 100, not NaN/error, despite
    # the division by zero in the intermediate `rs` computation.
    closes = pd.Series([10, 11, 12, 13, 14, 15, 16], dtype=float)
    result = rsi(closes, period=3)

    assert result.iloc[1:].tolist() == pytest.approx([100.0] * (len(closes) - 1))


def test_rsi_is_0_when_every_bar_is_a_loss_no_gains_at_all():
    closes = pd.Series([16, 15, 14, 13, 12, 11, 10], dtype=float)
    result = rsi(closes, period=3)

    assert result.iloc[1:].tolist() == pytest.approx([0.0] * (len(closes) - 1))


def test_macd_histogram_matches_independent_from_scratch_loop():
    # 40-bar synthetic series; expected values from a standalone
    # plain-Python recursive-EMA loop (no pandas calls at all), not
    # indicators.py's own formula.
    closes = pd.Series([
        100.0, 102.28669330795061, 104.4941834230865, 106.54642473395036,
        108.37356090899523, 109.91470984807897, 111.12039085967226,
        111.9544972998846, 112.39573603041505, 112.43847630878196,
        112.09297426825682, 111.3849640381959, 110.3546318055115,
        109.05501371821465, 107.54988150155906, 105.91120008059868,
        104.2162585657242, 102.54458897973169, 100.97479556705149,
        99.58142109057282, 98.43197504692071, 97.58424227586411,
        97.08397926110483, 96.96308996366537, 97.2383539116416,
        97.91075725336862, 98.96545344279846, 100.37235512444012,
        102.08733362127678, 104.05397820586244, 106.20584501801073,
        108.46910597182503, 110.76549204850492, 113.01541363513378,
        115.14113351138609, 117.0698659871879, 118.73667863849153,
        120.08708095811626, 121.07919672031487, 121.68543345374606,
    ])

    result = macd_histogram(closes)

    expected = [
        0.0, 0.14593142478089477, 0.3702927030573824, 0.6212993171475618,
        0.8600667793394008, 1.0584350898760895, 1.1972620331030992,
        1.2650502396095349, 1.2568039675780585, 1.1730372899184034,
        1.0188767656935784, 0.8032194395976502, 0.5379216860888469,
        0.23700639452174244, -0.08411442018281168, -0.4093970660553379,
        -0.722893702308844, -1.0094363032573876, -1.2552613915296997,
        -1.4485593353657917, -1.5799327826031504, -1.6427507192618114,
        -1.6333875007116596, -1.5513398029787453, -1.3992185658679888,
        -1.1826174279794646, -0.9098636662593653, -0.5916620356337003,
        -0.2406459564479495, 0.12914596307093662, 0.5028472121655772,
        0.865371539684475, 1.2020278761704053, 1.499117655685757,
        1.744490891287733, 1.9280390052967111, 2.0421049332053487,
        2.081794310140308, 2.045175481706961, 1.9333605012291497,
    ]
    assert result.tolist() == pytest.approx(expected)


# --- rolling_average --------------------------------------------------------
#
# Shared by orchestrator/shadow_loop.py's live Risk Voice re-check and
# backtest/engine.py's replay of it. The no-look-ahead test below is the
# critical one: a look-ahead bug here (e.g. an off-by-one that reads past
# as_of_index) would silently bias every Risk Voice spread/ATR check in a
# backtest, since neither of those two call sites' own tests use varying
# data that would expose it.


def test_rolling_average_uses_full_window_when_enough_history_exists():
    # window = ROLLING_AVG_DAYS * BARS_PER_DAY_H1 = 20 * 24 = 480 bars.
    # 500 values 1..500 (as floats); at as_of_index=499 (the last one), the
    # window covers indices [20, 499] inclusive = values 21..500.
    window = ROLLING_AVG_DAYS * BARS_PER_DAY_H1
    series = pd.Series(range(1, 501), dtype=float)

    result = rolling_average(series, as_of_index=499)

    expected = sum(range(500 - window + 1, 501)) / window
    assert result == pytest.approx(expected)


def test_rolling_average_uses_only_available_bars_when_history_shorter_than_window():
    # as_of_index=4 with only 5 bars total (0..4) -- far short of the
    # 480-bar window -- must average exactly those 5, not pad/extrapolate.
    series = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])

    result = rolling_average(series, as_of_index=4)

    assert result == pytest.approx((10.0 + 20.0 + 30.0 + 40.0 + 50.0) / 5)


def test_rolling_average_at_index_zero_returns_just_that_one_value():
    series = pd.Series([42.0, 999.0, 999.0])

    result = rolling_average(series, as_of_index=0)

    assert result == pytest.approx(42.0)


def test_rolling_average_never_looks_ahead_of_as_of_index():
    # Two series identical up to and including as_of_index=4, but wildly
    # different afterward -- a genuine look-ahead bug (reading index 5+)
    # would make these two calls disagree; a correct implementation must
    # return the exact same value for both, since only indices 0..4 should
    # ever be read.
    past_and_current = [1.0, 2.0, 3.0, 4.0, 5.0]
    series_a = pd.Series(past_and_current + [1_000_000.0, 2_000_000.0])
    series_b = pd.Series(past_and_current + [-1_000_000.0, -2_000_000.0, -3_000_000.0])

    result_a = rolling_average(series_a, as_of_index=4)
    result_b = rolling_average(series_b, as_of_index=4)

    assert result_a == pytest.approx(result_b)
    assert result_a == pytest.approx(sum(past_and_current) / len(past_and_current))
