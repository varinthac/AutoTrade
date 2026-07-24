"""Known-inputs-known-output tests for backtest/cost_model.py."""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.backtest.cost_model import (
    CostModelConfig,
    SwapModelConfig,
    commission_cost,
    effective_swap_nights,
    round_trip_cost,
    spread_slippage_price,
    swap_cost,
)
from autotrade.common.symbol_spec import SymbolSpec

# 2026-01-05 is a Monday: 05=Mon, 06=Tue, 07=Wed, 08=Thu, 09=Fri, 10=Sat, 11=Sun.
_MON = "2026-01-05"
_TUE = "2026-01-06"
_WED = "2026-01-07"
_THU = "2026-01-08"
_FRI = "2026-01-09"
_NEXT_MON = "2026-01-12"

SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def test_cost_model_config_defaults_are_the_documented_placeholder():
    config = CostModelConfig()
    assert config.commission_per_lot == 0.0
    assert config.slippage_points is None
    assert config.swap_model is None  # swap "not modeled" placeholder


_SWAP = SwapModelConfig(long_per_lot_per_night=-53.2, short_per_lot_per_night=36.8)


def test_effective_swap_nights_single_ordinary_overnight_is_one():
    # Mon 10:00 -> Tue 14:00 crosses only the Tue 00:00 rollover (weekday, 1x).
    nights = effective_swap_nights(
        pd.Timestamp(f"{_MON} 10:00"), pd.Timestamp(f"{_TUE} 14:00"), _SWAP
    )
    assert nights == pytest.approx(1.0)


def test_effective_swap_nights_wednesday_rollover_is_triple():
    # Tue 10:00 -> Wed 14:00 crosses the Wed 00:00 rollover (triple-swap day).
    nights = effective_swap_nights(
        pd.Timestamp(f"{_TUE} 10:00"), pd.Timestamp(f"{_WED} 14:00"), _SWAP
    )
    assert nights == pytest.approx(3.0)


def test_effective_swap_nights_weekend_rollovers_are_not_charged():
    # Fri 10:00 -> next Mon 14:00: Sat/Sun 00:00 boundaries are 0x (weekend
    # carry is recovered by the Wednesday 3x), only Mon 00:00 counts.
    nights = effective_swap_nights(
        pd.Timestamp(f"{_FRI} 10:00"), pd.Timestamp(f"{_NEXT_MON} 14:00"), _SWAP
    )
    assert nights == pytest.approx(1.0)


def test_effective_swap_nights_multi_night_span_includes_the_wednesday_triple():
    # Mon 10:00 -> Thu 14:00 crosses Tue(1x) + Wed(3x) + Thu(1x) = 5.
    nights = effective_swap_nights(
        pd.Timestamp(f"{_MON} 10:00"), pd.Timestamp(f"{_THU} 14:00"), _SWAP
    )
    assert nights == pytest.approx(5.0)


def test_effective_swap_nights_intraday_trade_crosses_no_rollover():
    nights = effective_swap_nights(
        pd.Timestamp(f"{_MON} 10:00"), pd.Timestamp(f"{_MON} 20:00"), _SWAP
    )
    assert nights == pytest.approx(0.0)


def test_swap_cost_long_is_a_positive_charge_short_is_a_credit():
    entry, exit_ = pd.Timestamp(f"{_MON} 10:00"), pd.Timestamp(f"{_TUE} 14:00")  # 1 night
    # Long pays: broker rate -53.2 -> +53.2 cost to subtract (per 1.0 lot).
    assert swap_cost("BUY", 1.0, entry, exit_, _SWAP) == pytest.approx(53.2)
    # Short is credited: broker rate +36.8 -> -36.8 (reduces cost) per 1.0 lot.
    assert swap_cost("SELL", 1.0, entry, exit_, _SWAP) == pytest.approx(-36.8)


def test_swap_cost_scales_with_lot_and_triple_wednesday():
    entry, exit_ = pd.Timestamp(f"{_TUE} 10:00"), pd.Timestamp(f"{_WED} 14:00")  # 3 nights (Wed)
    # 0.5 lot long across the triple-swap Wednesday: -53.2 * 0.5 * 3 -> +79.8.
    assert swap_cost("BUY", 0.5, entry, exit_, _SWAP) == pytest.approx(79.8)


def test_spread_slippage_price_defaults_slippage_to_the_bars_own_spread():
    config = CostModelConfig()
    # spread=5 points + slippage defaulted to the same 5 points = 10 points * 0.01 point size
    assert spread_slippage_price(bar_spread_points=5, symbol=SYMBOL, config=config) == pytest.approx(0.10)


def test_spread_slippage_price_uses_an_explicit_slippage_override():
    config = CostModelConfig(slippage_points=2.0)
    assert spread_slippage_price(bar_spread_points=5, symbol=SYMBOL, config=config) == pytest.approx(0.07)


def test_commission_cost_scales_with_lot_size():
    config = CostModelConfig(commission_per_lot=3.5)
    assert commission_cost(lot_size=4.0, config=config) == pytest.approx(14.0)


def test_round_trip_cost_combines_spread_slippage_and_commission():
    config = CostModelConfig(commission_per_lot=2.0)

    cost = round_trip_cost(
        entry_price=100.0, exit_price=120.0, lot_size=10.0,
        bar_spread_points=5, symbol=SYMBOL, config=config,
    )

    # (5 spread + 5 defaulted slippage) points * 0.01 point size * point_value(1.0) * 10 lots
    # + commission_per_lot(2.0) * 10 lots
    assert cost == pytest.approx(0.10 * 1.0 * 10 + 20.0)
    assert cost == pytest.approx(21.0)


# SYMBOL above has point == tick_size == 0.01, so a bug that swapped
# `symbol.point` for `symbol.tick_size` (or vice versa) anywhere in the
# points -> price-delta conversion would be numerically invisible against
# that fixture. DISTINCT_SYMBOL deliberately makes point, tick_size, and
# tick_value all different values (and non-multiples of one another) so any
# such field mix-up produces a detectably wrong number, not a coincidentally
# correct one.
DISTINCT_SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.5,
    tick_size=0.25, tick_value=2.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def test_spread_slippage_price_uses_symbol_point_not_tick_size_or_tick_value():
    config = CostModelConfig(slippage_points=0.0)

    # 4 spread points * symbol.point(0.5) = 2.0 price units -- if the
    # implementation mistakenly used tick_size(0.25) this would be 1.0
    # instead, and if it used tick_value(2.0) this would be 8.0 instead.
    assert spread_slippage_price(
        bar_spread_points=4, symbol=DISTINCT_SYMBOL, config=config
    ) == pytest.approx(2.0)


def test_round_trip_cost_uses_tick_value_over_tick_size_for_point_value_not_point():
    config = CostModelConfig(commission_per_lot=0.0, slippage_points=0.0)

    cost = round_trip_cost(
        entry_price=100.0, exit_price=100.0, lot_size=1.0,
        bar_spread_points=4, symbol=DISTINCT_SYMBOL, config=config,
    )

    # spread_slippage_price = 4 * 0.5 = 2.0 price units.
    # point_value = tick_value(2.0) / tick_size(0.25) = 8.0 currency/price-unit/lot.
    # cost = 2.0 * 8.0 * 1.0 lot = 16.0 -- a symbol.point/tick_size mix-up in
    # either the spread or point_value leg would produce a different number.
    assert cost == pytest.approx(16.0)
