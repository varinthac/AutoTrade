"""Hand-computed tests for council/order_construction.py, Appendix A §1.4.

Fixed inputs throughout: entry_price = 100, atr = 10, sl_buffer_atr = 0.2
(buffer = 2), sl_min_atr = 0.8 (min_distance = 8), sl_max_atr = 2.5
(max_distance = 25), tp_r_multiple = 2.0 -- so each expected value below is
traced by hand from the raw swing_price, independent of the module's own
arithmetic.
"""
from __future__ import annotations

from autotrade.council.order_construction import build_order_plan

ENTRY = 100.0
ATR = 10.0
SL_BUFFER_ATR = 0.2
SL_MIN_ATR = 0.8
SL_MAX_ATR = 2.5
TP_R_MULTIPLE = 2.0


def test_buy_raw_distance_within_clamp_range_used_as_is():
    # raw_stop = 90 - 2 = 88; raw_distance = 100 - 88 = 12, within [8, 25].
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=90.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.direction == "BUY"
    assert plan.entry == 100.0
    assert plan.stop_distance == 12.0
    assert plan.stop_loss == 88.0
    assert plan.take_profit == 124.0


def test_buy_raw_distance_too_narrow_clamped_up_to_sl_min_atr():
    # raw_stop = 95 - 2 = 93; raw_distance = 100 - 93 = 7, narrower than the
    # 8-point floor -> widened to exactly 8.
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=95.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 8.0
    assert plan.stop_loss == 92.0
    assert plan.take_profit == 116.0


def test_buy_raw_distance_too_wide_clamped_down_to_sl_max_atr():
    # raw_stop = 70 - 2 = 68; raw_distance = 100 - 68 = 32, wider than the
    # 25-point ceiling -> capped to exactly 25. TP must use the clamped 25,
    # not the raw pre-clamp 32 (which would give TP = 100 + 64 = 164).
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=70.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 25.0
    assert plan.stop_loss == 75.0
    assert plan.take_profit == 150.0


def test_sell_raw_distance_within_clamp_range_used_as_is():
    # raw_stop = 110 + 2 = 112; raw_distance = 112 - 100 = 12, within [8, 25].
    plan = build_order_plan(
        "SELL", ENTRY, swing_price=110.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.direction == "SELL"
    assert plan.stop_distance == 12.0
    assert plan.stop_loss == 112.0
    assert plan.take_profit == 76.0


def test_sell_raw_distance_too_narrow_clamped_up_to_sl_min_atr():
    # raw_stop = 105 + 2 = 107; raw_distance = 107 - 100 = 7, narrower than
    # the 8-point floor -> widened to exactly 8.
    plan = build_order_plan(
        "SELL", ENTRY, swing_price=105.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 8.0
    assert plan.stop_loss == 108.0
    assert plan.take_profit == 84.0


def test_sell_raw_distance_too_wide_clamped_down_to_sl_max_atr():
    # raw_stop = 130 + 2 = 132; raw_distance = 132 - 100 = 32, wider than the
    # 25-point ceiling -> capped to exactly 25. TP must use the clamped 25,
    # not the raw pre-clamp 32 (which would give TP = 100 - 64 = 36).
    plan = build_order_plan(
        "SELL", ENTRY, swing_price=130.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 25.0
    assert plan.stop_loss == 125.0
    assert plan.take_profit == 50.0


def test_buy_raw_distance_exactly_at_sl_min_atr_boundary_uses_it_unclamped():
    # raw_stop = 94 - 2 = 92; raw_distance = 100 - 92 = 8, exactly equal to
    # the 8-point floor (min(max(8, 8), 25) = 8) -- boundary must resolve to
    # 8, not be pushed to some other value by an off-by-one in the clamp.
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=94.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 8.0
    assert plan.stop_loss == 92.0
    assert plan.take_profit == 116.0


def test_buy_raw_distance_exactly_at_sl_max_atr_boundary_uses_it_unclamped():
    # raw_stop = 77 - 2 = 75; raw_distance = 100 - 75 = 25, exactly equal to
    # the 25-point ceiling.
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=77.0, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan.stop_distance == 25.0
    assert plan.stop_loss == 75.0
    assert plan.take_profit == 150.0


def test_invalid_direction_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        build_order_plan(
            "HOLD", ENTRY, swing_price=90.0, atr=ATR,  # type: ignore[arg-type]
            sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
            sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
        )


def test_no_confirmed_swing_returns_none():
    plan = build_order_plan(
        "BUY", ENTRY, swing_price=None, atr=ATR,
        sl_buffer_atr=SL_BUFFER_ATR, sl_min_atr=SL_MIN_ATR,
        sl_max_atr=SL_MAX_ATR, tp_r_multiple=TP_R_MULTIPLE,
    )
    assert plan is None
