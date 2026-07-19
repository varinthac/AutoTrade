"""Historical price download for backtesting (Phase 1).

Downloads via MT5's own history cache, dedups, and flags gaps that aren't
explained by the weekend market close — so any real data holes are visible
now rather than silently corrupting a Phase 4 backtest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from autotrade.common.config import REPO_ROOT
from autotrade.common.mt5_time import server_now
from autotrade.common.symbols import to_broker_name
from autotrade.feed.poller import TIMEFRAME_MAP

logger = logging.getLogger(__name__)

HISTORICAL_DIR = REPO_ROOT / "data" / "historical"

_TIMEFRAME_DELTA = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}


class HistoricalDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadResult:
    symbol: str
    timeframe: str
    rows: int
    start: datetime
    end: datetime
    duplicate_rows_dropped: int
    unexplained_gaps: int
    path: Path


def _is_weekend_gap(prev_time: datetime, next_time: datetime) -> bool:
    """True if the gap between two bars is plausibly just the Fri-close ->
    Sun/Mon-open weekend market closure rather than a real data hole."""
    return prev_time.weekday() == 4 and next_time.weekday() in (5, 6, 0) and (
        next_time - prev_time
    ) <= timedelta(days=3, hours=6)


def download_historical(
    symbol: str,
    timeframe: str,
    days: int,
    end: datetime | None = None,
) -> DownloadResult:
    """Requires an active mt5_session(). Saves CSV to data/historical/ and
    returns a summary including any unexplained gaps found."""
    broker_symbol = to_broker_name(symbol)
    mt5_timeframe = TIMEFRAME_MAP[timeframe]

    date_to = end or datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=days)

    rates = mt5.copy_rates_range(broker_symbol, mt5_timeframe, date_from, date_to)
    if rates is None or len(rates) == 0:
        code, desc = mt5.last_error()
        raise HistoricalDownloadError(
            f"copy_rates_range({broker_symbol!r}, {timeframe}, {date_from}, {date_to}) "
            f"returned nothing: [{code}] {desc}"
        )

    df = pd.DataFrame(rates)
    # rates["time"] is MT5 broker server time, not true UTC (see
    # common/mt5_time.py) — pd.to_datetime(..., unit="s") without utc=True
    # reads it as a naive server-time reading, matching feed/poller.py's
    # utcfromtimestamp() convention for the same raw integer.
    df["time"] = pd.to_datetime(df["time"], unit="s")

    before = len(df)
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    duplicates_dropped = before - len(df)
    if duplicates_dropped:
        logger.warning("%s %s: dropped %d duplicate-timestamp rows", symbol, timeframe, duplicates_dropped)

    expected_delta = _TIMEFRAME_DELTA[timeframe]

    now = server_now(broker_symbol)
    last_bar_time = df["time"].iloc[-1].to_pydatetime()
    if last_bar_time + expected_delta > now:
        df = df.iloc[:-1]
        logger.info(
            "%s %s: dropped still-forming last bar at %s (not yet closed as of server time %s)",
            symbol, timeframe, last_bar_time, now,
        )

    unexplained_gaps = 0
    for prev_time, next_time in zip(df["time"], df["time"].shift(-1).dropna()):
        gap = next_time - prev_time
        if gap > expected_delta and not _is_weekend_gap(prev_time, next_time):
            unexplained_gaps += 1
            logger.warning("%s %s: unexplained gap %s -> %s (%s)", symbol, timeframe, prev_time, next_time, gap)

    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = HISTORICAL_DIR / f"{symbol}_{timeframe}.csv"
    df.to_csv(out_path, index=False)

    logger.info(
        "%s %s: %d bars saved to %s (%s -> %s), %d duplicates dropped, %d unexplained gaps",
        symbol, timeframe, len(df), out_path, df["time"].iloc[0], df["time"].iloc[-1],
        duplicates_dropped, unexplained_gaps,
    )

    return DownloadResult(
        symbol=symbol,
        timeframe=timeframe,
        rows=len(df),
        start=df["time"].iloc[0].to_pydatetime(),
        end=df["time"].iloc[-1].to_pydatetime(),
        duplicate_rows_dropped=duplicates_dropped,
        unexplained_gaps=unexplained_gaps,
        path=out_path,
    )
