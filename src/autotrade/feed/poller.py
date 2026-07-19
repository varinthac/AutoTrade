"""Bar-close poller (Phase 1).

Watches one or more symbols and calls back exactly once per newly-*closed*
bar, never on the still-forming bar. This is the low-level piece the
asyncio orchestrator (built in a later phase) will wrap; kept a plain
blocking loop for now since Phase 0-1 doesn't need concurrency yet.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

import MetaTrader5 as mt5

from autotrade.common.symbols import to_broker_name
from autotrade.feed.snapshot import Bar, MarketSnapshot

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class BarFetchError(RuntimeError):
    pass


def fetch_last_closed_bar(broker_symbol: str, timeframe: str) -> Bar:
    """Position 1 (not 0) is the most recent fully-closed bar — position 0
    is the currently-forming bar and reading it would be look-ahead bias."""
    mt5_timeframe = TIMEFRAME_MAP[timeframe]
    rates = mt5.copy_rates_from_pos(broker_symbol, mt5_timeframe, 1, 1)
    if rates is None or len(rates) == 0:
        code, desc = mt5.last_error()
        raise BarFetchError(f"copy_rates_from_pos({broker_symbol!r}) returned nothing: [{code}] {desc}")

    r = rates[0]
    return Bar(
        time=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
        open=float(r["open"]),
        high=float(r["high"]),
        low=float(r["low"]),
        close=float(r["close"]),
        tick_volume=int(r["tick_volume"]),
        spread=int(r["spread"]),
    )


def poll_new_bars(
    symbols: list[str],
    timeframe: str,
    on_new_bar: Callable[[MarketSnapshot], None],
    poll_interval_sec: float = 5.0,
    max_iterations: int | None = None,
) -> None:
    """Blocking loop. Calls on_new_bar(snapshot) once per symbol the first
    time its last-closed-bar timestamp advances. Requires an active
    mt5_session(). Set max_iterations for tests/manual verification runs;
    leave None to run forever."""
    last_seen: dict[str, datetime] = {}
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        for symbol in symbols:
            broker_symbol = to_broker_name(symbol)
            try:
                bar = fetch_last_closed_bar(broker_symbol, timeframe)
            except BarFetchError:
                logger.exception("Failed to fetch bar for %s", symbol)
                continue

            if last_seen.get(symbol) != bar.time:
                last_seen[symbol] = bar.time
                logger.info(
                    "New closed %s bar for %s: time=%s O=%s H=%s L=%s C=%s spread=%s",
                    timeframe, symbol, bar.time, bar.open, bar.high, bar.low, bar.close, bar.spread,
                )
                on_new_bar(MarketSnapshot(symbol=symbol, timeframe=timeframe, bar=bar))

        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            time.sleep(poll_interval_sec)
