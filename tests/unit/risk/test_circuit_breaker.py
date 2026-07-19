"""Tests for risk/circuit_breaker.py -- The CFO, Appendix A §3.3.

Timestamps here stand in for MT5 server time (naive, per common/mt5_time.py's
convention) -- this module is fed via a Clock test double rather than
common/clock.RealClock, since RealClock always returns real wall-clock time.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from autotrade.risk.circuit_breaker import CircuitBreaker, DEFAULT_STATE_PATH


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


def test_daily_loss_exactly_at_limit_trips_boundary_is_inclusive():
    # loss_pct == 2.0% exactly (the ">=" gate) -- must trip, not require
    # strictly exceeding the limit.
    breaker = _breaker(daily_loss_limit_pct=2.0)
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=10000, peak_equity=10000, live_start_equity=10000, as_of=t0)
    breaker.record_trade_close(pnl=-200, closed_at=t0)  # exactly 2.0%

    state = breaker.check(FixedClock(t0))

    assert state.daily_loss_halted


def test_daily_loss_just_under_limit_does_not_trip():
    breaker = _breaker(daily_loss_limit_pct=2.0)
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=10000, peak_equity=10000, live_start_equity=10000, as_of=t0)
    breaker.record_trade_close(pnl=-199.99, closed_at=t0)  # 1.999%, just under 2.0%

    state = breaker.check(FixedClock(t0))

    assert not state.daily_loss_halted


def test_drawdown_exactly_at_threshold_halts_boundary_is_inclusive():
    breaker = _breaker(max_drawdown_halt_pct=8.0)
    t0 = datetime(2026, 7, 19, 9, 0)
    # exactly 8% down from peak: (10000-9200)/10000*100 = 8.0
    breaker.record_equity(equity=9200, peak_equity=10000, live_start_equity=10000, as_of=t0)

    state = breaker.check(FixedClock(t0))

    assert state.drawdown_halted


def test_drawdown_just_under_threshold_does_not_halt():
    breaker = _breaker(max_drawdown_halt_pct=8.0)
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_equity(equity=9201, peak_equity=10000, live_start_equity=10000, as_of=t0)  # 7.99%

    state = breaker.check(FixedClock(t0))

    assert not state.drawdown_halted


def test_live_downgrade_exactly_at_threshold_signals_boundary_is_inclusive():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    # exactly 15% below live_start_equity: (10000-8500)/10000*100 = 15.0
    breaker.record_equity(equity=8500, peak_equity=10000, live_start_equity=10000, as_of=t0)

    state = breaker.check(FixedClock(t0))

    assert state.should_downgrade_to_paper


def test_consecutive_loss_halt_boundary_now_equal_to_halt_until_is_not_halted():
    # The gate is "now < halt_until" (strict) -- at the exact expiry instant
    # the halt must already be lifted, not still active.
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    for i in range(3):
        breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5 * i))

    halt_until = (t0 + timedelta(minutes=10)) + timedelta(hours=24)
    state = breaker.check(FixedClock(halt_until))

    assert not state.consecutive_loss_halted


def test_a_4th_consecutive_loss_extends_the_halt_window_further():
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    for i in range(3):
        breaker.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5 * i))
    first_halt_until = t0 + timedelta(minutes=10) + timedelta(hours=24)

    # A 4th consecutive loss, well after the 3rd, should push the halt window
    # out further from *this* close, not leave it anchored to the 3rd loss.
    fourth_close = t0 + timedelta(hours=1)
    breaker.record_trade_close(pnl=-10, closed_at=fourth_close)
    new_halt_until = fourth_close + timedelta(hours=24)

    assert new_halt_until > first_halt_until
    # Right after the old (3rd-loss) halt window would have expired, the
    # breaker must still be halted because of the 4th loss's extension.
    state = breaker.check(FixedClock(first_halt_until + timedelta(minutes=1)))
    assert state.consecutive_loss_halted


def test_daily_loss_not_evaluated_when_no_equity_recorded_yet():
    # record_trade_close() alone, with no record_equity() call at all --
    # _latest_equity stays None, so the daily-loss gate must not trip (and
    # must not raise) even though a large loss was recorded.
    breaker = _breaker()
    t0 = datetime(2026, 7, 19, 9, 0)
    breaker.record_trade_close(pnl=-10000, closed_at=t0)

    state = breaker.check(FixedClock(t0))

    assert not state.daily_loss_halted


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


def test_default_state_path_lives_under_data_db():
    assert DEFAULT_STATE_PATH.parent.name == "db"
    assert DEFAULT_STATE_PATH.name == "circuit_breaker_state.json"


def test_state_survives_a_fresh_instance_pointed_at_the_same_file(tmp_path):
    state_path = tmp_path / "circuit_breaker_state.json"
    t0 = datetime(2026, 7, 19, 9, 0)

    original = _breaker(state_path=state_path)
    original.record_equity(equity=9500, peak_equity=10000, live_start_equity=10000, as_of=t0)
    original.record_trade_close(pnl=-50, closed_at=t0)

    restarted = _breaker(state_path=state_path)

    assert restarted.check(FixedClock(t0)).daily_loss_reason == original.check(FixedClock(t0)).daily_loss_reason
    assert restarted._realized_pnl_today == -50
    assert restarted._latest_peak_equity == 10000
    assert restarted._latest_live_start_equity == 10000


def test_active_drawdown_halt_survives_a_simulated_restart(tmp_path):
    # The concrete case that matters most: the drawdown halt must NOT be
    # silently cleared by a process crash/restart -- only clear_drawdown_halt()
    # (an explicit human action) may lift it.
    state_path = tmp_path / "circuit_breaker_state.json"
    t0 = datetime(2026, 7, 19, 9, 0)

    original = _breaker(state_path=state_path)
    original.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)
    assert original.check(FixedClock(t0)).drawdown_halted

    # Simulate a restart: a brand new instance, no in-memory state carried
    # over, pointed at the same persisted file.
    restarted = CircuitBreaker(
        daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0,
        state_path=state_path,
    )

    assert restarted.check(FixedClock(t0)).drawdown_halted
    assert restarted.check(FixedClock(t0)).blocks_new_entries


def test_manual_clear_of_drawdown_halt_persists_across_a_restart(tmp_path):
    state_path = tmp_path / "circuit_breaker_state.json"
    t0 = datetime(2026, 7, 19, 9, 0)

    original = _breaker(state_path=state_path)
    original.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)
    assert original.check(FixedClock(t0)).drawdown_halted

    original.clear_drawdown_halt()

    restarted = _breaker(state_path=state_path)
    assert not restarted.check(FixedClock(t0)).drawdown_halted


def test_no_state_path_means_no_file_written(tmp_path):
    state_path = tmp_path / "circuit_breaker_state.json"
    breaker = _breaker()  # no state_path
    t0 = datetime(2026, 7, 19, 9, 0)

    breaker.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)
    breaker.record_trade_close(pnl=-50, closed_at=t0)
    breaker.clear_drawdown_halt()

    assert not state_path.exists()


def test_missing_state_file_is_a_clean_start_not_an_error(tmp_path):
    state_path = tmp_path / "does_not_exist_yet.json"
    breaker = _breaker(state_path=state_path)

    assert not breaker.check(FixedClock(datetime(2026, 7, 19, 9, 0))).blocks_new_entries


def test_corrupt_state_file_is_treated_as_a_clean_start(tmp_path):
    state_path = tmp_path / "circuit_breaker_state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    breaker = _breaker(state_path=state_path)

    assert not breaker.check(FixedClock(datetime(2026, 7, 19, 9, 0))).blocks_new_entries


def test_active_consecutive_loss_halt_survives_a_simulated_restart(tmp_path):
    # None of the existing persistence tests cover this gate specifically --
    # they only exercise drawdown_halted round-tripping. consecutive_losses /
    # consecutive_loss_halt_until are separate fields in the persisted
    # payload (_load_state/_save_state) that could silently fail to round-trip
    # without any existing test catching it.
    state_path = tmp_path / "circuit_breaker_state.json"
    t0 = datetime(2026, 7, 19, 9, 0)

    original = _breaker(state_path=state_path)
    original.record_trade_close(pnl=-10, closed_at=t0)
    original.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=5))
    original.record_trade_close(pnl=-10, closed_at=t0 + timedelta(minutes=10))
    just_after = t0 + timedelta(minutes=11)
    assert original.check(FixedClock(just_after)).consecutive_loss_halted

    # Simulate a restart: a brand new instance, no in-memory state carried
    # over, pointed at the same persisted file.
    restarted = CircuitBreaker(
        daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0,
        state_path=state_path,
    )

    restarted_state = restarted.check(FixedClock(just_after))
    assert restarted_state.consecutive_loss_halted
    assert restarted_state.blocks_new_entries
    # And the halt boundary itself (not just the boolean) must have
    # round-tripped correctly -- must still lift at the same instant as the
    # original instance would have.
    halt_expiry = t0 + timedelta(minutes=10) + timedelta(hours=24)
    assert not restarted.check(FixedClock(halt_expiry)).consecutive_loss_halted
    assert not original.check(FixedClock(halt_expiry)).consecutive_loss_halted


def test_full_state_round_trip_preserves_every_persisted_field(tmp_path):
    # Populate every field _save_state()/_load_state() are supposed to
    # persist (per their explicit payload dict) in one go, then assert each
    # one individually survives a restart -- rather than only spot-checking
    # a couple of fields (as test_state_survives_a_fresh_instance_pointed_at_
    # the_same_file does for daily-loss/peak/live-start equity), so a future
    # change that silently drops one field from the payload dict gets caught.
    state_path = tmp_path / "circuit_breaker_state.json"
    t0 = datetime(2026, 7, 19, 9, 0)

    original = _breaker(state_path=state_path, max_drawdown_halt_pct=8.0)
    # Drive drawdown_halted True.
    original.record_equity(equity=9100, peak_equity=10000, live_start_equity=10000, as_of=t0)
    # Drive realized_pnl_today / current_server_day.
    original.record_trade_close(pnl=-75, closed_at=t0)
    # Drive consecutive_losses / consecutive_loss_halt_until (2 losses, not
    # yet halted -- distinct from the halted case covered above, proves the
    # *count* itself round-trips, not just the boolean halted outcome).
    original.record_trade_close(pnl=-5, closed_at=t0 + timedelta(minutes=1))

    assert original._drawdown_halted is True
    assert original._realized_pnl_today == -80
    assert original._consecutive_losses == 2
    assert original._consecutive_loss_halt_until is None  # only 2 of 3 losses so far
    assert original._current_server_day == t0.date()
    assert original._latest_peak_equity == 10000
    assert original._latest_live_start_equity == 10000

    restarted = CircuitBreaker(
        daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0,
        state_path=state_path,
    )

    assert restarted._drawdown_halted == original._drawdown_halted
    assert restarted._realized_pnl_today == original._realized_pnl_today
    assert restarted._consecutive_losses == original._consecutive_losses
    assert restarted._consecutive_loss_halt_until == original._consecutive_loss_halt_until
    assert restarted._current_server_day == original._current_server_day
    assert restarted._latest_peak_equity == original._latest_peak_equity
    assert restarted._latest_live_start_equity == original._latest_live_start_equity

    # A 3rd loss on the restarted instance must correctly extend from
    # _consecutive_losses=2 (i.e. it really did load as 2, not silently
    # reset to 0) -- halts on this 3rd loss, proving the loaded count feeds
    # real gate logic, not just being an inert round-tripped number.
    restarted.record_trade_close(pnl=-5, closed_at=t0 + timedelta(minutes=2))
    assert restarted.check(FixedClock(t0 + timedelta(minutes=3))).consecutive_loss_halted
