"""Connectivity watchdog -- Watchman item 7 (trading_system_summary_v2.md
Appendix A §4.7): "ถ้าระบบขาดการเชื่อมต่อ MT5 เกิน 5 นาที -> แจ้งเตือนทันที
(ไม้ยังปลอดภัยเพราะมี SL ฝั่งโบรก)".

Deliberately small: this class does NOT attempt reconnection, does NOT touch
MT5 itself, and does NOT take any protective action on the positions
themselves -- existing positions stay safe purely because every position
this system opens always carries a broker-side hard stop-loss set at open
time (Appendix A §4 item 1), which keeps protecting the position whether or
not THIS system can currently see the account. The watchdog's only job is to
alert a human loudly once the outage has run long enough to matter.

Tracks "the last time an MT5 touch succeeded" and, on each `check()` call,
fires exactly one CRITICAL-level alert per outage once `timeout_minutes` has
elapsed since that last success -- not one alert per polling cycle for the
whole duration of a long outage (that would just be log spam once the fact
is already known). The alert re-arms the next time `record_connected()` is
called after a fresh success.

Injected `Clock`, never reads the OS clock directly (spec.md §2.3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from autotrade.common.clock import Clock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectivityWatchdogConfig:
    """`config/base.yaml`'s `watchman.connectivity_timeout_minutes` (Appendix
    A §4.7). `[adjustable]` per the spec."""

    timeout_minutes: float = 5.0


class ConnectivityWatchdog:
    def __init__(self, clock: Clock, config: ConnectivityWatchdogConfig | None = None) -> None:
        self._clock = clock
        self._config = config or ConnectivityWatchdogConfig()
        self._last_known_good: datetime | None = None
        self._alerted_since_last_good = False

    def record_connected(self) -> None:
        """Call this immediately after any successful MT5 round-trip (e.g.
        once per Watchman loop cycle, right after `get_open_positions()`
        succeeds) -- resets the watchdog's clock and re-arms the alert for
        the next outage."""
        self._last_known_good = self._clock.now()
        self._alerted_since_last_good = False

    def check(self) -> bool:
        """Returns True if more than `timeout_minutes` has elapsed since the
        last known-good connection (and logs one CRITICAL alert, the first
        time this becomes True after a `record_connected()`). Returns False
        if still within the timeout, or if a connection has never been
        recorded yet -- nothing to alert on until at least one success has
        been observed to measure the gap from."""
        if self._last_known_good is None:
            return False

        elapsed = self._clock.now() - self._last_known_good
        if elapsed <= timedelta(minutes=self._config.timeout_minutes):
            return False

        if not self._alerted_since_last_good:
            logger.critical(
                "MT5 CONNECTIVITY LOST for %.1f minutes (timeout=%.1f min) -- ALERTING. "
                "This system is not currently monitoring open positions, but existing "
                "positions remain protected by their own broker-side hard stop-loss, "
                "independent of this system's monitoring (Appendix A §4.7).",
                elapsed.total_seconds() / 60.0, self._config.timeout_minutes,
            )
            self._alerted_since_last_good = True
        return True
