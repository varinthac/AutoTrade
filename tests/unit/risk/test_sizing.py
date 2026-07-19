"""Tests for risk/sizing.py -- The CFO, Appendix A §3.1/§3.2.

`point_value` in these fixtures is chosen as 100 throughout, representing
e.g. tick_value=1 / tick_size=0.01 (a $1-per-tick, 0.01-tick-size instrument),
so `stop_distance * point_value` gives dollars-at-risk per lot directly.
"""
from __future__ import annotations

import pytest

from autotrade.risk.sizing import compute_lot_size


def test_known_inputs_known_output():
    # risk_amount = 10000 * 0.005 = 50; stop_distance = 10; point_value = 100
    # lot = 50 / (10 * 100) = 0.05 -- already an exact multiple of volume_step.
    lot = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=0.5,
        entry=2000,
        stop_loss=1990,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot == pytest.approx(0.05)


def test_lot_rounds_down_never_up():
    # risk_amount = 11400 * 0.005 = 57; stop_distance = 10; point_value = 100
    # raw lot = 57 / 1000 = 0.057 -- must round DOWN to 0.05, never up to 0.06.
    lot = compute_lot_size(
        equity=11400,
        risk_per_trade_pct=0.5,
        entry=100,
        stop_loss=90,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot == pytest.approx(0.05)


def test_below_min_lot_returns_none_no_trade():
    # raw lot = (100*0.001) / (10*100) = 0.0001 -- rounds down to 0.0, below
    # volume_min -- must signal "do not trade" (None), not the min lot.
    lot = compute_lot_size(
        equity=100,
        risk_per_trade_pct=0.1,
        entry=100,
        stop_loss=90,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot is None


def test_lot_capped_at_volume_max():
    lot = compute_lot_size(
        equity=1_000_000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1990,
        point_value=1,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot == pytest.approx(50)


def test_volatility_halving_stacks_with_natural_stop_distance_effect():
    # Normal volatility, narrow stop: lot = (10000*0.01) / (10*100) = 0.1
    lot_normal = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1990,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot_normal == pytest.approx(0.1)

    # High volatility widens the stop to 20 (natural effect alone, no ATR
    # ratio breach yet): lot = (10000*0.01) / (20*100) = 0.05 -- already half
    # of lot_normal from the wider stop alone.
    lot_wide_stop_only = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1980,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
        current_atr=14,
        avg_atr_20d=10,  # ratio 1.4 < 1.5 threshold -- no halving triggered
    )
    assert lot_wide_stop_only == pytest.approx(0.05)

    # Same wide stop, but current_atr now breaches 1.5x avg_atr_20d -> the
    # explicit risk_per_trade halving ALSO applies on top of the wider stop:
    # risk_amount = 10000 * 0.005 = 50; lot = 50 / (20*100) = 0.025 -> 0.02
    lot_wide_stop_and_halved = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1980,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
        current_atr=16,
        avg_atr_20d=10,  # ratio 1.6 > 1.5 threshold -- halving triggered
    )
    assert lot_wide_stop_and_halved == pytest.approx(0.02)
    assert lot_wide_stop_and_halved < lot_wide_stop_only


def test_entry_equal_stop_loss_raises():
    with pytest.raises(ValueError):
        compute_lot_size(
            equity=10000,
            risk_per_trade_pct=0.5,
            entry=2000,
            stop_loss=2000,
            point_value=100,
            volume_min=0.01,
            volume_max=50,
            volume_step=0.01,
        )


def test_equity_must_be_positive():
    with pytest.raises(ValueError):
        compute_lot_size(
            equity=0,
            risk_per_trade_pct=0.5,
            entry=2000,
            stop_loss=1990,
            point_value=100,
            volume_min=0.01,
            volume_max=50,
            volume_step=0.01,
        )


def test_risk_per_trade_pct_must_be_positive():
    with pytest.raises(ValueError):
        compute_lot_size(
            equity=10000,
            risk_per_trade_pct=0,
            entry=2000,
            stop_loss=1990,
            point_value=100,
            volume_min=0.01,
            volume_max=50,
            volume_step=0.01,
        )


def test_point_value_must_be_positive():
    with pytest.raises(ValueError):
        compute_lot_size(
            equity=10000,
            risk_per_trade_pct=0.5,
            entry=2000,
            stop_loss=1990,
            point_value=0,
            volume_min=0.01,
            volume_max=50,
            volume_step=0.01,
        )


def test_lot_exactly_at_volume_min_is_accepted_not_rejected():
    # raw lot = (10000*0.001) / (10*100) = 0.01 -- exactly volume_min. The
    # gate is "< volume_min", so an exact match must be accepted, not
    # treated as "below minimum".
    lot = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=0.1,
        entry=2000,
        stop_loss=1990,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot == pytest.approx(0.01)


def test_volatility_ratio_exactly_at_threshold_does_not_trigger_halving():
    # current_atr / avg_atr_20d == 1.5 exactly -- the halving condition is
    # a strict ">", so the exact threshold must NOT halve risk_per_trade.
    lot = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1990,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
        current_atr=15.0,
        avg_atr_20d=10.0,  # ratio exactly 1.5
        volatility_multiplier_threshold=1.5,
    )
    # No halving: lot = (10000*0.01) / (10*100) = 0.1
    assert lot == pytest.approx(0.1)


def test_avg_atr_20d_zero_is_treated_as_no_volatility_data_no_division_error():
    # avg_atr_20d=0 must not raise ZeroDivisionError and must not trigger
    # halving (guarded by the explicit `avg_atr_20d > 0` check).
    lot = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=1.0,
        entry=2000,
        stop_loss=1990,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
        current_atr=5.0,
        avg_atr_20d=0.0,
    )
    assert lot == pytest.approx(0.1)


def test_sell_direction_stop_above_entry_uses_absolute_distance():
    # SELL: stop_loss above entry -- (entry - stop_loss) is negative, but
    # stop_distance must be treated as an absolute distance.
    lot = compute_lot_size(
        equity=10000,
        risk_per_trade_pct=0.5,
        entry=1990,
        stop_loss=2000,
        point_value=100,
        volume_min=0.01,
        volume_max=50,
        volume_step=0.01,
    )
    assert lot == pytest.approx(0.05)
