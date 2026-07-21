"""Tests for watchman/evaluate.py -- priority ordering (Appendix A §4):
structure invalidation > time stop > SL trail update > no action."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from autotrade.watchman.evaluate import WatchmanConfig, evaluate_watchman
from autotrade.watchman.position_metadata import PositionMetadata

ENTRY = 100.0
STOP_DISTANCE = 10.0
OPENED_AT = datetime(2026, 7, 19, 9, 0)


def _meta(**overrides) -> PositionMetadata:
    defaults = dict(
        ticket=1,
        symbol="XAUUSD",
        direction="BUY",
        entry_price=ENTRY,
        initial_stop_distance=STOP_DISTANCE,
        entry_swing_index=0,
        opened_at=OPENED_AT,
    )
    defaults.update(overrides)
    return PositionMetadata(**defaults)


def _df_no_invalidation() -> pd.DataFrame:
    return pd.DataFrame([
        {"close": 100.0, "low": 95.0, "high": 105.0},
        {"close": 101.0, "low": 96.0, "high": 106.0},
    ])


def _df_invalidated() -> pd.DataFrame:
    return pd.DataFrame([
        {"close": 100.0, "low": 95.0, "high": 105.0},  # entry swing low = 95
        {"close": 90.0, "low": 88.0, "high": 96.0},  # closed below 95 -> invalidated
    ])


def test_structure_invalidation_wins_over_a_simultaneous_sl_update_opportunity():
    meta = _meta(opened_at=OPENED_AT)  # recent -- would never time-stop
    config = WatchmanConfig(breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0)

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=130.0,  # deep in profit -- would otherwise trigger a trail SL update
        current_atr=2.0,
        df=_df_invalidated(),
        as_of_index=1,
        now=OPENED_AT + timedelta(hours=1),
        config=config,
    )

    assert decision.action == "CLOSE"
    assert "structure invalidation" in decision.reason
    assert decision.new_stop_loss is None


def test_time_stop_wins_over_a_simultaneous_sl_update_opportunity():
    meta = _meta(opened_at=OPENED_AT)
    # A contrived (but valid) config where the dead-trade R band is wide
    # enough to overlap the breakeven trigger region, purely to construct a
    # single profit_r value at which BOTH the time-stop condition and an
    # SL-update opportunity would independently fire -- isolating the
    # priority-ordering behavior itself from realistic threshold tuning.
    config = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0,
        time_stop_hours=1.0, dead_trade_r_band=2.0,
    )
    now = OPENED_AT + timedelta(hours=2)  # past time_stop_hours=1.0
    current_price = 110.0  # profit_r = 1.0 -- >= breakeven_at_r AND within +/-2.0 dead band

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,
        df=_df_no_invalidation(),
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "CLOSE"
    assert "time stop" in decision.reason
    assert decision.new_stop_loss is None


def test_time_stop_wins_over_a_simultaneous_full_trailing_update_not_just_breakeven():
    # Same shape as test_time_stop_wins_over_a_simultaneous_sl_update_opportunity
    # but the SL-update opportunity that time stop must beat is a full TRAIL
    # candidate (profit_r=1.5 >= trail_start_r), not merely a breakeven move --
    # closes off the possibility of a bug where time stop only outranks the
    # cheaper breakeven case.
    meta = _meta(opened_at=OPENED_AT)
    config = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0,
        time_stop_hours=1.0, dead_trade_r_band=2.0,
    )
    now = OPENED_AT + timedelta(hours=2)  # past time_stop_hours=1.0
    current_price = 115.0  # profit_r = 1.5 -- exactly trail_start_r, full trail active

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,  # trail candidate would be 115 - 2 = 113, a real MODIFY_SL
        df=_df_no_invalidation(),
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "CLOSE"
    assert "time stop" in decision.reason
    assert decision.new_stop_loss is None


def test_structure_invalidation_wins_over_a_simultaneous_time_stop():
    # Both exit conditions are true at once (structure broken AND the
    # position is a dead trade past time_stop_hours) -- the code checks
    # structure invalidation first, so it must win; pins that ordering
    # explicitly rather than leaving it implicit in the source's comment.
    meta = _meta(opened_at=OPENED_AT)
    config = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0,
        time_stop_hours=1.0, dead_trade_r_band=2.0,
    )
    now = OPENED_AT + timedelta(hours=2)  # past time_stop_hours=1.0
    current_price = 110.0  # profit_r = 1.0 -- within the wide +/-2.0 dead band too

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,
        df=_df_invalidated(),  # structure broken
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "CLOSE"
    assert "structure invalidation" in decision.reason
    assert "time stop" not in decision.reason


def test_clean_sl_update_only_case():
    meta = _meta(opened_at=OPENED_AT)
    config = WatchmanConfig(breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0, time_stop_hours=48.0)
    now = OPENED_AT + timedelta(hours=1)  # far from time-stop
    current_price = 110.0  # profit_r = 1.0 -- triggers breakeven move to 100

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,
        df=_df_no_invalidation(),
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "MODIFY_SL"
    assert decision.new_stop_loss == 100.0


def test_both_gates_disabled_turns_a_would_be_modify_sl_into_no_action():
    # Same setup as test_clean_sl_update_only_case (profit_r=1.0, would
    # MODIFY_SL to 100.0 under defaults), but with breakeven_enabled=
    # trail_enabled=False -- structure-invalidation/time-stop don't
    # independently trigger in this fixture (recent opened_at, no
    # invalidation), so the result must be NO_ACTION.
    meta = _meta(opened_at=OPENED_AT)
    config = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0, time_stop_hours=48.0,
        breakeven_enabled=False, trail_enabled=False,
    )
    now = OPENED_AT + timedelta(hours=1)
    current_price = 110.0  # profit_r = 1.0 -- would trigger breakeven move to 100 by default

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,
        df=_df_no_invalidation(),
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "NO_ACTION"
    assert decision.new_stop_loss is None


def test_genuine_no_action_case():
    meta = _meta(opened_at=OPENED_AT)
    config = WatchmanConfig(breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0, time_stop_hours=48.0)
    now = OPENED_AT + timedelta(hours=1)
    current_price = 102.0  # profit_r = 0.2 -- below breakeven, no candidates trigger

    decision = evaluate_watchman(
        position_metadata=meta,
        current_sl=90.0,
        current_price=current_price,
        current_atr=2.0,
        df=_df_no_invalidation(),
        as_of_index=1,
        now=now,
        config=config,
    )

    assert decision.action == "NO_ACTION"
    assert decision.new_stop_loss is None
