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

**Two clocks, deliberately separate.** `clock` drives the watchdog's own
elapsed-duration/reconnect-detection math (`record_connected()`/`check()`'s
`timeout_minutes` comparison) -- this can be wall-clock (`RealClock`), since
it only measures "how long has it actually been", not a server-day boundary.
`journal_clock` (defaults to `clock` if not given) is the clock whose
`.now()` is written into `AnomalyEventRecord.timestamp` -- `store/journal.py`'s
day-boundary queries bucket by MT5 SERVER day and interleave/order against
server-time rows from other tables in the same daily report, so this MUST be
server time (see `store/models.py`'s module docstring), not UTC/wall-clock,
even though the watchdog's own internal math is fine staying wall-clock-based.
`scripts/run_shadow_loop.py` passes the same `ServerClock` instance already
wired for `CircuitBreaker`/`ShadowLoop`/`WatchmanLoop` as `journal_clock`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from autotrade.common.clock import Clock
from autotrade.notify.telegram import notify
from autotrade.store import journal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectivityWatchdogConfig:
    """`config/base.yaml`'s `watchman.connectivity_timeout_minutes` (Appendix
    A §4.7). `[adjustable]` per the spec."""

    timeout_minutes: float = 5.0


class ConnectivityWatchdog:
    def __init__(
        self,
        clock: Clock,
        config: ConnectivityWatchdogConfig | None = None,
        journal_db_path: Path | None = None,
        journal_clock: Clock | None = None,
    ) -> None:
        self._clock = clock
        self._config = config or ConnectivityWatchdogConfig()
        self._journal_db_path = journal_db_path
        self._journal_clock = journal_clock or clock
        self._last_known_good: datetime | None = None
        self._alerted_since_last_good = False

    def record_connected(self) -> None:
        """Call this immediately after any successful MT5 round-trip (e.g.
        once per Watchman loop cycle, right after `get_open_positions()`
        succeeds) -- resets the watchdog's clock and re-arms the alert for
        the next outage. If the outage just ending was one that already
        alerted (past `timeout_minutes`), sends a matching "restored"
        notification -- otherwise a human who saw the DOWN alert has no way
        to know the system recovered short of watching logs."""
        was_alerted = self._alerted_since_last_good
        self._last_known_good = self._clock.now()
        self._alerted_since_last_good = False
        if was_alerted:
            logger.warning("MT5 connectivity restored.")
            notify("[AutoTrade] ✅ MT5 connectivity restored.")

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
            elapsed_minutes = elapsed.total_seconds() / 60.0
            logger.critical(
                "MT5 CONNECTIVITY LOST for %.1f minutes (timeout=%.1f min) -- ALERTING. "
                "This system is not currently monitoring open positions, but existing "
                "positions remain protected by their own broker-side hard stop-loss, "
                "independent of this system's monitoring (Appendix A §4.7).",
                elapsed_minutes, self._config.timeout_minutes,
            )
            notify(
                f"[AutoTrade] \U0001F6A8 MT5 CONNECTIVITY LOST for {elapsed_minutes:.1f} minutes "
                "(e.g. the MT5 terminal was closed) -- signals are not being evaluated and open "
                "positions are not being actively monitored by this system, but they remain "
                "protected by their own broker-side stop-loss regardless."
            )
            journal.record_anomaly_event(
                timestamp=self._journal_clock.now(), event_type="reconnect",
                details=(
                    f"MT5 connectivity lost for {elapsed_minutes:.1f} minutes "
                    f"(timeout={self._config.timeout_minutes:.1f} min)"
                ),
                db_path=self._journal_db_path,
            )
            self._alerted_since_last_good = True
        return True
