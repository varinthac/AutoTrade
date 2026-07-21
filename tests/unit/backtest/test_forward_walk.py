"""Tests for backtest/forward_walk.py -- the Auditor's borderline-order
replay (Appendix A §5.4). Mirrors tests/unit/backtest/test_engine.py's
fixture style (same point_value=1.0 SYMBOL, hand-checkable numbers)."""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.forward_walk import simulate_order_forward
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan

SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def _bars(rows: list[dict]) -> pd.DataFrame:
    times = pd.date_range("2026-07-06 00:00:00", periods=len(rows), freq="h")
    return pd.DataFrame([{"time": t, **row} for t, row in zip(times, rows)])


def test_take_profit_hit_first():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 5},   # 0: as-of bar
        {"open": 101, "high": 105, "low": 99, "close": 104, "spread": 5},   # 1: no touch
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 5},  # 2: TP touched
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=5.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "take_profit"
    assert result.exit_index == 2
    assert result.exit_price == pytest.approx(120.0)
    assert result.gross_r == pytest.approx(2.0)  # (120-100)/10
    # cost_r = (5 spread * 0.01 point + 0/point_value) / 10 = 0.05/10 = 0.005
    assert result.net_r == pytest.approx(1.995)


def test_stop_loss_hit_first():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 95, "high": 96, "low": 85, "close": 90, "spread": 0},  # SL touched, TP not
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "stop_loss"
    assert result.exit_price == pytest.approx(90.0)
    assert result.gross_r == pytest.approx(-1.0)
    assert result.net_r == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_the_actual_gapped_open():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 80, "high": 82, "low": 78, "close": 81, "spread": 0},  # gapped below SL on open
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "stop_loss"
    assert result.exit_price == pytest.approx(80.0)
    assert result.gross_r == pytest.approx(-2.0)


def test_same_bar_touches_both_sl_and_tp_stop_loss_takes_priority():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 100, "high": 125, "low": 85, "close": 110, "spread": 0},  # both SL and TP touched
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "stop_loss"
    assert result.exit_price == pytest.approx(90.0)


def test_time_stop_expiry_marks_at_last_window_bar_close():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},  # 0: as-of bar
        {"open": 101, "high": 103, "low": 100, "close": 102, "spread": 0},  # 1
        {"open": 102, "high": 104, "low": 101, "close": 103, "spread": 0},  # 2 (last bar of a 2-bar window)
        {"open": 103, "high": 130, "low": 102, "close": 125, "spread": 0},  # 3: would hit TP, past the window
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=2,
    )
    assert result.outcome == "time_stop"
    assert result.exit_index == 2
    assert result.exit_price == pytest.approx(103.0)
    assert result.gross_r == pytest.approx(0.3)  # (103-100)/10


def test_no_exit_when_data_runs_out_before_the_time_stop_window_elapses():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 101, "high": 103, "low": 100, "close": 102, "spread": 0},
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "no_exit"
    assert result.exit_index is None
    assert result.exit_price is None
    assert result.gross_r is None
    assert result.net_r is None


def test_start_index_beyond_available_data_is_no_exit():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([{"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0}])
    result = simulate_order_forward(
        df, start_index=5, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "no_exit"


def test_sell_direction_gross_r_sign():
    plan = OrderPlan(direction="SELL", entry=100.0, stop_loss=110.0, take_profit=80.0, stop_distance=10.0)
    df = _bars([
        {"open": 99, "high": 101, "low": 98, "close": 99, "spread": 0},
        {"open": 84, "high": 85, "low": 79, "close": 80, "spread": 0},  # TP touched
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=0.0), time_stop_bars=48,
    )
    assert result.outcome == "take_profit"
    assert result.gross_r == pytest.approx(2.0)  # (100-80)/10 for SELL


def test_commission_reduces_net_r_independent_of_lot_size():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 101, "high": 125, "low": 100, "close": 120, "spread": 0},  # TP touched
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=0.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=2.0), time_stop_bars=48,
    )
    # cost_r = (0 + 2.0 / point_value(1.0)) / stop_distance(10.0) = 0.2
    assert result.gross_r == pytest.approx(2.0)
    assert result.net_r == pytest.approx(1.8)


def test_zero_stop_distance_does_not_raise_and_yields_zero_r_multiples():
    # A malformed/corrupt plan (a real OrderPlan never has stop_distance==0,
    # see council/order_construction.py) must not crash the replay with a
    # ZeroDivisionError -- same defensive convention as engine.py's
    # _close_trade guarding its own risk_amount-based division.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=0.0)
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 101, "high": 125, "low": 100, "close": 120, "spread": 0},  # TP touched
    ])
    result = simulate_order_forward(
        df, start_index=1, plan=plan, entry_price=100.0, spread_points_at_entry=5.0,
        symbol_spec=SYMBOL, cost_model=CostModelConfig(commission_per_lot=2.0), time_stop_bars=48,
    )
    assert result.outcome == "take_profit"
    assert result.gross_r == 0.0
    assert result.net_r == 0.0
