"""Tests for council/scoring.py -- Bull/Bear Voice point-scoring, Appendix A
§1.1/§1.2.

Each component is exercised in isolation first (asserting only that specific
`BullBearScore` field, so other components' incidental values on the same
fixture don't matter), cross-checked against `features/`'s own
already-tested functions per the module's own boundary-convention docstring,
plus a `score == sum(components)` invariant check and out-of-bounds guards.
"""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.scoring import score_bear_voice, score_bull_voice
from autotrade.features.indicators import ema, rsi

SYMBOL_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def _df(highs, lows, closes, start="2026-01-01 00:00") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="h")
    return pd.DataFrame({"time": times, "high": highs, "low": lows, "close": closes})


def _flat_df(close: float, half_range: float, n: int, start="2026-07-19 00:00") -> pd.DataFrame:
    closes = [close] * n
    highs = [close + half_range] * n
    lows = [close - half_range] * n
    return _df(highs, lows, closes, start=start)


# --- Trend alignment ---------------------------------------------------

# Long, strictly monotonic ramp -- ema(20) > ema(50) > ema(200) is verified
# directly against features.indicators.ema below rather than hand-derived.
_TREND30_UP_CLOSES = [100.0 + i for i in range(300)]
_TREND30_DOWN_CLOSES = [100.0 - i for i in range(300)]

# Anchored high for 250 bars (so ema200 stays elevated), then a drop and a
# partial recovery -- ema20 pulls above ema50 first (recent recovery) while
# ema50 is still below the still-elevated ema200 (elif-only, not the full
# stack). Values cross-checked against ema() directly below.
_TREND15_UP_CLOSES = [200.0] * 250 + [50.0] * 80 + [80.0] * 20
_TREND15_DOWN_CLOSES = [50.0] * 250 + [200.0] * 80 + [170.0] * 20


def _last_ema_ordering(closes: list[float]):
    s = pd.Series(closes)
    return ema(s, 20).iloc[-1], ema(s, 50).iloc[-1], ema(s, 200).iloc[-1]


def test_bull_trend_alignment_scores_30_when_ema20_gt_50_gt_200():
    e20, e50, e200 = _last_ema_ordering(_TREND30_UP_CLOSES)
    assert e20 > e50 > e200  # precondition, cross-checked against ema() directly

    df = _df([c + 1 for c in _TREND30_UP_CLOSES], [c - 1 for c in _TREND30_UP_CLOSES], _TREND30_UP_CLOSES)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 30


def test_bull_trend_alignment_scores_15_when_only_ema20_gt_ema50():
    e20, e50, e200 = _last_ema_ordering(_TREND15_UP_CLOSES)
    assert e20 > e50 and not (e50 > e200)  # precondition

    closes = _TREND15_UP_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 15


def test_bull_trend_alignment_scores_0_when_ema20_not_above_ema50():
    closes = _TREND30_DOWN_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 0


def test_bear_trend_alignment_scores_30_when_ema20_lt_50_lt_200():
    e20, e50, e200 = _last_ema_ordering(_TREND30_DOWN_CLOSES)
    assert e20 < e50 < e200  # precondition

    closes = _TREND30_DOWN_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 30


def test_bear_trend_alignment_scores_15_when_only_ema20_lt_ema50():
    e20, e50, e200 = _last_ema_ordering(_TREND15_DOWN_CLOSES)
    assert e20 < e50 and not (e50 < e200)  # precondition

    closes = _TREND15_DOWN_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 15


def test_bear_trend_alignment_scores_0_when_ema20_not_below_ema50():
    closes = _TREND30_UP_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.trend_alignment == 0


# --- Momentum: RSI -------------------------------------------------------

def _alternating_closes(up_step: float, down_step: float, n: int) -> list[float]:
    vals = [100.0]
    for i in range(n):
        vals.append(vals[-1] + (up_step if i % 2 == 0 else -down_step))
    return vals


# Net-upward-biased alternation -> RSI(14) oscillates ~65-68, inside [50,70].
_RSI_BULL_BAND_CLOSES = _alternating_closes(1.0, 0.5, 200)
# Net-downward-biased alternation -> RSI(14) oscillates ~32-35, inside [30,50].
_RSI_BEAR_BAND_CLOSES = _alternating_closes(0.5, 1.0, 200)  # net down (loses more than it gains)


def _last_rsi(closes: list[float]) -> float:
    return float(rsi(pd.Series(closes), 14).iloc[-1])


def test_bull_momentum_rsi_scores_20_within_50_70_band():
    closes = _RSI_BULL_BAND_CLOSES
    rsi_value = _last_rsi(closes)
    assert 50 <= rsi_value <= 70  # precondition, cross-checked directly against features.indicators.rsi

    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_rsi == 20


def test_bear_momentum_rsi_scores_0_on_the_same_bull_band_series():
    closes = _RSI_BULL_BAND_CLOSES
    rsi_value = _last_rsi(closes)
    assert not (30 <= rsi_value <= 50)  # precondition: outside the bear band

    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_rsi == 0


def test_bear_momentum_rsi_scores_20_within_30_50_band():
    closes = _RSI_BEAR_BAND_CLOSES
    rsi_value = _last_rsi(closes)
    assert 30 <= rsi_value <= 50  # precondition, cross-checked directly against features.indicators.rsi

    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_rsi == 20


def test_bull_momentum_rsi_scores_0_when_overbought():
    closes = [100.0 + i for i in range(50)]  # strong uptrend -> RSI == 100
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_rsi == 0


def test_bear_momentum_rsi_scores_0_when_oversold():
    closes = [100.0 - i for i in range(50)]  # strong downtrend -> RSI == 0
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_rsi == 0


# --- Momentum: MACD histogram --------------------------------------------

def _accelerating_closes(sign: float, n: int) -> list[float]:
    vals = [100.0]
    for i in range(n):
        vals.append(vals[-1] + sign * (1.0 + i * 0.1))
    return vals


_MACD_BULL_EXPANDING_CLOSES = _accelerating_closes(1.0, 80)
_MACD_BEAR_CONTRACTING_CLOSES = _accelerating_closes(-1.0, 80)


def test_bull_momentum_macd_scores_15_when_positive_and_expanding():
    closes = _MACD_BULL_EXPANDING_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_macd == 15


def test_bear_momentum_macd_scores_15_when_negative_and_contracting():
    closes = _MACD_BEAR_CONTRACTING_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert score.momentum_macd == 15


def test_momentum_macd_scores_0_on_a_flat_series_for_both_voices():
    df = _flat_df(100.0, 1.0, 30)

    bull = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    bear = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert bull.momentum_macd == 0
    assert bear.momentum_macd == 0


def test_momentum_macd_scores_0_at_as_of_index_zero_no_prior_bar():
    closes = _MACD_BULL_EXPANDING_CLOSES
    df = _df([c + 1 for c in closes], [c - 1 for c in closes], closes)

    score = score_bull_voice(df, 0, SYMBOL_SPEC)
    assert score.momentum_macd == 0


# --- Market structure ------------------------------------------------------

# Two confirmed swing lows: index 5 (5.0) then index 14 (8.0, a higher low),
# close[17] = 20 > 8 -- reused from features/swing.py's own test convention
# (tests/unit/features/test_swing.py's `_HL_*` fixture), so `is_higher_low`
# is already known (by that module's own tests) to return True here.
_HL_LOWS = [20, 18, 16, 14, 12, 5, 12, 14, 16, 18, 20, 18, 16, 14, 8, 14, 16, 18, 20, 22]
_HL_HIGHS = [v + 10 for v in _HL_LOWS]
_HL_CLOSES = [v + 2 for v in _HL_LOWS]

_LH_HIGHS = [30 - v for v in _HL_LOWS]
_LH_LOWS = [v - 10 for v in _LH_HIGHS]
_LH_CLOSES = [v - 2 for v in _LH_HIGHS]


def test_bull_market_structure_scores_20_on_confirmed_higher_low():
    df = _df(_HL_HIGHS, _HL_LOWS, _HL_CLOSES)

    score = score_bull_voice(df, 17, SYMBOL_SPEC)
    assert score.market_structure == 20


def test_bull_market_structure_scores_0_without_a_second_confirmed_swing():
    df = _df(_HL_HIGHS, _HL_LOWS, _HL_CLOSES)

    score = score_bull_voice(df, 9, SYMBOL_SPEC)
    assert score.market_structure == 0


def test_bear_market_structure_scores_20_on_confirmed_lower_high():
    df = _df(_LH_HIGHS, _LH_LOWS, _LH_CLOSES)

    score = score_bear_voice(df, 17, SYMBOL_SPEC)
    assert score.market_structure == 20


def test_bear_market_structure_scores_0_without_a_second_confirmed_swing():
    df = _df(_LH_HIGHS, _LH_LOWS, _LH_CLOSES)

    score = score_bear_voice(df, 9, SYMBOL_SPEC)
    assert score.market_structure == 0


# --- Confluence ------------------------------------------------------------

def test_confluence_scores_15_when_near_a_round_number():
    # Flat close=100.2, half_range=1.2 -> atr==2.4, threshold==1.2. Nearest
    # round number (digits=2 -> 0.5 granularity) is 100.0, distance 0.2.
    df = _flat_df(100.2, 1.2, 20)

    bull = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    bear = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert bull.confluence == 15
    assert bear.confluence == 15


def test_confluence_scores_0_when_far_from_any_level():
    # Flat close=100.25 (exactly halfway between round numbers), tight
    # half_range=0.05 -> atr==0.1, threshold==0.05 < the 0.25 distance to
    # the nearest round number either way.
    df = _flat_df(100.25, 0.05, 20)

    bull = score_bull_voice(df, len(df) - 1, SYMBOL_SPEC)
    bear = score_bear_voice(df, len(df) - 1, SYMBOL_SPEC)
    assert bull.confluence == 0
    assert bear.confluence == 0


def _two_day_pivot_confluence_df() -> pd.DataFrame:
    # Day 1: high=110, low=95, close=108 -> daily_pivot == 104.333...
    # Day 2: flat close=104.35, tight half_range=0.02 -> atr small enough
    # that the nearest round number (104.5, distance 0.15) is OUT of range
    # but the daily pivot (distance ~0.0167) is IN range -- isolates the
    # daily-pivot leg of the confluence check specifically.
    day1_times = pd.date_range("2026-07-18 00:00", periods=24, freq="h")
    day1_high = [100.0] * 24
    day1_high[9] = 110.0
    day1_low = [100.0] * 24
    day1_low[3] = 95.0
    day1_close = [100.0] * 24
    day1_close[23] = 108.0

    day2_times = pd.date_range("2026-07-19 00:00", periods=20, freq="h")
    day2_close = [104.35] * 20
    day2_high = [c + 0.02 for c in day2_close]
    day2_low = [c - 0.02 for c in day2_close]

    return pd.DataFrame(
        {
            "time": list(day1_times) + list(day2_times),
            "high": day1_high + day2_high,
            "low": day1_low + day2_low,
            "close": day1_close + day2_close,
        }
    )


def test_confluence_scores_15_when_near_the_daily_pivot_specifically():
    df = _two_day_pivot_confluence_df()
    as_of_index = len(df) - 1

    bull = score_bull_voice(df, as_of_index, SYMBOL_SPEC)
    assert bull.confluence == 15


# --- Combined / invariants --------------------------------------------------

def test_score_equals_sum_of_its_components():
    df = _df(_HL_HIGHS, _HL_LOWS, _HL_CLOSES)

    bull = score_bull_voice(df, 17, SYMBOL_SPEC)
    bear = score_bear_voice(df, 17, SYMBOL_SPEC)

    assert bull.score == (
        bull.trend_alignment + bull.momentum_rsi + bull.momentum_macd
        + bull.market_structure + bull.confluence
    )
    assert bear.score == (
        bear.trend_alignment + bear.momentum_rsi + bear.momentum_macd
        + bear.market_structure + bear.confluence
    )


# --- Bounds ------------------------------------------------------------

def test_score_bull_voice_raises_on_out_of_bounds_as_of_index():
    df = _flat_df(100.0, 1.0, 10)

    with pytest.raises(ValueError):
        score_bull_voice(df, len(df), SYMBOL_SPEC)

    with pytest.raises(ValueError):
        score_bull_voice(df, -1, SYMBOL_SPEC)


def test_score_bear_voice_raises_on_out_of_bounds_as_of_index():
    df = _flat_df(100.0, 1.0, 10)

    with pytest.raises(ValueError):
        score_bear_voice(df, len(df), SYMBOL_SPEC)

    with pytest.raises(ValueError):
        score_bear_voice(df, -1, SYMBOL_SPEC)
