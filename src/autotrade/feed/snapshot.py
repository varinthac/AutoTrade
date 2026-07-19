"""MarketSnapshot: the shared dataclass produced by feed/ and consumed by
features/, council/, shield/, risk/, watchman/ (spec.md §2.2 data flow).

Only price data is populated in Phase 1. `news` stays None until the
economic-calendar feed is added (Phase 2/6) — it's on the dataclass now so
downstream modules can be written against the final shape without a later
breaking change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bar:
    time: datetime  # bar OPEN time, UTC, as reported by MT5
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str  # canonical name, e.g. "XAUUSD"
    timeframe: str  # e.g. "H1"
    bar: Bar  # the most recent *closed* bar — never an in-progress bar
    news: None = None  # placeholder — populated from Phase 2 onward
