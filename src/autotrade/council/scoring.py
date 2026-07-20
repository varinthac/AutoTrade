"""Bull Voice / Bear Voice point-scoring, per
trading_system_summary_v2.md Appendix A §1.1 (Bull) / §1.2 (Bear):
"แต่ละ voice ให้คะแนน 0-100 จากสูตร ไม่ใช่ดุลยพินิจ" -- each voice scores
0-100 from a fixed formula, not judgment.

This module only computes scores; the Decision Matrix (which threshold turns
a score into a proposed trade) lives in `council/decision_matrix.py`.

No-lookahead note: exactly like `council/trivial_signal.py`, every series
used here is sliced to `df[...].iloc[:as_of_index + 1]` *before* any
indicator is computed, so a bar after `as_of_index` can never enter a
calculation, even indirectly through a recursive/EWM formula.

Boundary conventions (Appendix A leaves the exact edges undocumented, so they
are pinned down here):

- RSI momentum band is inclusive at both ends: Bull fires on `50 <= RSI(14)
  <= 70`, Bear fires on `30 <= RSI(14) <= 50`. The two bands intentionally
  overlap at exactly RSI == 50 (both Bull and Bear can award their +20 there)
  -- this mirrors the source table's own overlapping "50" boundary and is not
  a bug.
- MACD histogram "positive and expanding" (Bull) / "negative and contracting"
  (Bear) both require *strict* inequality against the prior bar
  (`macd_histogram[i] > macd_histogram[i-1]` for Bull, `<` for Bear) -- a flat
  histogram does not count as expanding/contracting. If there is no prior bar
  to compare against (`as_of_index == 0`), this component scores 0.
- Confluence (`features.levels.is_near_key_level`) needs a completed prior
  server-time day to compute the daily pivot (`features.levels.
  prior_day_ohlc`). Early in a series, before any prior day has completed,
  `prior_day_ohlc` raises `ValueError` -- this is caught here and the
  confluence check falls back to the round-number level alone, rather than
  failing the whole score.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.features.indicators import atr, ema, macd_histogram, rsi
from autotrade.features.levels import daily_pivot, is_near_key_level, nearest_round_number, prior_day_ohlc
from autotrade.features.swing import is_higher_low, is_lower_high

EMA_FAST_PERIOD = 20
EMA_MID_PERIOD = 50
EMA_SLOW_PERIOD = 200
RSI_PERIOD = 14


@dataclass(frozen=True)
class BullBearScore:
    """A single voice's score (0-100) plus its individual component
    contributions, so the rationale is fully inspectable/loggable rather
    than a bare number (spec.md §3.3: personas log direction, score, the
    specific features they keyed on, and rationale, in full)."""

    score: int
    trend_alignment: int
    momentum_rsi: int
    momentum_macd: int
    market_structure: int
    confluence: int


def _confluence_score(df: pd.DataFrame, as_of_index: int, symbol_spec: SymbolSpec, points: int) -> int:
    closes = df["close"].iloc[: as_of_index + 1]
    highs = df["high"].iloc[: as_of_index + 1]
    lows = df["low"].iloc[: as_of_index + 1]
    price = float(closes.iloc[-1])
    atr_value = float(atr(highs, lows, closes).iloc[-1])

    levels = [nearest_round_number(price, symbol_spec.digits)]
    try:
        prior_high, prior_low, prior_close = prior_day_ohlc(df, as_of_index)
        levels.append(daily_pivot(prior_high, prior_low, prior_close))
    except ValueError:
        pass

    return points if is_near_key_level(price, atr_value, levels, multiple=0.5) else 0


def score_bull_voice(
    df: pd.DataFrame,
    as_of_index: int,
    symbol_spec: SymbolSpec,
    pivot_bars: int = 3,
) -> BullBearScore:
    """Bull Voice score (Appendix A §1.1), closed bars only, no lookahead."""
    if as_of_index < 0 or as_of_index >= len(df):
        raise ValueError(f"as_of_index {as_of_index} is out of bounds for df of length {len(df)}")

    closes = df["close"].iloc[: as_of_index + 1]

    ema20 = ema(closes, EMA_FAST_PERIOD).iloc[-1]
    ema50 = ema(closes, EMA_MID_PERIOD).iloc[-1]
    ema200 = ema(closes, EMA_SLOW_PERIOD).iloc[-1]
    if ema20 > ema50 > ema200:
        trend_alignment = 30
    elif ema20 > ema50:
        trend_alignment = 15
    else:
        trend_alignment = 0

    rsi_value = rsi(closes, RSI_PERIOD).iloc[-1]
    momentum_rsi = 20 if 50 <= rsi_value <= 70 else 0

    momentum_macd = 0
    if as_of_index >= 1:
        hist = macd_histogram(closes)
        curr_hist, prev_hist = hist.iloc[-1], hist.iloc[-2]
        if curr_hist > 0 and curr_hist > prev_hist:
            momentum_macd = 15

    market_structure = 20 if is_higher_low(df, as_of_index, pivot_bars=pivot_bars) else 0

    confluence = _confluence_score(df, as_of_index, symbol_spec, points=15)

    score = trend_alignment + momentum_rsi + momentum_macd + market_structure + confluence
    return BullBearScore(
        score=score,
        trend_alignment=trend_alignment,
        momentum_rsi=momentum_rsi,
        momentum_macd=momentum_macd,
        market_structure=market_structure,
        confluence=confluence,
    )


def score_bear_voice(
    df: pd.DataFrame,
    as_of_index: int,
    symbol_spec: SymbolSpec,
    pivot_bars: int = 3,
) -> BullBearScore:
    """Bear Voice score (Appendix A §1.2), exactly symmetric to
    `score_bull_voice` -- see that function's docstring and this module's
    docstring for the shared boundary conventions."""
    if as_of_index < 0 or as_of_index >= len(df):
        raise ValueError(f"as_of_index {as_of_index} is out of bounds for df of length {len(df)}")

    closes = df["close"].iloc[: as_of_index + 1]

    ema20 = ema(closes, EMA_FAST_PERIOD).iloc[-1]
    ema50 = ema(closes, EMA_MID_PERIOD).iloc[-1]
    ema200 = ema(closes, EMA_SLOW_PERIOD).iloc[-1]
    if ema20 < ema50 < ema200:
        trend_alignment = 30
    elif ema20 < ema50:
        trend_alignment = 15
    else:
        trend_alignment = 0

    rsi_value = rsi(closes, RSI_PERIOD).iloc[-1]
    momentum_rsi = 20 if 30 <= rsi_value <= 50 else 0

    momentum_macd = 0
    if as_of_index >= 1:
        hist = macd_histogram(closes)
        curr_hist, prev_hist = hist.iloc[-1], hist.iloc[-2]
        if curr_hist < 0 and curr_hist < prev_hist:
            momentum_macd = 15

    market_structure = 20 if is_lower_high(df, as_of_index, pivot_bars=pivot_bars) else 0

    confluence = _confluence_score(df, as_of_index, symbol_spec, points=15)

    score = trend_alignment + momentum_rsi + momentum_macd + market_structure + confluence
    return BullBearScore(
        score=score,
        trend_alignment=trend_alignment,
        momentum_rsi=momentum_rsi,
        momentum_macd=momentum_macd,
        market_structure=market_structure,
        confluence=confluence,
    )
