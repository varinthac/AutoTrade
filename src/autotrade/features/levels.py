"""Key-level confluence per trading_system_summary_v2.md Appendix A §1.1/§1.2:
"ราคาอยู่ใกล้ (<=0.5xATR) แนวรับสำคัญ (daily pivot, round number)".

Day boundaries follow spec.md/Appendix A §0's server-time convention (MT5
broker server time throughout, never UTC or local time) -- `prior_day_ohlc`
groups by the calendar date of each bar's naive server-time `time` column.
"""
from __future__ import annotations

import pandas as pd


def daily_pivot(prior_day_high: float, prior_day_low: float, prior_day_close: float) -> float:
    """Standard floor-trader pivot point: (H + L + C) / 3."""
    return (prior_day_high + prior_day_low + prior_day_close) / 3


def prior_day_ohlc(df: pd.DataFrame, as_of_index: int) -> tuple[float, float, float]:
    """(high, low, close) of the most recently *completed* server-time
    calendar day as of `as_of_index` -- no lookahead: only bars up to and
    including `as_of_index` are considered, and only a day strictly before
    the day of `as_of_index`'s own bar counts as "completed"."""
    if as_of_index < 0 or as_of_index >= len(df):
        raise ValueError(f"as_of_index {as_of_index} is out of bounds for df of length {len(df)}")

    sub = df.iloc[: as_of_index + 1]
    days = sub["time"].dt.normalize()
    current_day = days.iloc[-1]

    prior_days = days[days < current_day]
    if prior_days.empty:
        raise ValueError("no completed prior server-time day available before as_of_index")

    prior_day = prior_days.max()
    day_bars = sub[days == prior_day]
    return (
        float(day_bars["high"].max()),
        float(day_bars["low"].min()),
        float(day_bars["close"].iloc[-1]),
    )


def nearest_round_number(price: float, symbol_digits: int) -> float:
    """Nearest "round number" level for confluence-with-support checks.

    Appendix A doesn't pin down a precise granularity, so this picks a
    defensible convention: whole/half-unit levels (e.g. 4018.00, 4018.50)
    for 2-3 digit symbols (Gold, JPY pairs), and 50-pip levels (e.g. 1.1050)
    for 4-5 digit majors. May need broker/symbol-specific tuning later.
    """
    granularity = 0.5 if symbol_digits <= 3 else 0.0050
    return round(price / granularity) * granularity


def is_near_key_level(price: float, atr_value: float, levels: list[float], multiple: float = 0.5) -> bool:
    """True if `price` is within `multiple`xATR of any level in `levels`
    (Appendix A's exact "<=0.5xATR" confluence rule)."""
    threshold = multiple * atr_value
    return any(abs(price - level) <= threshold for level in levels)
