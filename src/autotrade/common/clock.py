"""Clock abstraction — the only source of "now" for decision code.

Per spec.md §2.3: council/, shield/, risk/, watchman/, features/ must never
read the OS clock directly. RealClock is used in live/sandbox; backtest
supplies a SimulatedClock (added when backtest/ is built in Phase 4) so the
same decision code runs unmodified across all three contexts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class RealClock:
    """Wall-clock time, UTC. MT5 server time is a separate concept (see
    common/mt5_time.py) used for daily-loss-reset/news-window boundaries."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
