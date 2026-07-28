"""Watchman exit conditions -- structure invalidation + time stop, per
trading_system_summary_v2.md Appendix A §4 items 4 and 6.

Both functions are pure and take every input explicitly (no I/O, no Clock) --
same convention as `council/order_construction.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import pandas as pd


def check_structure_invalidation(
    direction: Literal["BUY", "SELL"],
    entry_swing_index: int,
    df: pd.DataFrame,
    as_of_index: int,
    entry_swing_level: float | None = None,
) -> bool:
    """True if the market structure that justified this entry has broken,
    per Appendix A §4.4:

        BUY:  H1 close at `as_of_index` < the low of the swing-low bar
              referenced at entry.
        SELL: H1 close at `as_of_index` > the high of the swing-high bar
              referenced at entry.

    `as_of_index` must be the latest CLOSED bar -- same no-lookahead
    discipline as `features/swing.py` (structure invalidation only evaluates
    on closed-bar confirmed swings per spec.md §3.4's "Reacts tick-by-tick
    for hard protection; structure-trail only on closed-bar confirmed
    swings").

    **`entry_swing_level` (2026-07-29 frame-shift bugfix) is the preferred
    input**: the entry swing's actual price level (swing-low bar's low for
    BUY, swing-high bar's high for SELL), captured at ENTRY time and carried
    on `PositionMetadata` -- immune to any process restart, history reseed,
    or rolling-window trim, because it is exactly the number this check
    needs, with no positional lookup at all. When given, `entry_swing_index`
    is ignored entirely.

    `entry_swing_index` remains ONLY as the legacy fallback for metadata
    recorded before `entry_swing_level` existed, with its original contract:
    it is a POSITIONAL (`.iloc`) index, and `df` MUST be the exact same
    fixed-origin, contiguous `RangeIndex` frame (or one only grown by
    appending rows at the end) that was in use when the index was recorded.
    That contract is UNVERIFIABLE across a process restart -- observed live
    (2026-07-29 counterfactual audit): a reseeded frame silently re-pointed
    a recorded index at a different bar, producing a FALSE structure
    invalidation that closed trade #4 early -- which is precisely why the
    level-based path above replaced it for all new positions.
    """
    current_close = df["close"].iloc[as_of_index]

    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {direction!r}")

    if entry_swing_level is not None:
        if direction == "BUY":
            return bool(current_close < entry_swing_level)
        return bool(current_close > entry_swing_level)

    if entry_swing_index > as_of_index:
        raise ValueError(
            f"entry_swing_index ({entry_swing_index}) must be <= as_of_index "
            f"({as_of_index}) -- the entry swing was confirmed before entry, "
            "so it cannot be later than the bar being evaluated"
        )

    if direction == "BUY":
        swing_low = df["low"].iloc[entry_swing_index]
        return bool(current_close < swing_low)
    swing_high = df["high"].iloc[entry_swing_index]
    return bool(current_close > swing_high)


def check_time_stop(
    opened_at: datetime,
    now: datetime,
    entry_price: float,
    current_price: float,
    initial_stop_distance: float,
    direction: Literal["BUY", "SELL"],
    time_stop_hours: float,
    dead_trade_r_band: float = 0.3,
) -> bool:
    """True if this position is a "dead trade" per Appendix A §4.6: open at
    least `time_stop_hours` AND its current R-multiple is within
    `[-dead_trade_r_band, +dead_trade_r_band]` (going nowhere)."""
    if initial_stop_distance <= 0:
        raise ValueError(f"initial_stop_distance must be positive, got {initial_stop_distance}")

    if now - opened_at < timedelta(hours=time_stop_hours):
        return False

    if direction == "BUY":
        profit_r = (current_price - entry_price) / initial_stop_distance
    elif direction == "SELL":
        profit_r = (entry_price - current_price) / initial_stop_distance
    else:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {direction!r}")

    return -dead_trade_r_band <= profit_r <= dead_trade_r_band
