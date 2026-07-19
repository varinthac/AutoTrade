"""Tests for risk/circuit_breaker.py -- The CFO, Appendix A §3.3.

Timestamps here stand in for MT5 server time (naive, per common/mt5_time.py's
convention) -- this module is fed via a Clock test double rather than
common/clock.RealClock, since RealClock always returns real wall-clock time.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from autotrade.risk.circuit_breaker import CircuitBreaker


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _breaker(**overrides) -> CircuitBreaker:
    defaults = dict(
        daily_loss_limit_pct=2.0,
        max_consecutive_losses=3,
        max_drawdown_halt_pct=8.0,
    )
    defaults.update(overrides)
    return CircuitBreaker(**defaults)


def test_no_gates_tripped_in_clean_state():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=10000, peak_equity=10000, live_start_equity=10000, as_of=t0)

    state = breaker.check(FixedClock(t0))

    assert not state.daily_loss_halted
    assert not state.consecutive_loss_halted
    assert not state.drawdown_halted
    assert not state.should_downgrade_to_paper
    assert not state.blocks_new_entries


def test_daily_loss_limit_trips_and_blocks_new_entries():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=10000, peak_equity=10000, live_start_equity=10000, as_of=t0)
    breaker.record_trade_close(pnl=-250, closed_at=t0)  # 2.5% >= 2% limit

    state = breaker.check(FixedClock(t0))

    assert state.daily_loss_halted
    assert state.daily_loss_reason is not None
    assert state.blocks_new_entries


def test_daily_loss_uses_realized_plus_floating_pnl():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_trade_close(pnl=-100, closed_at=t0)  # 1% realized, below limit alone
    breaker.record_equity(
        equity=9900, peak_equity=10000, live_start_equity=10000, as_of=t0, floating_pnl=-150
    )  # + 1.5% floating -> 2.5% total >= 2% limit

    state = breaker.check(FixedClock(t0))

    assert state.daily_loss_halted


def test_daily_loss_resets_at_next_server_day():
    breaker = _breaker()
    day1 = datetime(2026, 7, 19, 20, 0)
    breaker.record_equity(equity=10000, peak_equity=10000, live_start_equity=10000, as_of=day1)
    breaker.record_trade_close(pnl=-250, closed_at=day1)

    assert breaker.check(FixedClock(day1)).daily_loss_halted

    day2 = datetime(2026, 7, 20, 1, 0)  # new server day, no new trades closed yet
    state_day2 = breaker.check(FixedClock(day2))

    assert not state_day2.daily_loss_halted


def test_three_consecutive_losses_halts_for_24_hours():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_trade_close(pnl=-10, closed_at=t0)
    breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5))
    breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=10))

    state = breaker.check(FixedClock(t0 + timedelta(minutes=11)))

    assert state.consecutive_loss_halted
    assert state.consecutive_loss_reason is not None
    assert state.blocks_new_entries


def test_consecutive_loss_halt_expires_after_24_hours():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    for i in range(3):
        breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5 * i))

    state_before = breaker.check(FixedClock(t0 + timedelta(hours=23)))
    state_after = breaker.check(FixedClock(t0 + timedelta(hours=24, minutes=15)))

    assert state_before.consecutive_loss_halted
    assert not state_after.consecutive_loss_halted


def test_a_win_resets_the_consecutive_loss_streak():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_trade_close(pnl=-10, closed_at=t0)
    breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5))
    breaker.record_trade_close(pnl=50, closed_at=t0 + timedelta(minutes=10))  # win
    breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=15))

    state = breaker.check(FixedClock(t0 + timedelta(minutes=16)))

    assert not state.consecutive_loss_halted


def test_drawdown_from_peak_halts_entirely():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)

    state = breaker.check(FixedClock(t0))

    assert state.drawdown_halted
    assert state.drawdown_reason is not None
    assert state.blocks_new_entries


def test_drawdown_halt_does_not_auto_clear_on_time_or_equity_recovery():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)
    assert breaker.check(FixedClock(t0)).drawdown_halted

    # Equity fully recovers above the old peak, and a lot of time passes --
    # this must NOT clear the halt on its own.
    t1 = t0 + timedelta(days=90)
    breaker.record_equity(equity=10500, peak_equity=10500, live_start_equity=10000, as_of=t1)

    state = breaker.check(FixedClock(t1))
    assert state.drawdown_halted

    # Only an explicit manual clear lifts it.
    breaker.clear_drawdown_halt()
    assert not breaker.check(FixedClock(t1)).drawdown_halted


def test_equity_15pct_below_live_start_signals_downgrade_without_blocking():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    # 16% below live_start_equity, but only 7.69% below its own peak (9100)
    # -- isolates the downgrade signal from the (separate) drawdown-halt gate.
    breaker.record_equity(equity=8400, peak_equity=9100, live_start_equity=10000, as_of=t0)

    state = breaker.check(FixedClock(t0))

    assert state.should_downgrade_to_paper
    assert state.downgrade_reason is not None
    assert not state.drawdown_halted
    assert not state.blocks_new_entries
