"""AutoTrading (MT5 terminal toggle) watchdog.

**Real incident (2026-07-21):** the live shadow loop hit MT5 retcode 10027
(`TRADE_RETCODE_CLIENT_DISABLES_AT`, "AutoTrading disabled by client")
because the MT5 terminal's own AutoTrading/"Algo Trading" toggle (Tools >
Options > Expert Advisors, or the toolbar button) had been switched off -- a
MANUAL, terminal-level setting nothing in this codebase previously
monitored. Every order attempt failed silently from this system's point of
view (each one just came back as an ordinary rejected `OrderResult`,
indistinguishable in the logs from any other broker-side rejection) until a
human happened to notice. This watchdog's only job is to catch that toggle
changing state -- in EITHER direction -- and alert a human immediately via
Telegram, so an accidental disable can't silently block trading for hours,
and so the human knows the moment it's safe to trade again after
re-enabling it.

Deliberately small and MT5-free, mirroring `connectivity_watchdog.py`: this
class never imports `MetaTrader5` itself and never calls
`mt5.terminal_info()` directly -- the caller (the orchestrator layer, where
MT5 is already an accepted dependency) is responsible for reading
`mt5.terminal_info().trade_allowed` each cycle and passing the result (or
`None` if `terminal_info()` itself returned `None`) into `check()`. This
keeps the watchdog importable/testable with zero live MT5 connection, same
rationale as `ConnectivityWatchdog`.

**A `None` reading (`terminal_info()` failed) is "unknown, skip"** -- never
treated as a state change in either direction, no notification, no anomaly
event; this mirrors `get_open_positions()`'s fail-soft handling elsewhere in
this project (a transient read failure must not manufacture a false alert
out of missing data). Critically, a `None` reading does NOT reset the
watchdog's remembered last-known state: if the toggle was last seen `True`,
then a cycle returns `None` (a hiccup), then a later cycle returns `False`,
this is still reported as a `True -> False` transition, not silently lost
across the gap -- only an actual bool reading ever updates the remembered
state.

**Journal timestamp is server time, deliberately separate from any other
clock.** Same reasoning as `connectivity_watchdog.py`'s module docstring:
`store/journal.py`'s day-boundary queries bucket anomaly events by MT5
SERVER day and interleave/order against server-time rows from other tables
in the same daily report, so `AnomalyEventRecord.timestamp` must be server
time (see `store/models.py`'s module docstring) -- this class has no other
clock of its own (unlike `ConnectivityWatchdog`, which also needs a clock
for its own elapsed-duration math), so `journal_clock` is simply the one
clock this class needs. `scripts/run_shadow_loop.py` passes the same
`ServerClock` instance already wired for `CircuitBreaker`/`ShadowLoop`/
`WatchmanLoop`/`ConnectivityWatchdog`'s own `journal_clock`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from autotrade.common.clock import Clock
from autotrade.notify.telegram import notify
from autotrade.store import journal

logger = logging.getLogger(__name__)

_DISABLED_MESSAGE = (
    "[AutoTrade] \U0001F6A8 AutoTrading has just been switched OFF in the MT5 terminal -- "
    "no new orders can be placed until it's turned back on. This is the terminal's own "
    "AutoTrading/\"Algo Trading\" toggle (Tools > Options > Expert Advisors, or the toolbar "
    "button) -- not a bug in this system. Existing open positions are unaffected and remain "
    "protected by their own broker-side stop-loss."
)
_ENABLED_MESSAGE = (
    "[AutoTrade] ✅ AutoTrading is back ON in the MT5 terminal -- trading can resume normally."
)


class AutoTradingWatchdog:
    def __init__(self, journal_clock: Clock, journal_db_path: Path | None = None) -> None:
        self._journal_clock = journal_clock
        self._journal_db_path = journal_db_path
        self._last_known_state: bool | None = None

    def check(self, trade_allowed: bool | None) -> None:
        """Call this once per poll cycle with the CURRENT reading of
        `mt5.terminal_info().trade_allowed` (or `None` if `terminal_info()`
        itself returned `None` this cycle). Notifies (and records an
        anomaly event) on the first check if already disabled, and on every
        subsequent True<->False transition -- see module docstring for the
        full semantics, including the `None`-reading and `None`-in-the-
        middle-of-a-sequence handling.

        Each alert therefore sends two Telegram messages: the custom,
        human-worded one below (`_DISABLED_MESSAGE`/`_ENABLED_MESSAGE`), and
        `journal.record_anomaly_event`'s own generic "Anomaly (event_type)
        at ...: ..." wrapper (needed regardless, to persist the event for
        `get_anomaly_events_for_day`). `notify()` has no rate-limiting/dedupe
        machinery by design (see its own module docstring) -- two related
        messages arriving close together for one real event is an accepted,
        harmless redundancy, not a bug."""
        if trade_allowed is None:
            logger.debug(
                "AutoTrading toggle state could not be read this cycle "
                "(mt5.terminal_info() returned None) -- skipping check, last known state (%s) unchanged.",
                self._last_known_state,
            )
            return

        previous_state = self._last_known_state
        self._last_known_state = trade_allowed

        if previous_state is None:
            if trade_allowed is False:
                self._alert_disabled()
            else:
                logger.info("AutoTrading toggle confirmed ON at startup.")
            return

        if trade_allowed == previous_state:
            return

        if trade_allowed is False:
            self._alert_disabled()
        else:
            self._alert_enabled()

    def _alert_disabled(self) -> None:
        logger.critical(_DISABLED_MESSAGE)
        notify(_DISABLED_MESSAGE)
        journal.record_anomaly_event(
            timestamp=self._journal_clock.now(), event_type="autotrading_disabled",
            details="MT5 terminal AutoTrading toggle switched OFF -- no new orders can be placed.",
            db_path=self._journal_db_path,
        )

    def _alert_enabled(self) -> None:
        logger.warning(_ENABLED_MESSAGE)
        notify(_ENABLED_MESSAGE)
        journal.record_anomaly_event(
            timestamp=self._journal_clock.now(), event_type="autotrading_enabled",
            details="MT5 terminal AutoTrading toggle switched back ON -- trading can resume.",
            db_path=self._journal_db_path,
        )
