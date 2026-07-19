"""Unit test for common/clock.py's RealClock — the only sanctioned source of
"now" for decision code (spec.md §2.3). No sleeping: bounds the returned
value between two instantaneous datetime.now() calls taken immediately
before/after, so the test is deterministic and not time-of-day dependent."""
from __future__ import annotations

from datetime import datetime, timezone

from autotrade.common.clock import RealClock


def test_real_clock_now_returns_tz_aware_utc_within_call_bounds():
    clock = RealClock()

    before = datetime.now(timezone.utc)
    result = clock.now()
    after = datetime.now(timezone.utc)

    assert result.tzinfo is timezone.utc
    assert before <= result <= after
