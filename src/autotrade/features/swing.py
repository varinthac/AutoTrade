"""Swing-point (fractal pivot) detection per
trading_system_summary_v2.md Appendix A §0:

    swing high = a bar whose high is higher than the high of the 3 bars
    before AND the 3 bars after (symmetric "fractal 3-3", `pivot_bars=3`)
    swing low is the symmetric opposite on lows.

    "swing ยืนยันได้หลังปิดแท่งที่ 3 ฝั่งขวา — ห้าม lookahead ใช้ swing ที่ยังไม่ยืนยัน"
    -> a swing candidate at index i is only *confirmed* once bar i+pivot_bars
    has closed. Code must never treat an unconfirmed swing as known.

`detect_swings()` is for offline/backtest use where the whole series is
already known (it may reference bars "after" a pivot because, offline, those
bars are historical fact, not a live lookahead). `latest_confirmed_swing_low()`
/ `latest_confirmed_swing_high()` are what live/decision code must call
instead: they take an explicit `as_of_index` ("we are standing at this closed
bar; what's the latest swing we're allowed to know about?") and only ever
search candidates whose confirmation bar (`i + pivot_bars`) is <= as_of_index,
so it's structurally impossible to return an unconfirmed swing.
"""
from __future__ import annotations

import pandas as pd


def detect_swings(df: pd.DataFrame, pivot_bars: int = 3) -> pd.DataFrame:
    """Every fractal swing high/low in the full `df` (offline/backtest use
    only -- see module docstring). Returns a DataFrame aligned to `df.index`
    with boolean columns 'swing_high' and 'swing_low'."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)

    swing_high = pd.Series(False, index=df.index)
    swing_low = pd.Series(False, index=df.index)

    for i in range(pivot_bars, n - pivot_bars):
        left_high = highs[i - pivot_bars : i]
        right_high = highs[i + 1 : i + pivot_bars + 1]
        if highs[i] > left_high.max() and highs[i] > right_high.max():
            swing_high.iloc[i] = True

        left_low = lows[i - pivot_bars : i]
        right_low = lows[i + 1 : i + pivot_bars + 1]
        if lows[i] < left_low.min() and lows[i] < right_low.min():
            swing_low.iloc[i] = True

    return pd.DataFrame({"swing_high": swing_high, "swing_low": swing_low})


def _confirmed_swing_indices(
    df: pd.DataFrame, as_of_index: int, pivot_bars: int, column: str
) -> list[int]:
    """Indices of every swing (of `column`'s kind) confirmed as of
    `as_of_index`, oldest first. A candidate at index i is confirmed only
    once bar i+pivot_bars has closed, i.e. i <= as_of_index - pivot_bars --
    enforced twice here (once implicitly, because slicing the frame to
    df.iloc[:as_of_index + 1] denies detect_swings the right-side bars it
    needs to confirm any later candidate; once explicitly via the index
    filter below) so the boundary can't be got wrong by future edits to
    either function in isolation."""
    if as_of_index < 0 or as_of_index >= len(df):
        raise ValueError(f"as_of_index {as_of_index} is out of bounds for df of length {len(df)}")

    if not (df.index == pd.RangeIndex(len(df))).all():
        raise ValueError(
            "df must have a contiguous 0..n-1 RangeIndex -- as_of_index is a "
            "positional contract, and a non-contiguous index would silently "
            "produce wrong swings"
        )

    last_allowed = as_of_index - pivot_bars
    if last_allowed < pivot_bars:
        return []

    sub = df.iloc[: as_of_index + 1]
    swings = detect_swings(sub, pivot_bars=pivot_bars)
    candidate_idx = swings.index[swings[column]]
    confirmed_idx = [i for i in candidate_idx if i <= last_allowed]
    return confirmed_idx


def latest_confirmed_swing_low(
    df: pd.DataFrame, as_of_index: int, pivot_bars: int = 3
) -> tuple[int, float] | None:
    """(index, low) of the most recent swing low confirmed as of
    `as_of_index`, or None if there isn't one yet."""
    indices = _confirmed_swing_indices(df, as_of_index, pivot_bars, "swing_low")
    if not indices:
        return None
    idx = indices[-1]
    return idx, float(df.loc[idx, "low"])


def latest_confirmed_swing_high(
    df: pd.DataFrame, as_of_index: int, pivot_bars: int = 3
) -> tuple[int, float] | None:
    """(index, high) of the most recent swing high confirmed as of
    `as_of_index`, or None if there isn't one yet."""
    indices = _confirmed_swing_indices(df, as_of_index, pivot_bars, "swing_high")
    if not indices:
        return None
    idx = indices[-1]
    return idx, float(df.loc[idx, "high"])


def is_higher_low(df: pd.DataFrame, as_of_index: int, pivot_bars: int = 3) -> bool:
    """Bull "market structure" component (Appendix A §1.1): price made the
    latest confirmed higher low, and is currently holding above it."""
    indices = _confirmed_swing_indices(df, as_of_index, pivot_bars, "swing_low")
    if len(indices) < 2:
        return False
    latest_idx, prev_idx = indices[-1], indices[-2]
    latest_low = df.loc[latest_idx, "low"]
    prev_low = df.loc[prev_idx, "low"]
    current_close = df.loc[as_of_index, "close"]
    return bool(latest_low > prev_low and current_close > latest_low)


def is_lower_high(df: pd.DataFrame, as_of_index: int, pivot_bars: int = 3) -> bool:
    """Bear "market structure" component (Appendix A §1.2), symmetric to
    `is_higher_low`: price made the latest confirmed lower high, and is
    currently holding below it."""
    indices = _confirmed_swing_indices(df, as_of_index, pivot_bars, "swing_high")
    if len(indices) < 2:
        return False
    latest_idx, prev_idx = indices[-1], indices[-2]
    latest_high = df.loc[latest_idx, "high"]
    prev_high = df.loc[prev_idx, "high"]
    current_close = df.loc[as_of_index, "close"]
    return bool(latest_high < prev_high and current_close < latest_high)
