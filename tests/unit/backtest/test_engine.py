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
from autotrade.backtest.engine import BacktestConfig, _risk_voice_inputs, run_backtest
from autotrade.backtest.news_stub import NoHistoricalNewsDataProvider
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.council.scoring import BullBearScore
from autotrade.features.indicators import atr
from autotrade.features.swing import latest_confirmed_swing_low
from autotrade.shield.checkpoint import ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig, evaluate_watchman
from autotrade.watchman.position_metadata import PositionMetadata

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


def _signal_at_indices(plan_by_index: dict[int, OrderPlan]):
    """Like `_fixed_signal_at` but fires a (possibly different) plan at
    several distinct indices -- needed for the Shield cooldown tests below,
    which fire two same-direction BUY signals at different bars to exercise
    rule 6."""
    def _signal_fn(df, as_of_index, **kwargs):
        return plan_by_index.get(as_of_index)
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


def test_min_lot_risk_cap_pct_threads_into_the_real_compute_lot_size_call():
    # Same below-broker-minimum setup as the test above, but with
    # min_lot_risk_cap_pct set -- proves BacktestConfig.min_lot_risk_cap_pct
    # actually reaches the real risk.sizing.compute_lot_size() call inside
    # run_backtest() (not just added to the dataclass and silently ignored --
    # this project has hit that exact bug pattern before). min_lot_risk =
    # stop_distance(10) * point_value(1.0) * volume_min(0.01) = 0.1, well
    # within 1.0% of equity(10000) = 100 -- the fallback rescues the lot
    # instead of skipping the signal.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
        {"open": 101, "high": 102, "low": 99.5, "close": 101.5, "spread": 0},
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY,
        risk_per_trade_pct=0.0001,  # same tiny risk% as the None-cap test above
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
        min_lot_risk_cap_pct=1.0,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].lot_size == pytest.approx(SYMBOL.volume_min)


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


# --- Phase 6b: BacktestConfig's default signal_fn is now Council wiring ---
#
# Bull/Bear Voice scoring is monkeypatched here (same convention as
# tests/unit/council/test_decision_matrix.py's own docstring explains) so a
# clean BUY fires as soon as a confirmed swing exists, without depending on
# real score-threshold-crossing OHLC -- these two tests only prove the
# *wiring* (BacktestConfig() with no signal_fn override really does drive
# council.decision_matrix.evaluate_council end to end, and correctly treats
# a borderline hypothetical order as "no trade"), not engine mechanics
# (covered exhaustively above with the fake, fully-controlled signal_fn).


def _council_signal_bars(n: int = 40) -> pd.DataFrame:
    """Flat OHLC with a confirmed swing low at index 10 -- mirrors
    tests/unit/council/test_decision_matrix.py's `_order_capable_df`."""
    times = pd.date_range("2026-07-06 00:00:00", periods=n, freq="h")
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    lows[10] = 90.0
    return pd.DataFrame({
        "time": times, "open": closes, "high": highs, "low": lows, "close": closes,
        "spread": [0] * n,
    })


def _score(total: int) -> BullBearScore:
    return BullBearScore(
        score=total, trend_alignment=0, momentum_rsi=0, momentum_macd=0, market_structure=0, confluence=0
    )


def test_default_signal_fn_wires_the_real_council_and_produces_a_buy_trade(monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].direction == "BUY"


def test_default_signal_fn_treats_a_borderline_hypothetical_order_as_no_trade(monkeypatch):
    # Borderline decisions (both scores >= conflict_threshold) build a
    # hypothetical OrderPlan for logging purposes
    # (decision_matrix.CouncilDecision.order_plan) even though
    # decision.direction is None -- the default signal_fn must not mistake
    # that hypothetical plan for a real tradeable one.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(60))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(65))
    df = _council_signal_bars()
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert trades == []


# --- Risk Voice wiring (closes the "Known gap Phase 6b" in the module
# docstring) -- risk_voice_cfg=None (the BacktestConfig default) preserves
# the old behavior, already proven by every test above this section; these
# prove the OPT-IN behavior when a real RiskVoiceConfig is given. ------------


_PERMISSIVE_RISK_VOICE_CFG = RiskVoiceConfig(
    max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
    max_stop_atr_multiple=1e9, session_start_hour=0, session_end_hour=24,
    friday_close_hour=24, max_atr_panic_multiple=1e9,
)
"""Every threshold set maximally permissive so no condition can veto --
isolates whichever single condition a test overrides on top of this."""


def test_risk_voice_inputs_never_looks_ahead_of_as_of_index():
    # Two dataframes identical up to and including as_of_index=4, but
    # wildly different afterward -- a look-ahead bug (e.g. reading
    # df["spread"].iloc[as_of_index + 1], or an ATR series computed over
    # the full df instead of df.iloc[:as_of_index+1]) would make these two
    # calls disagree. current_spread_points/current_atr/avg_*_20d must all
    # be identical between the two, since only indices 0..4 should ever be
    # read for a decision made "as of" bar 4.
    shared_highs = [101.0, 102.0, 103.0, 104.0, 105.0]
    shared_lows = [99.0, 98.0, 97.0, 96.0, 95.0]
    shared_closes = [100.0, 100.5, 101.0, 101.5, 102.0]
    shared_spreads = [3, 4, 5, 6, 7]

    def _df(future_highs, future_lows, future_closes, future_spreads):
        highs = shared_highs + future_highs
        lows = shared_lows + future_lows
        closes = shared_closes + future_closes
        spreads = shared_spreads + future_spreads
        times = pd.date_range("2026-07-06 00:00:00", periods=len(highs), freq="h")
        return pd.DataFrame({
            "time": times, "open": closes, "high": highs, "low": lows, "close": closes, "spread": spreads,
        })

    df_a = _df([200.0, 200.0], [1.0, 1.0], [150.0, 150.0], [500, 500])
    df_b = _df([9999.0, 9999.0, 9999.0], [-500.0, -500.0, -500.0], [-1.0, -1.0, -1.0], [1, 1, 1])

    inputs_a = _risk_voice_inputs(df_a, as_of_index=4)
    inputs_b = _risk_voice_inputs(df_b, as_of_index=4)

    assert inputs_a == pytest.approx(inputs_b)
    assert inputs_a["current_spread_points"] == pytest.approx(7.0)  # bar 4's own spread, not bar 5+'s


def test_risk_voice_cfg_session_hour_reflects_the_actual_signal_bar_not_a_stale_or_initial_hour(monkeypatch):
    # _council_signal_bars() (below) confirmed empirically to fire its BUY
    # signal at bar 13 (2026-07-06 13:00:00) -- a narrow session window
    # containing ONLY that hour must let the trade through, proving the
    # clock check_risk_voice sees reflects bar 13's own time, not bar 0's
    # (00:00) or some other stale value.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    matching_hour_cfg = RiskVoiceConfig(
        max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
        max_stop_atr_multiple=1e9, session_start_hour=13, session_end_hour=14,
        friday_close_hour=24, max_atr_panic_multiple=1e9,
    )
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        risk_voice_cfg=matching_hour_cfg,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1


def test_risk_voice_cfg_session_hour_tracks_a_later_bar_not_a_stale_initial_hour(monkeypatch):
    # Same fixture/hypothesis as the test above, but with a session window
    # (20:00-21:00) that excludes bar 13's hour entirely. Council's mocked
    # scores stay constant across every bar, so a vetoed bar 13 doesn't
    # block the trade forever -- the engine re-evaluates the signal on
    # every subsequent bar until one satisfies Risk Voice too (bar 20,
    # hour=20). If the clock passed to check_risk_voice were stale (stuck
    # at bar 0's hour=0, or bar 13's hour from the test above, or any fixed
    # value), this would either fire at the WRONG bar or never fire at
    # all -- asserting the trade lands specifically at bar 20's fill
    # (21:00, one hour later than bar 13's 14:00 from the previous test)
    # proves the clock genuinely advances bar-by-bar in step with
    # as_of_index, not just "some" permissive value.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    later_hour_cfg = RiskVoiceConfig(
        max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
        max_stop_atr_multiple=1e9, session_start_hour=20, session_end_hour=21,
        friday_close_hour=24, max_atr_panic_multiple=1e9,
    )
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        risk_voice_cfg=later_hour_cfg,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].entry_time == pd.Timestamp("2026-07-06 21:00:00")


def test_risk_voice_cfg_none_never_vetoes_even_outside_any_session(monkeypatch):
    # Default behavior (risk_voice_cfg=None) is untouched by this feature --
    # a trade that a real RiskVoiceConfig would veto on session grounds still
    # places, proving the opt-in nature of the new parameter.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1


def test_risk_voice_cfg_permissive_still_lets_the_trade_through(monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        risk_voice_cfg=_PERMISSIVE_RISK_VOICE_CFG,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].direction == "BUY"


def test_risk_voice_cfg_session_veto_blocks_the_trade_entirely(monkeypatch):
    # session_start_hour == session_end_hour == 0 means `0 <= hour < 0` is
    # always False -- every bar is "outside the session", so this isolates
    # the session condition as the one doing the vetoing.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    always_outside_session_cfg = RiskVoiceConfig(
        max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
        max_stop_atr_multiple=1e9, session_start_hour=0, session_end_hour=0,
        friday_close_hour=24, max_atr_panic_multiple=1e9,
    )
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        risk_voice_cfg=always_outside_session_cfg,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert trades == []


def test_risk_voice_cfg_stop_distance_veto_blocks_the_trade(monkeypatch):
    # A near-zero max_stop_atr_multiple guarantees the stop-distance
    # condition vetoes regardless of the other permissive thresholds.
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    tight_stop_cfg = RiskVoiceConfig(
        max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
        max_stop_atr_multiple=1e-9, session_start_hour=0, session_end_hour=24,
        friday_close_hour=24, max_atr_panic_multiple=1e9,
    )
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        risk_voice_cfg=tight_stop_cfg,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert trades == []


def test_no_historical_news_data_provider_always_returns_empty_list_never_none():
    # Distinguishing [] ("fetched, no event") from None ("fetch failed" ->
    # fail-safe veto per risk_voice.py's convention) is load-bearing here --
    # returning None would veto every single backtest trade on the news
    # condition, defeating the entire point of wiring Risk Voice in.
    provider = NoHistoricalNewsDataProvider()

    events = provider.get_high_impact_events(
        "USD", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02"),
    )

    assert events == []


# --- Watchman exits wiring (closes the "watchman.* is a no-op in backtest"
# gap EXP-002, experiments/experiments_log.md, confirmed) -------------------
#
# Every fixture below shares a 7-bar (indices 0-6) confirmed-swing-low setup
# (a dip to low=90 at index 3, flanked by low=99 on both sides -- confirmed
# once bar 3+pivot_bars(3)=6 has closed) so `_build_watchman_metadata` can
# re-derive `entry_swing_index=3` exactly the way `orchestrator/
# shadow_loop.py`'s live loop does. Every fixture signals at index 6 (fills
# at index 7) using a hand-built OrderPlan, same fake-signal_fn convention as
# every other test in this file.

SIGNAL_INDEX = 6
ENTRY_SWING_INDEX = 3
ENTRY_SWING_LOW = 90.0

_WATCHMAN_PLAN = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=1000.0, stop_distance=10.0)


def _swing_setup_rows() -> list[dict]:
    return [
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": ENTRY_SWING_LOW, "close": 100, "spread": 0},  # 3: the swing low
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
    ]


def _watchman_backtest_config(cfg: WatchmanConfig | None, plan: OrderPlan = _WATCHMAN_PLAN) -> BacktestConfig:
    calls: list[int] = []
    return BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(SIGNAL_INDEX, plan, calls),
        watchman_cfg=cfg,
    )


def _breakeven_then_stop_rows() -> list[dict]:
    return _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 109, "high": 112, "low": 108, "close": 111, "spread": 0},     # 8: breakeven trigger (profit_r=1.0)
        {"open": 105, "high": 106, "low": 99, "close": 100.5, "spread": 0},    # 9: low touches breakeven=100
    ]


def test_watchman_buy_trails_to_breakeven_then_stops_exactly_at_breakeven():
    df = _bars(_breakeven_then_stop_rows())
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(100.0)  # entry price -- the breakeven level, not the original 90.0
    assert trade.exit_time == df["time"].iloc[9]


def test_watchman_cfg_none_never_trails_or_closes_even_when_price_would_trigger_breakeven():
    # Backward-compatibility regression guard: the EXACT same price action
    # that (with watchman_cfg set, above) stops the trade out early at
    # breakeven must, with watchman_cfg=None, leave the ORIGINAL fixed
    # stop_loss(90)/take_profit(1000) untouched -- neither is ever reached
    # by these bars (lowest low is 99, highest high is 112), so the position
    # must ride all the way to end_of_data exactly as it would have before
    # Watchman was wired in.
    df = _bars(_breakeven_then_stop_rows())
    config = _watchman_backtest_config(cfg=None)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "end_of_data"
    assert trade.exit_price == pytest.approx(df["close"].iloc[-1])
    assert trade.exit_price == pytest.approx(100.5)


def _trailing_stop_fixture() -> tuple[pd.DataFrame, WatchmanConfig, float, float]:
    """BUY that breaks even at bar 8 (profit_r=1.0) then trails further at
    bar 9 (profit_r=2.0 >= trail_start_r=1.5). Bar 10's exact stop-hit level
    is derived from the SAME `atr()` primitive the engine's trail math uses
    (period=14, bars up to and including bar 9) rather than hand-typed, so
    this fixture stays correct if the ATR warm-up window ever changes."""
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 105, "high": 112, "low": 104, "close": 110, "spread": 0},     # 8: breakeven (profit_r=1.0)
        {"open": 112, "high": 121, "low": 111, "close": 120, "spread": 0},     # 9: trail further (profit_r=2.0)
    ]
    df_partial = _bars(rows)
    atr9 = float(atr(df_partial["high"], df_partial["low"], df_partial["close"], period=14).iloc[-1])
    expected_trailed_sl = 120.0 - atr9
    assert expected_trailed_sl > 100.0, "test assumption: the trail must land beyond the breakeven level"

    rows.append({
        "open": expected_trailed_sl + 5, "high": expected_trailed_sl + 6,
        "low": expected_trailed_sl - 5, "close": expected_trailed_sl + 2, "spread": 0,
    })
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    return df, cfg, expected_trailed_sl, atr9


def test_watchman_trailed_stop_moves_beyond_breakeven_and_stops_at_the_trailed_level():
    df, cfg, expected_trailed_sl, _atr9 = _trailing_stop_fixture()
    config = _watchman_backtest_config(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(expected_trailed_sl)
    assert trade.exit_price > 100.0  # trailed strictly beyond the breakeven level
    # r_multiple/risk_amount must be computed from the ORIGINAL
    # plan.stop_distance (10.0, per _WATCHMAN_PLAN) -- never from the
    # trailed stop's distance from entry (which would be
    # expected_trailed_sl - 100.0, a completely different, ATR-dependent
    # number). _close_trade's risk_amount = plan.stop_distance * point_value
    # * lot_size, and lot_size cancels out of r_multiple = net_pnl /
    # risk_amount (net_pnl is itself proportional to lot_size, cost=0 here),
    # leaving r_multiple = (exit_price - entry_price) / plan.stop_distance --
    # asserting against that hardcoded 10.0 directly (not re-deriving it from
    # the trailed level) is what pins this to the ORIGINAL distance.
    assert trade.r_multiple == pytest.approx((trade.exit_price - trade.entry_price) / 10.0)
    # Sanity check the two formulas actually diverge on this fixture (i.e.
    # this isn't a coincidental pass): using the trailed stop's own distance
    # from entry as the (wrong) denominator would always yield exactly 1.0R,
    # since exit_price == that same trailed level by construction above.
    assert trade.r_multiple != pytest.approx(1.0)


def test_watchman_engine_decision_matches_a_direct_evaluate_watchman_call():
    # Reuse-parity: the engine must not reimplement evaluate_watchman's
    # decision logic -- a direct call with the same in-memory
    # PositionMetadata/current_sl/current_price/current_atr/as_of_index must
    # produce the exact same new stop-loss the engine's own replay realizes
    # (observable via the resulting trade's exit_price at the bar it's
    # eventually stopped out on).
    df, cfg, expected_trailed_sl, atr9 = _trailing_stop_fixture()
    metadata = PositionMetadata(
        ticket=0, symbol="XAUUSD", direction="BUY", entry_price=100.0,
        initial_stop_distance=10.0, entry_swing_index=ENTRY_SWING_INDEX,
        opened_at=df["time"].iloc[7].to_pydatetime(),
    )

    direct_decision = evaluate_watchman(
        position_metadata=metadata, current_sl=100.0, current_price=120.0, current_atr=atr9,
        df=df, as_of_index=9, now=df["time"].iloc[9].to_pydatetime(), config=cfg,
    )

    assert direct_decision.action == "MODIFY_SL"
    assert direct_decision.new_stop_loss == pytest.approx(expected_trailed_sl)

    config = _watchman_backtest_config(cfg)
    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert trades[0].exit_price == pytest.approx(direct_decision.new_stop_loss)


def test_watchman_no_lookahead_a_bar_that_trails_the_stop_cannot_be_stopped_out_by_it_the_same_bar():
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 105, "high": 112, "low": 95, "close": 110, "spread": 0},      # 8: breakeven trigger -- this bar's
        # OWN low (95) would ALSO touch the newly-computed sl(100) if the
        # engine incorrectly re-checked the SAME bar against its own trail.
        {"open": 105, "high": 106, "low": 95, "close": 100.5, "spread": 0},    # 9: correctly stopped out here instead
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(100.0)
    assert trade.exit_time == df["time"].iloc[9]  # NOT bar 8, the bar that computed the new stop


def test_watchman_trailed_stop_gapped_through_at_next_bars_open_fills_at_that_open():
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 105, "high": 112, "low": 104, "close": 110, "spread": 0},     # 8: breakeven trigger -> sl becomes 100
        {"open": 70, "high": 72, "low": 65, "close": 68, "spread": 0},         # 9: gaps below the trailed sl(100) on open
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(70.0)  # the bar's actual gapped open, not the nominal trailed 100.0


def test_watchman_structure_invalidation_closes_at_the_bars_close_price():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=50.0, take_profit=1000.0, stop_distance=50.0)
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 97, "high": 98, "low": 84, "close": 85, "spread": 0},         # 8: close(85) < swing low(90)
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=5.0, trail_start_r=10.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg, plan=plan)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "structure_invalidation"
    assert trade.exit_price == pytest.approx(85.0)  # the bar's close, not the far-away hard stop(50.0)
    assert trade.exit_time == df["time"].iloc[8]


def test_watchman_time_stop_closes_a_dead_trade_at_the_bars_close_price():
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar (opened_at)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},  # 8: +1h, still under time_stop_hours
        {"open": 100.0, "high": 101, "low": 99, "close": 100.2, "spread": 0},  # 9: +2h, dead trade -> time stop
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=5.0, trail_start_r=10.0, trail_distance_atr=1.0,
        time_stop_hours=2.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "time_stop"
    assert trade.exit_price == pytest.approx(100.2)
    assert trade.exit_time == df["time"].iloc[9]


# --- Gap 1 (test-engineer review of the Watchman-wiring diff): the small
# ad-hoc `test_watchman_cfg_none_never_trails_or_closes_even_when_price_
# would_trigger_breakeven` above only proves Watchman doesn't fire on ONE
# small fixture -- it doesn't prove `watchman_cfg=None` reproduces the
# fixed-SL/TP-only engine's results trade-for-trade over a realistic,
# multi-cycle sequence. This section adds that: a 19-bar, 5-trade fixture
# (BUY and SELL, wins and losses, ordinary TP, ordinary SL, a weekend-style
# gapped SL, and an end-of-data close) whose exact expected `ClosedTrade`
# values are independently hand-derived below (compounding equity through
# `risk.sizing.compute_lot_size`'s own documented formula -- itself already
# unit-tested in `tests/unit/risk/test_sizing.py`, never reimplemented here
# -- and plain P&L arithmetic), the same "independent oracle" convention
# `test_spread_slippage_cost_is_tracked_separately_...` above already
# established for this file. -----------------------------------------------


def _sequential_signals(plans: list[OrderPlan], calls: list[int]):
    """Returns the next plan in `plans`, in order, each time it is called
    while flat -- once exhausted, always returns `None`. Unlike
    `_fixed_signal_at` (one hardcoded plan at one hardcoded bar index), this
    lets one fixture chain several DIFFERENT plans back to back, each firing
    on whatever bar the engine happens to be flat on right after the
    previous trade closes (the same "signal_fn only ever consulted while
    flat" invariant `test_engine_ignores_new_signals_while_a_position_is_
    open_...` above already proved) -- reused here to build a longer,
    independently hand-verifiable multi-trade chain."""
    queue = list(plans)

    def _signal_fn(df, as_of_index, **kwargs):
        calls.append(as_of_index)
        return queue.pop(0) if queue else None

    return _signal_fn


def test_watchman_cfg_none_matches_hand_computed_fixed_sl_tp_trades_across_a_multi_trade_win_loss_sequence():
    # Five back-to-back trades, watchman_cfg=None throughout -- if the
    # Watchman wiring had any effect (even a subtle one) when opted out,
    # SOME trade in this chain would deviate from the plain fixed-SL/TP/
    # gap/end-of-data arithmetic below. Every entry fills at open=100.0 with
    # zero spread/slippage/commission, so entry_price is always exactly
    # 100.0 and gross_pnl == net_pnl == (exit_price - 100.0) * direction_sign
    # * lot_size; only exit_price/exit_reason/lot_size (via compounding
    # equity) differ trade to trade.
    plan1 = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=125.0, stop_distance=10.0)
    plan2 = OrderPlan(direction="SELL", entry=100.0, stop_loss=110.0, take_profit=50.0, stop_distance=10.0)
    plan3 = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=145.0, stop_distance=10.0)
    plan4 = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=200.0, stop_distance=10.0)
    plan5 = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=1000.0, stop_distance=10.0)
    calls: list[int] = []

    rows = [
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},        # 0: signal1 (plan1, BUY)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},    # 1: fill1 @100.0
        {"open": 100, "high": 102, "low": 98, "close": 101, "spread": 0},        # 2: holding, no breach
        {"open": 101, "high": 103, "low": 99, "close": 102, "spread": 0},        # 3: holding, no breach
        {"open": 103, "high": 130, "low": 100, "close": 128, "spread": 0},       # 4: TP1(125) touched; signal2 (plan2, SELL)
        {"open": 100.0, "high": 101, "low": 99, "close": 99.5, "spread": 0},     # 5: fill2 @100.0
        {"open": 100, "high": 103, "low": 97, "close": 99, "spread": 0},         # 6: holding, no breach
        {"open": 99, "high": 104, "low": 96, "close": 98, "spread": 0},          # 7: holding, no breach
        {"open": 102, "high": 112, "low": 95, "close": 109, "spread": 0},        # 8: SL2(110) touched; signal3 (plan3, BUY)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},    # 9: fill3 @100.0
        {"open": 100, "high": 102, "low": 98, "close": 101, "spread": 0},        # 10: holding, no breach
        {"open": 101, "high": 103, "low": 99, "close": 102, "spread": 0},        # 11: holding, no breach
        {"open": 103, "high": 150, "low": 100, "close": 148, "spread": 0},       # 12: TP3(145) touched; signal4 (plan4, BUY)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},    # 13: fill4 @100.0
        {"open": 100, "high": 102, "low": 98, "close": 101, "spread": 0},        # 14: holding, no breach
        {"open": 75, "high": 76, "low": 70, "close": 72, "spread": 0},           # 15: gaps below SL4(90) on open; signal5 (plan5, BUY)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},    # 16: fill5 @100.0
        {"open": 100, "high": 102, "low": 98, "close": 101, "spread": 0},        # 17: holding, no breach
        {"open": 101, "high": 104, "low": 99, "close": 103, "spread": 0},        # 18: last bar, no breach -> end_of_data @103
    ]
    assert len(rows) == 19
    df = _bars(rows)

    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_sequential_signals([plan1, plan2, plan3, plan4, plan5], calls),
        watchman_cfg=None,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 5

    # Hand-derived expected values (compounding equity via
    # risk.sizing.compute_lot_size's documented formula: lot = floor_to_step(
    # equity * risk_pct/100 / stop_distance, 0.01); all stop_distance=10.0,
    # point_value=1.0, commission/spread/slippage=0 so net_pnl == gross_pnl):
    #   trade1: equity=10000.00 -> lot=10.00; BUY TP @125 (delta +25) ->
    #           gross=250.00  -> equity=10250.00
    #   trade2: equity=10250.00 -> lot=10.25; SELL SL @110 (delta +10 adverse)
    #           -> gross=-102.50 -> equity=10147.50
    #   trade3: equity=10147.50 -> raw_lot=10.1475 -> floors to lot=10.14;
    #           BUY TP @145 (delta +45) -> gross=456.30 -> equity=10603.80
    #   trade4: equity=10603.80 -> lot=10.60; BUY gapped SL @75 (delta -25)
    #           -> gross=-265.00 -> equity=10338.80
    #   trade5: equity=10338.80 -> raw_lot=10.3388 -> floors to lot=10.33;
    #           BUY end_of_data @103 (delta +3) -> gross=30.99
    expected = [
        dict(direction="BUY", entry_price=100.0, exit_price=125.0, exit_reason="take_profit",
             lot_size=10.0, net_pnl=250.0, r_multiple=2.5,
             entry_time=df["time"].iloc[1], exit_time=df["time"].iloc[4]),
        dict(direction="SELL", entry_price=100.0, exit_price=110.0, exit_reason="stop_loss",
             lot_size=10.25, net_pnl=-102.5, r_multiple=-1.0,
             entry_time=df["time"].iloc[5], exit_time=df["time"].iloc[8]),
        dict(direction="BUY", entry_price=100.0, exit_price=145.0, exit_reason="take_profit",
             lot_size=10.14, net_pnl=456.3, r_multiple=4.5,
             entry_time=df["time"].iloc[9], exit_time=df["time"].iloc[12]),
        dict(direction="BUY", entry_price=100.0, exit_price=75.0, exit_reason="stop_loss",
             lot_size=10.6, net_pnl=-265.0, r_multiple=-2.5,
             entry_time=df["time"].iloc[13], exit_time=df["time"].iloc[15]),
        dict(direction="BUY", entry_price=100.0, exit_price=103.0, exit_reason="end_of_data",
             lot_size=10.33, net_pnl=30.99, r_multiple=0.3,
             entry_time=df["time"].iloc[16], exit_time=df["time"].iloc[18]),
    ]

    for trade, exp in zip(trades, expected):
        assert trade.direction == exp["direction"]
        assert trade.exit_reason == exp["exit_reason"]
        assert trade.entry_price == pytest.approx(exp["entry_price"])
        assert trade.exit_price == pytest.approx(exp["exit_price"])
        assert trade.lot_size == pytest.approx(exp["lot_size"])
        assert trade.net_pnl == pytest.approx(exp["net_pnl"])
        assert trade.gross_pnl == pytest.approx(exp["net_pnl"])  # cost=0 throughout
        assert trade.cost == pytest.approx(0.0)
        assert trade.spread_slippage_cost == pytest.approx(0.0)
        assert trade.r_multiple == pytest.approx(exp["r_multiple"])
        assert trade.entry_time == exp["entry_time"]
        assert trade.exit_time == exp["exit_time"]


# --- Gap 2 (test-engineer review): every Watchman test above this point is
# BUY-only -- these are the SELL-direction equivalents for the breakeven/
# trail, no-lookahead, and structure-invalidation tests, verified against
# stop_logic.py's actual SELL math (`min()` trail/breakeven candidates,
# profit_r = (entry - current_price) / initial_stop_distance) and
# exit_conditions.py's SELL structure check (close > latest_confirmed_
# swing_high, not the BUY-side swing_low), not just BUY numbers with signs
# flipped. -------------------------------------------------------------

ENTRY_SWING_HIGH = 115.0

_WATCHMAN_PLAN_SELL = OrderPlan(
    direction="SELL", entry=100.0, stop_loss=110.0, take_profit=-800.0, stop_distance=10.0
)


def _swing_setup_rows_sell() -> list[dict]:
    """SELL-side mirror of `_swing_setup_rows`: a confirmed swing HIGH (not
    low) at index 3, flanked by high=101 on both sides, confirmed once bar
    3+pivot_bars(3)=6 has closed -- same shape `_build_watchman_metadata`
    re-derives via `latest_confirmed_swing_high` for a SELL position."""
    return [
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": ENTRY_SWING_HIGH, "low": 99, "close": 100, "spread": 0},  # 3: the swing high
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
        {"open": 100, "high": 101, "low": 99, "close": 100, "spread": 0},
    ]


def _watchman_backtest_config_sell(cfg: WatchmanConfig | None, plan: OrderPlan = _WATCHMAN_PLAN_SELL) -> BacktestConfig:
    calls: list[int] = []
    return BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(SIGNAL_INDEX, plan, calls),
        watchman_cfg=cfg,
    )


def test_watchman_sell_trails_to_breakeven_then_stops_exactly_at_breakeven():
    # stop_logic.compute_updated_stop_loss's SELL branch: profit_r = (entry
    # - current_price) / initial_stop_distance = (100-89)/10 = 1.1 >= 1.0 ->
    # breakeven candidate = entry_price(100.0); candidates=[current_sl(110),
    # 100.0] -> min() = 100.0 (moves DOWN/tighter, the favorable direction
    # for a SELL) -- the mirror image of the BUY test's max()-moves-up.
    df = _bars(_swing_setup_rows_sell() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 95, "high": 96, "low": 88, "close": 89, "spread": 0},         # 8: breakeven trigger (profit_r=1.1)
        {"open": 95, "high": 101, "low": 94, "close": 95, "spread": 0},        # 9: high touches breakeven=100
    ])
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config_sell(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "SELL"
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(100.0)  # entry price -- the breakeven level, not the original 110.0
    assert trade.exit_time == df["time"].iloc[9]


def test_watchman_sell_no_lookahead_a_bar_that_trails_the_stop_cannot_be_stopped_out_by_it_the_same_bar():
    rows = _swing_setup_rows_sell() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 95, "high": 105, "low": 88, "close": 89, "spread": 0},        # 8: breakeven trigger -- this bar's
        # OWN high (105) would ALSO touch the newly-computed sl(100) if the
        # engine incorrectly re-checked the SAME bar against its own trail
        # (the OLD sl was 110, genuinely unreached by this bar's high of 105).
        {"open": 95, "high": 106, "low": 95, "close": 100.2, "spread": 0},     # 9: correctly stopped out here instead
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config_sell(cfg)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "SELL"
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(100.0)
    assert trade.exit_time == df["time"].iloc[9]  # NOT bar 8, the bar that computed the new stop


def test_watchman_sell_structure_invalidation_closes_at_the_bars_close_price():
    # exit_conditions.check_structure_invalidation's SELL branch: CLOSE at
    # as_of_index > latest_confirmed_swing_high (115.0 here), NOT the
    # BUY-side swing_low check.
    plan = OrderPlan(direction="SELL", entry=100.0, stop_loss=150.0, take_profit=-1000.0, stop_distance=50.0)
    rows = _swing_setup_rows_sell() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 112, "high": 121, "low": 111, "close": 120, "spread": 0},     # 8: close(120) > swing high(115)
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=5.0, trail_start_r=10.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config_sell(cfg, plan=plan)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "SELL"
    assert trade.exit_reason == "structure_invalidation"
    assert trade.exit_price == pytest.approx(120.0)  # the bar's close, not the far-away hard stop(150.0)
    assert trade.exit_time == df["time"].iloc[8]


# --- Gap 3 (test-engineer review): no existing test proves a trailed
# position can still exit via its ORDINARY, unmoved take_profit on a later
# bar -- take_profit never moves, only current_sl does, so a prior
# MODIFY_SL must not corrupt this path. -------------------------------------


def test_watchman_trailed_position_still_exits_via_its_original_unmoved_take_profit_later():
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=130.0, stop_distance=10.0)
    rows = _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar
        {"open": 109, "high": 112, "low": 108, "close": 111, "spread": 0},     # 8: breakeven trigger (profit_r=1.1) -> sl becomes 100
        {"open": 115, "high": 118, "low": 110, "close": 117, "spread": 0},     # 9: holding above the trailed sl(100), no breach either way
        {"open": 125, "high": 131, "low": 124, "close": 129, "spread": 0},     # 10: TP(130) touched -- unmoved by trailing
    ]
    df = _bars(rows)
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = _watchman_backtest_config(cfg, plan=plan)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(130.0)  # the ORIGINAL take_profit, never moved by the bar-8 breakeven
    assert trade.exit_time == df["time"].iloc[10]
    assert trade.entry_price == pytest.approx(100.0)
    # A clean 3.0R take-profit win: (130.0 - 100.0) / stop_distance(10.0) --
    # proves the prior trailing (which only ever touched current_sl, never
    # plan.take_profit or plan.stop_distance) didn't corrupt this fixed,
    # positive-R TP-win arithmetic.
    assert trade.r_multiple == pytest.approx(3.0)
    assert trade.lot_size == pytest.approx(10.0)
    assert trade.net_pnl == pytest.approx(300.0)


# --- Gap 4 (test-engineer review): the defensive `_build_watchman_metadata`
# `None` fallback (no confirmed swing at fill time) -- per its docstring
# "should not normally happen", but the code path exists and must not crash
# or silently misbehave. -----------------------------------------------------


def test_watchman_metadata_none_fallback_when_no_confirmed_swing_exists_at_entry_still_exits_via_fixed_sl_tp():
    # A signal firing at as_of_index=0 (the very start of the data) can
    # NEVER have a confirmed swing yet, regardless of the actual low/high
    # values: features/swing.py's `_confirmed_swing_indices` returns []
    # whenever `as_of_index - pivot_bars < pivot_bars`, i.e. as_of_index < 6
    # for the engine's default pivot_bars=3 -- structurally, not just for
    # this particular fixture's data. So `_build_watchman_metadata`'s
    # `latest_confirmed_swing_low` lookup returns None here, and the
    # resulting `_OpenPosition.metadata` is None even though
    # `watchman_cfg` is NOT None -- exercising the "defensively... should
    # not normally happen" fallback branch documented in engine.py.
    assert latest_confirmed_swing_low(
        _bars([
            {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},
            {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},
            {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 0},
        ]),
        as_of_index=0,
        pivot_bars=3,
    ) is None  # confirms the premise: no confirmed swing exists at signal time

    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    calls: list[int] = []
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},        # 0: signal bar (as_of_index=0)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},    # 1: fill bar
        {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 0},       # 2: TP(120) touched
    ])
    cfg = WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=5.0, trail_distance_atr=1.0,
        time_stop_hours=1000.0, dead_trade_r_band=0.3,
    )
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, calls),
        watchman_cfg=cfg,
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(120.0)  # nominal TP, fixed-SL/TP fallback behavior intact
    assert trade.net_pnl == pytest.approx(200.0)


# --- Shield's duplicate-signal cooldown (rule 6), wired into the engine via
# `BacktestConfig.shield_cfg` -- reuses `_swing_setup_rows()`/`SIGNAL_INDEX`
# (confirmed swing low at index 3, first signal fires at index 6) from the
# Watchman fixtures above. A second same-direction BUY signal is fired later
# at index 9, close enough in time to the first trade's entry (index 7) to
# fall inside a 4h cooldown window (elapsed = 2h) -- these tests prove the
# gate is wired, not Shield's own rule-6 correctness (already exhaustively
# covered by tests/unit/shield/test_checkpoint.py). -------------------------

_COOLDOWN_PLAN = OrderPlan(direction="BUY", entry=100.0, stop_loss=95.0, take_profit=1000.0, stop_distance=5.0)


def _cooldown_rows() -> list[dict]:
    return _swing_setup_rows() + [
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 7: fill bar 1
        {"open": 100.0, "high": 100, "low": 90, "close": 94, "spread": 0},     # 8: SL(95) touched -> trade 1 closes
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 9: signal bar 2 (as_of_index=9)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.0, "spread": 0},  # 10: fill bar 2 (last bar)
    ]


def _cooldown_backtest_config(shield_cfg: ShieldConfig | None) -> BacktestConfig:
    return BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_signal_at_indices({SIGNAL_INDEX: _COOLDOWN_PLAN, 9: _COOLDOWN_PLAN}),
        shield_cfg=shield_cfg,
    )


def test_cooldown_blocks_a_same_swing_signal_fired_within_the_window():
    df = _bars(_cooldown_rows())
    config = _cooldown_backtest_config(ShieldConfig(duplicate_signal_cooldown_hours=4.0))

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    # Trade 1 (stop_loss @ index 8) opens; the index-9 signal re-derives the
    # same swing_index=3 shield.check() was told about trade 1, only 2h
    # after trade 1's entry (index7 -> index9) -- inside the 4h cooldown, so
    # it never becomes a second trade.
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"


def test_cooldown_approved_once_the_window_has_elapsed():
    df = _bars(_cooldown_rows())
    # Same 2h gap as above, but the cooldown itself is shorter than that gap.
    config = _cooldown_backtest_config(ShieldConfig(duplicate_signal_cooldown_hours=1.5))

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 2
    assert trades[0].exit_reason == "stop_loss"
    assert trades[1].exit_reason == "end_of_data"  # trade 2 fills at bar 10, the last bar


def test_shield_cfg_none_never_gates_even_within_the_cooldown_window():
    # Same fixture as the blocking test above -- proves the gate is strictly
    # opt-in (matches risk_voice_cfg/watchman_cfg's own None-means-not-
    # modeled convention), not silently on-by-default.
    df = _bars(_cooldown_rows())
    config = _cooldown_backtest_config(None)

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 2
    assert trades[1].exit_reason == "end_of_data"


def test_shield_check_skipped_without_crashing_when_no_confirmed_swing_exists_at_signal_time():
    # Same defensive premise as
    # test_watchman_metadata_none_fallback_when_no_confirmed_swing_exists_at_entry_still_exits_via_fixed_sl_tp
    # above: a signal firing at as_of_index=0 can never have a confirmed
    # swing yet, so `_swing_index_at` returns None and the Shield check must
    # be skipped rather than crash trying to call `shield.check()` with a
    # swing_index of None.
    plan = OrderPlan(direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0)
    df = _bars([
        {"open": 99, "high": 100, "low": 98, "close": 100, "spread": 0},        # 0: signal bar (as_of_index=0)
        {"open": 100.0, "high": 101, "low": 99, "close": 100.5, "spread": 0},   # 1: fill bar
        {"open": 116, "high": 125, "low": 115, "close": 118, "spread": 0},      # 2: TP(120) touched
    ])
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=0.0),
        signal_fn=_fixed_signal_at(0, plan, []),
        shield_cfg=ShieldConfig(),
    )

    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    assert trades[0].exit_reason == "take_profit"
