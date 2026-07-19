"""Tests for council/trivial_signal.py -- the Phase 3 EMA20/EMA50 crossover
placeholder signal (spec.md §6 Phase 3), not the full Bull/Bear Council
(Phase 6).

Fixtures are engineered so the crossover location is obvious by construction:
closes are held flat at 100 for bars 0-69 (both EMAs converge to 100, diff =
0) then jump to a new flat level for bars 70-109. The EMA20/EMA50 diff can
only cross zero on the very first post-jump bar (index 70) -- verified
independently in scratchpad before being hardcoded here -- so bar 69 (one
before) and bar 71 (one continuing-trend bar after, no new cross) are clean
off-by-one checks.
"""
from __future__ import annotations

import pandas as pd

from autotrade.council.order_construction import build_order_plan
from autotrade.council.trivial_signal import build_trade_idea, generate_trivial_signal
from autotrade.features.indicators import atr as atr_indicator

CROSS_INDEX = 70


def _flat_then_jump_df(pre_level: float, post_level: float, swing_dip_index: int | None = None,
                        swing_is_high: bool = False) -> pd.DataFrame:
    closes = [pre_level] * CROSS_INDEX + [post_level] * 40
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    if swing_dip_index is not None:
        if swing_is_high:
            highs[swing_dip_index] = pre_level + 20
        else:
            lows[swing_dip_index] = pre_level - 20
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes})


def test_generate_trivial_signal_fires_buy_exactly_on_the_crossover_bar():
    df = _flat_then_jump_df(100.0, 180.0)

    assert generate_trivial_signal(df, CROSS_INDEX - 1) is None
    assert generate_trivial_signal(df, CROSS_INDEX) == "BUY"
    assert generate_trivial_signal(df, CROSS_INDEX + 1) is None


def test_generate_trivial_signal_fires_sell_exactly_on_the_crossover_bar():
    df = _flat_then_jump_df(100.0, 20.0)

    assert generate_trivial_signal(df, CROSS_INDEX - 1) is None
    assert generate_trivial_signal(df, CROSS_INDEX) == "SELL"
    assert generate_trivial_signal(df, CROSS_INDEX + 1) is None


def test_generate_trivial_signal_none_when_as_of_index_is_zero_or_out_of_bounds():
    df = _flat_then_jump_df(100.0, 180.0)

    assert generate_trivial_signal(df, 0) is None
    assert generate_trivial_signal(df, len(df)) is None


def test_build_trade_idea_returns_none_when_no_confirmed_swing_yet():
    # Flat, dip-free lows/highs -- the crossover fires BUY at index 70, but
    # no fractal swing low exists anywhere in the data, so no stop-loss
    # anchor is available yet.
    df = _flat_then_jump_df(100.0, 180.0)

    assert generate_trivial_signal(df, CROSS_INDEX) == "BUY"
    assert build_trade_idea(df, CROSS_INDEX) is None


def test_build_trade_idea_composes_signal_swing_and_order_construction_for_buy():
    df = _flat_then_jump_df(100.0, 180.0, swing_dip_index=30, swing_is_high=False)

    plan = build_trade_idea(
        df, CROSS_INDEX, sl_buffer_atr=0.2, sl_min_atr=0.8, sl_max_atr=2.5, tp_r_multiple=2.0, pivot_bars=3
    )

    expected_atr = atr_indicator(
        df["high"].iloc[: CROSS_INDEX + 1],
        df["low"].iloc[: CROSS_INDEX + 1],
        df["close"].iloc[: CROSS_INDEX + 1],
    ).iloc[-1]
    expected = build_order_plan(
        "BUY",
        entry_price=df["close"].iloc[CROSS_INDEX],
        swing_price=80.0,  # pre_level (100) - 20 dip at index 30
        atr=expected_atr,
        sl_buffer_atr=0.2, sl_min_atr=0.8, sl_max_atr=2.5, tp_r_multiple=2.0,
    )

    assert plan == expected


def test_build_trade_idea_composes_signal_swing_and_order_construction_for_sell():
    df = _flat_then_jump_df(100.0, 20.0, swing_dip_index=30, swing_is_high=True)

    plan = build_trade_idea(
        df, CROSS_INDEX, sl_buffer_atr=0.2, sl_min_atr=0.8, sl_max_atr=2.5, tp_r_multiple=2.0, pivot_bars=3
    )

    expected_atr = atr_indicator(
        df["high"].iloc[: CROSS_INDEX + 1],
        df["low"].iloc[: CROSS_INDEX + 1],
        df["close"].iloc[: CROSS_INDEX + 1],
    ).iloc[-1]
    expected = build_order_plan(
        "SELL",
        entry_price=df["close"].iloc[CROSS_INDEX],
        swing_price=120.0,  # pre_level (100) + 20 dip at index 30
        atr=expected_atr,
        sl_buffer_atr=0.2, sl_min_atr=0.8, sl_max_atr=2.5, tp_r_multiple=2.0,
    )

    assert plan == expected
