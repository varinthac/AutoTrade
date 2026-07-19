"""Tests for backtest/engine.py -- the core event-driven loop.

A fake, fully-controlled `signal_fn` (not `council.trivial_signal.
build_trade_idea`) is used throughout, deliberately: it lets each fixture
hand-verify exact fill timing, cost-model application, and SL/TP/gap
priority without also depending on EMA-crossover/swing-detection details
already covered by `tests/unit/council/test_trivial_signal.py`.

All fixtures share the same symbol spec (point_value = tick_value/tick_size
= 1.0, so price differences translate 1:1 into currency per 1.0 lot) and the
same equity/risk setup (equity=10000, risk_per_trade_pct=1.0%, stop_distance
=10 -> lot=10.0), so expected P&L numbers are easy to hand-check.
"""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.backtest.cost_model import CostModelConfig, round_trip_cost
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan

SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

STARTING_EQUITY = 10_000.0
RISK_PCT = 1.0  # -> risk_amount = 100, stop_distance = 10 -> lot = 10.0


def _bars(rows: list[dict]) -> pd.DataFrame:
    times = pd.date_range("2026-07-06 00:00:00", periods=len(rows), freq="h")  # Monday start
    return pd.DataFrame(
        [{"time": t, **row} for t, row in zip(times, rows)]
    )


def _fixed_signal_at(index: int, plan: OrderPlan, calls: list[int]):
    def _signal_fn(df, as_of_index, **kwargs):
        calls.append(as_of_index)
        return plan if as_of_index == index else None
    return _signal_fn


def test_clean_take_profit_hit_applies_entry_cost_and_commission():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},   # 0: signal bar
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 5},  # 1: fill bar
        {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 3},  # 2: TP touched
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=2.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "take_profit"
    # fill = bar1 open(100.0) + (5 spread + 5 defaulted slippage) * 0.01 point = 100.10
    assert trade.entry_price == pytest.approx(100.10)
    assert trade.exit_price == pytest.approx(120.0)  # nominal TP, not the bar's high of 125
    assert trade.lot_size == pytest.approx(10.0)
    assert trade.gross_pnl == pytest.approx(199.0)
    assert trade.cost == pytest.approx(20.0)  # commission only: 2.0/lot * 10 lots
    # spread_slippage_cost = fill's (5 spread + 5 defaulted slippage) * 0.01 point
    # * point_value(1.0) * 10 lots = 0.10 * 1.0 * 10 = 1.0, already folded into
    # entry_price/gross_pnl above but tracked separately here.
    assert trade.spread_slippage_cost == pytest.approx(1.0)
    assert trade.net_pnl == pytest.approx(179.0)
    assert trade.r_multiple == pytest.approx(1.79)


def test_spread_slippage_cost_is_tracked_separately_and_cost_plus_it_recovers_the_full_round_trip_cost():
    # cost_model.round_trip_cost is the module's own independent formula for
    # "spread + slippage + commission in currency" (it doesn't reuse the
    # engine's fill-price math -- see its docstring) so it's a legitimate
    # hand-computed oracle here: if the engine's cost + spread_slippage_cost
    # doesn't match it, one of the two cost-tracking paths is wrong.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 6},  # 1: fill bar, spread=6
        {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 3},  # 2: TP touched
    ])
    cost_model = CostModelConfig(commission_per_lot=1.5)
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=cost_model,
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    # spread_slippage_price = (6 spread + 6 defaulted slippage) * 0.01 point = 0.12 price units
    # spread_slippage_cost = 0.12 * point_value(1.0) * lot(10.0) = 1.2
    assert trade.spread_slippage_cost == pytest.approx(1.2)
    assert trade.cost == pytest.approx(15.0)  # commission: 1.5/lot * 10 lots

    expected_total_cost = round_trip_cost(
        entry_price=trade.entry_price, exit_price=trade.exit_price, lot_size=trade.lot_size,
        bar_spread_points=6, symbol=SYMBOL, config=cost_model,
    )
    assert trade.cost + trade.spread_slippage_cost == pytest.approx(expected_total_cost)
    assert trade.cost + trade.spread_slippage_cost == pytest.approx(16.2)


def test_clean_stop_loss_hit():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},  # fill, no cost
        {"open": 95, "high": 96, "low": 85, "close": 90, "spread": 0},  # SL touched, TP not
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(90.0)  # nominal SL
    assert trade.gross_pnl == pytest.approx(-100.0)
    assert trade.cost == pytest.approx(0.0)
    assert trade.spread_slippage_cost == pytest.approx(0.0)  # zero spread/slippage fill bar
    assert trade.net_pnl == pytest.approx(-100.0)
    assert trade.r_multiple == pytest.approx(-1.0)


def test_same_bar_touches_both_sl_and_tp_stop_loss_takes_priority():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 100, "high": 125, "low": 85, "close": 110, "spread": 0},  # both SL(90) and TP(120) touched
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_price == pytest.approx(90.0)


def test_weekend_gap_through_stop_exits_at_the_actual_gapped_open_not_nominal_sl():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 80, "high": 82, "low": 78, "close": 81, "spread": 0},  # gapped below SL(90) on open
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(80.0)  # the bar's actual open, not the nominal 90.0 SL
    assert trade.gross_pnl == pytest.approx(-200.0)
    assert trade.r_multiple == pytest.approx(-2.0)


def test_position_still_open_at_end_of_data_is_closed_at_last_close_not_dropped():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 101, "high": 102, "low": 99.5, "close": 101.5, "spread": 0},  # last bar, no breach
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "end_of_data"
    assert trade.exit_time == df["time"].iloc[-1]
    assert trade.exit_price == pytest.approx(101.5)
    assert trade.net_pnl == pytest.approx(15.0)


def test_no_lookahead_signal_evaluated_once_and_fill_uses_next_bar_open_not_signal_bar_close():
    # entry/stop_loss are deliberately far from both bar0's close (50) and
    # bar1's open (100) -- if the engine wrongly used the signal bar's close,
    # the plan's nominal entry, or any other stray price for the recorded
    # fill, this would catch it as a detectably wrong number.
    plan = OrderPlan(direction="BUY", entry=105.0, stop_loss=95.0, take_profit=125.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 40, "high": 41, "low": 39, "close": 50, "spread": 0},   # 0: signal bar, close=50
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},  # 1: fill bar, open=100
        {"open": 101, "high": 102, "low": 99.5, "close": 101.5, "spread": 0},  # 2: last bar, no breach
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    # signal_fn is only ever called once, at bar 0, while flat -- never
    # called again once a position/pending order exists, so it structurally
    # never sees bar 1 or bar 2 at the moment the entry decision is made.
    assert calls == [0]

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_price == pytest.approx(100.0)  # bar1's open
    assert trade.entry_price != pytest.approx(50.0)   # not bar0's close
    assert trade.entry_price != pytest.approx(105.0)  # not the plan's nominal entry
    assert trade.entry_time == df["time"].iloc[1]


def test_signal_firing_on_the_very_last_bar_produces_no_trade_no_next_bar_to_fill_on():
    # A signal at the very last index has no bar i+1 to fill at -- spec.md
    # §4's next-bar-open fill guardrail means this must never become a
    # same-bar (or otherwise stray) fill; it must simply not trade.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 101, "high": 102, "low": 99.5, "close": 101.5, "spread": 0},  # last bar: signal fires here
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(2, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert calls == [0, 1, 2]  # engine kept evaluating while flat every bar
    assert trades == []


def test_signal_below_broker_minimum_lot_never_becomes_a_trade():
    # risk_per_trade_pct tiny enough that compute_lot_size rounds down to
    # below SYMBOL.volume_min (0.01) and returns None -- the engine must
    # treat this as "do not trade", not silently floor to the minimum lot.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 101, "high": 102, "low": 99.5, "close": 101.5, "spread": 0},
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY,
        risk_per_trade_pct=0.0001,  # risk_amount=0.01 -> raw_lot=0.001 -> floors to 0.00 < volume_min
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert trades == []


def test_engine_ignores_new_signals_while_a_position_is_open_even_if_signal_fn_always_fires():
    # Unlike the no-lookahead test above (whose fake signal_fn only ever
    # fires once), this signal_fn returns a plan on EVERY call. If the
    # engine had no "only evaluate signal_fn while flat" guard, it would
    # call signal_fn (and try to open a second, overlapping position) on
    # bars where a position is already open/pending. Proves the guard is
    # real, not just an artifact of the fixture never firing twice.
    #
    # Fill timing recap: a signal evaluated at bar i becomes pending and
    # fills at bar i+1's open (checked the same iteration it fills), so:
    #   bar0: flat -> signal fires -> pending
    #   bar1: pending fills (trade1 opens), no breach
    #   bar2: trade1 hits TP and closes; now flat again -> signal re-fires
    #         -> pending2
    #   bar3: pending2 fills (trade2 opens), no breach
    #   bar4, bar5: trade2 stays open, no breach -- signal_fn must NOT be
    #         called on any of bar1, bar3, bar4, bar5 since a position or a
    #         pending order exists throughout.
    #   bar5 (last bar): trade2 still open at end of data -> closed at
    #         bar5's close.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []

    def _always_fires(df, as_of_index, **kwargs):
        calls.append(as_of_index)
        return plan

    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},      # 0: signal (flat) -> pending
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},  # 1: fill trade1, no breach
        {"open": 101, "high": 130, "low": 99.5, "close": 118, "spread": 0},   # 2: TP(120) hit -> trade1 closes; re-fires -> pending2
        {"open": 105, "high": 106, "low": 104, "close": 105, "spread": 0},   # 3: fill trade2, no breach
        {"open": 106, "high": 107, "low": 105, "close": 106, "spread": 0},   # 4: no breach
        {"open": 107, "high": 108, "low": 106, "close": 107, "spread": 0},   # 5: last bar, no breach -> end_of_data
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_always_fires,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    # signal_fn is only ever consulted while flat: bar0 (before trade1) and
    # bar2 (the same bar trade1 closes on, once flat again) -- never while
    # pending/position is live (bar1, bar3, bar4, bar5).
    assert calls == [0, 2]

    assert len(trades) == 2
    trade1, trade2 = trades

    assert trade1.entry_time == df["time"].iloc[1]
    assert trade1.entry_price == pytest.approx(100.0)
    assert trade1.exit_time == df["time"].iloc[2]
    assert trade1.exit_reason == "take_profit"
    assert trade1.exit_price == pytest.approx(120.0)
    assert trade1.lot_size == pytest.approx(10.0)
    assert trade1.net_pnl == pytest.approx(200.0)

    # trade2's lot is sized off the compounded equity after trade1
    # (10000 + 200 = 10200), proving equity compounding carries across
    # trades, not just within one: risk_amount = 10200*1% = 102,
    # lot = 102 / (10 * point_value(1.0)) = 10.2.
    assert trade2.entry_time == df["time"].iloc[3]
    assert trade2.entry_price == pytest.approx(105.0)
    assert trade2.lot_size == pytest.approx(10.2)
    assert trade2.exit_reason == "end_of_data"
    assert trade2.exit_time == df["time"].iloc[5]
    assert trade2.exit_price == pytest.approx(107.0)  # last bar's close
    assert trade2.net_pnl == pytest.approx((107.0 - 105.0) * 1.0 * 10.2)


def test_immediate_exit_on_the_same_bar_the_pending_order_fills():
    # The fill bar is also checked for exit within the same loop iteration
    # -- a stop/TP touched on the very bar a position was just opened must
    # still be honored the same bar, not deferred to the next one.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},   # 0: signal bar
        {"open": 100.0, "high": 125, "low": 99, "close": 118, "spread": 0},  # 1: fill AND TP(120) touched same bar
        {"open": 119, "high": 121, "low": 118, "close": 120, "spread": 0},
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_time == df["time"].iloc[1]
    assert trade.exit_time == df["time"].iloc[1]  # same bar as the fill
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(120.0)


def test_default_zero_commission_is_actually_applied_end_to_end_not_silently_ignored():
    # Uses the bare CostModelConfig() default (no override), unlike every
    # other engine test which passes an explicit commission_per_lot -- this
    # closes the gap where the "0.0 placeholder default" is only verified at
    # the CostModelConfig level (test_cost_model.py), never proven to
    # actually flow through a real trade's cost/net_pnl.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 0},  # TP touched
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(),  # bare default: commission_per_lot=0.0
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.cost == pytest.approx(0.0)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl)


def test_sell_direction_fills_below_open_and_profits_when_price_falls():
    # Every other test in this file uses BUY -- the sign convention for SELL
    # (fill at open - cost, profit when price falls) is otherwise completely
    # untested.
    plan = OrderPlan(direction="SELL", entry=100.0, stop_loss=110.0, take_profit=80.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 101, "high": 102, "low": 100, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 99.5, "spread": 5},  # 1: fill bar, spread=5
        {"open": 84, "high": 85, "low": 79, "close": 80, "spread": 0},  # 2: TP(80) touched
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    # SELL fills at open - cost: 100.0 - (5 spread + 5 defaulted slippage)*0.01 = 99.90
    assert trade.entry_price == pytest.approx(99.90)
    assert trade.entry_price < 100.0  # sold at a worse (lower) price, not a better one
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(80.0)
    # gross_pnl = (entry - exit) * point_value * lot for SELL = (99.90-80.0)*1.0*10 = 199.0
    assert trade.gross_pnl == pytest.approx(199.0)
    assert trade.net_pnl == pytest.approx(199.0)
