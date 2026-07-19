"""`SimulatedClock` — the backtest implementation of `common/clock.py`'s
`Clock` protocol (spec.md §2.3: "No direct OS clock reads in decision code").

Structured like `common/mt5_time.py`'s `ServerClock`: a thin object whose
`.now()` returns whatever time it's currently wired to, rather than reading
a live source. Here, `backtest/engine.py`'s event loop pushes the current
bar's own timestamp in via `set()` as it walks forward, so "now" always
matches the bar being processed.

Not consumed by `council/trivial_signal.py` or `council/order_construction.py`
today -- neither reads the Clock. Wired up and advanced every bar anyway,
so Phase 6/7's Council/Watchman/CFO circuit-breaker code (which *will* need
"now" during backtest replay) is a drop-in later, not an engine rewrite.
"""
from __future__ import annotations

from datetime import datetime


class SimulatedClock:
    def __init__(self, initial_time: datetime) -> None:
        self._time = initial_time

    def now(self) -> datetime:
        return self._time

    def set(self, new_time: datetime) -> None:
        self._time = new_time
