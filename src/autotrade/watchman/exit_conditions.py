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
) -> bool:
    """True if the market structure that justified this entry has broken,
    per Appendix A §4.4:

        BUY:  H1 close at `as_of_index` < the low of the swing-low bar
              referenced at entry (`entry_swing_index`).
        SELL: H1 close at `as_of_index` > the high of the swing-high bar
              referenced at entry.

    `as_of_index` must be the latest CLOSED bar -- same no-lookahead
    discipline as `features/swing.py` (structure invalidation only evaluates
    on closed-bar confirmed swings per spec.md §3.4's "Reacts tick-by-tick
    for hard protection; structure-trail only on closed-bar confirmed
    swings").

    The swing referenced at entry was, by construction, confirmed strictly
    before the entry bar (see `features/swing.py`), so
    `entry_swing_index <= as_of_index` should always hold for any bar at or
    after entry -- enforced defensively here rather than assumed silently.

    CONTRACT ON `df` (read this before wiring in Phase 7b): `entry_swing_index`
    and `as_of_index` are POSITIONAL (`.iloc`) indices, not labels. `df` at
    evaluation time MUST be the exact same fixed-origin, contiguous
    `RangeIndex` frame (per `features/swing.py`'s own hard requirement) --
    or a frame that has only grown by appending new rows at the end (the
    `_append_bar` pattern in `orchestrator/shadow_loop.py`) -- that was in
    use when `entry_swing_index` was originally recorded
    (`watchman/position_metadata.py`'s `PositionMetadata.entry_swing_index`).
    Passing a re-sliced/rolling window here (front bars dropped, indices
    renumbered) will silently make `iloc[entry_swing_index]` point at the
    wrong bar -- a wrong structure-invalidation decision with real money at
    stake, and NO error will be raised to catch it. Never trim rows from the
    front of `df` between recording `entry_swing_index` and evaluating it.
    """
    if entry_swing_index > as_of_index:
        raise ValueError(
            f"entry_swing_index ({entry_swing_index}) must be <= as_of_index "
            f"({as_of_index}) -- the entry swing was confirmed before entry, "
            "so it cannot be later than the bar being evaluated"
        )

    current_close = df["close"].iloc[as_of_index]

    if direction == "BUY":
        swing_low = df["low"].iloc[entry_swing_index]
        return bool(current_close < swing_low)
    elif direction == "SELL":
        swing_high = df["high"].iloc[entry_swing_index]
        return bool(current_close > swing_high)
    else:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {direction!r}")


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
