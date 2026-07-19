"""Broker server time — the single source of "now" for anything compared
against stored bar/tick times (daily-loss reset, news-window boundaries,
Auditor daily reset — spec.md Appendix A §0: "เวลาและ 'วัน' = MT5 server time
ทั้งระบบ").

MT5's `time` fields (both `copy_rates_*` bars and `symbol_info_tick`) are
expressed in the trade server's own timezone, not true UTC — a well-known
quirk of the MetaTrader5 Python package. `datetime.utcfromtimestamp()` reads
that integer as a naive stand-in for the server's own wall-clock reading,
without applying any timezone conversion (this deliberately does NOT produce
true UTC — see feed/poller.py and feed/historical.py for the same
convention applied to bar timestamps).
"""
from __future__ import annotations

from datetime import datetime, timezone

import MetaTrader5 as mt5


class ServerTimeError(RuntimeError):
    pass


def server_now(reference_symbol_broker_name: str) -> datetime:
    """Current MT5 broker server time (naive, not UTC, not local), read from
    the latest tick of `reference_symbol_broker_name`. Requires an active
    mt5_session()."""
    tick = mt5.symbol_info_tick(reference_symbol_broker_name)
    if tick is None or tick.time == 0:
        code, desc = mt5.last_error()
        raise ServerTimeError(
            f"mt5.symbol_info_tick({reference_symbol_broker_name!r}) failed: [{code}] {desc}"
        )
    return datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)


class ServerClock:
    """`common/clock.Clock` implementation backed by MT5 broker server time --
    for anything that must be compared against server-time values (e.g.
    `risk/circuit_breaker.py`'s daily-loss reset boundary, which is
    server-day-based, not UTC-day-based). Requires an active mt5_session()
    for the lifetime of any code calling `.now()`."""

    def __init__(self, reference_symbol_broker_name: str) -> None:
        self._reference_symbol_broker_name = reference_symbol_broker_name

    def now(self) -> datetime:
        return server_now(self._reference_symbol_broker_name)
