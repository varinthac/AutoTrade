"""Phase-3 trivial trade signal -- a deliberately simple EMA20/EMA50 crossover,
used only to prove the pipeline (feed -> features -> council -> risk ->
execution) works mechanically end-to-end on the demo account (spec.md §6,
Phase 3). This is NOT the full Bull/Bear scoring Council of
trading_system_summary_v2.md Appendix A §1.1/§1.2 -- that is Phase 6's job and
will replace this module wholesale.

No-lookahead note: `generate_trivial_signal` slices `df["close"]` down to
`df["close"].iloc[:as_of_index + 1]` *before* computing EMA, rather than
computing EMA over the full `df` and indexing into it afterwards. EMA is a
causal/recursive formula, so both approaches give the same numeric value at
`as_of_index` -- but slicing first makes the no-lookahead guarantee structural
rather than incidental: it is impossible for a bar after `as_of_index` to
enter the calculation at all, even if a future edit changes how EMA is
computed.
"""
from __future__ import annotations

from typing import Literal

import pandas as pd

from autotrade.features.indicators import atr, ema
from autotrade.features.swing import latest_confirmed_swing_high, latest_confirmed_swing_low
from autotrade.council.order_construction import OrderPlan, build_order_plan

EMA_FAST_PERIOD = 20
EMA_SLOW_PERIOD = 50


def generate_trivial_signal(df: pd.DataFrame, as_of_index: int) -> Literal["BUY", "SELL"] | None:
    """EMA20/EMA50 crossover exactly on the bar at `as_of_index`, using
    closed bars only (`df["close"].iloc[:as_of_index + 1]`).

    BUY:  EMA20 <= EMA50 at as_of_index - 1, EMA20 > EMA50 at as_of_index.
    SELL: EMA20 >= EMA50 at as_of_index - 1, EMA20 < EMA50 at as_of_index.
    Anything else (no cross, or no prior bar to compare against): None.
    """
    if as_of_index <= 0 or as_of_index >= len(df):
        return None

    closes = df["close"].iloc[: as_of_index + 1]
    ema_fast = ema(closes, EMA_FAST_PERIOD)
    ema_slow = ema(closes, EMA_SLOW_PERIOD)

    prev_fast, prev_slow = ema_fast.iloc[-2], ema_slow.iloc[-2]
    curr_fast, curr_slow = ema_fast.iloc[-1], ema_slow.iloc[-1]

    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return "BUY"
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return "SELL"
    return None


def build_trade_idea(
    df: pd.DataFrame,
    as_of_index: int,
    sl_buffer_atr: float = 0.2,
    sl_min_atr: float = 0.8,
    sl_max_atr: float = 2.5,
    tp_r_multiple: float = 2.0,
    pivot_bars: int = 3,
) -> OrderPlan | None:
    """Compose `generate_trivial_signal` + `order_construction.build_order_plan`
    end-to-end. Returns `None` at any stage that can't produce a valid order
    (no signal fires, or no confirmed swing is available yet)."""
    direction = generate_trivial_signal(df, as_of_index)
    if direction is None:
        return None

    closes = df["close"].iloc[: as_of_index + 1]
    highs = df["high"].iloc[: as_of_index + 1]
    lows = df["low"].iloc[: as_of_index + 1]
    current_atr = atr(highs, lows, closes).iloc[-1]

    if direction == "BUY":
        swing = latest_confirmed_swing_low(df, as_of_index, pivot_bars=pivot_bars)
    else:
        swing = latest_confirmed_swing_high(df, as_of_index, pivot_bars=pivot_bars)

    if swing is None:
        return None
    swing_price = swing[1]

    entry_price = df["close"].iloc[as_of_index]

    return build_order_plan(
        direction=direction,
        entry_price=entry_price,
        swing_price=swing_price,
        atr=current_atr,
        sl_buffer_atr=sl_buffer_atr,
        sl_min_atr=sl_min_atr,
        sl_max_atr=sl_max_atr,
        tp_r_multiple=tp_r_multiple,
    )
