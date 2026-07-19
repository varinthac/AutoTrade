"""Independent look-ahead-bias validation -- spec.md §4: "the engine is
validated once against a known-good tool on a trivial strategy to rule out
look-ahead bias."

spec.md §4 also explicitly scopes vectorbt/backtrader OUT as anything to pull
in now (they're a narrower, later, secondary addition at most) -- so instead
of a heavy third-party "known-good tool", `_naive_reference_backtest` below
is a fresh, deliberately separate, naively-coded single-trade backtest loop:
plain Python, no pandas vectorization, no imports from `backtest/engine.py`,
short enough to read top to bottom and hand-verify. It implements the same
*documented* (not code-shared) fill/exit rules from `engine.py`'s module
docstring, written from scratch. Two independently-written implementations
of the same mechanical rules landing on identical numbers, bar for bar, to
the last decimal, is real cross-check evidence against a shared blind spot in
the real engine's fill-timing/exit-detection indexing -- unlike a test that
just re-asserts the engine's own logic against itself.

This file is NOT a general-purpose engine unit-test suite (see
`test_engine.py` for that) -- its only job is this one validation, run over
one trivial, fixed (no signal-generation logic at all) BUY idea per case.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd
import pytest

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan

# point=1.0, tick_size=tick_value=1.0 -> point_value=1.0: every price unit of
# movement is worth exactly 1 currency unit per 1.0 lot, so every number in
# this file is hand-traceable without a unit-conversion step.
SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=1.0,
    tick_size=1.0, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

STARTING_EQUITY = 10_000.0
RISK_PCT = 1.0  # risk_amount = 100
COST_MODEL = CostModelConfig(commission_per_lot=0.0)  # commission-free: isolate fill/exit mechanics only


def _bars(rows: list[dict]) -> pd.DataFrame:
    times = pd.date_range("2026-07-06 00:00:00", periods=len(rows), freq="h")  # Monday start
    return pd.DataFrame([{"time": t, **row} for t, row in zip(times, rows)])


def _fixed_signal_at(index: int, plan: OrderPlan):
    def _signal_fn(df, as_of_index, **kwargs):
        return plan if as_of_index == index else None
    return _signal_fn


@dataclass
class _NaiveResult:
    fill_price: float
    exit_price: float
    exit_reason: Literal["stop_loss", "take_profit", "end_of_data"]
    exit_bar_index: int
    net_pnl: float


def _naive_reference_backtest(
    bars: list[dict],
    signal_index: int,
    direction: Literal["BUY", "SELL"],
    stop_loss: float,
    take_profit: float,
    lot_size: float,
) -> _NaiveResult:
    """Enter at `bars[signal_index + 1]`'s open (plus spread+slippage, same
    additive `(spread + slippage) * point` convention as `cost_model.
    spread_slippage_price`, slippage defaulting to the bar's own spread),
    then scan forward bar by bar for the first SL/TP touch: a gapped-through
    open overrides the nominal SL, and a same-bar double-touch resolves to
    SL first -- exactly the rules documented (not code-shared) in `engine.
    py`'s module docstring. Deliberately no pandas, no whole-series
    look-back at once -- just a plain index-by-index loop.
    """
    fill_index = signal_index + 1
    fill_bar = bars[fill_index]
    spread_points = fill_bar["spread"]
    slippage_points = spread_points  # cost_model default: 1 spread of slippage
    cost = (spread_points + slippage_points) * SYMBOL.point
    fill_price = fill_bar["open"] + cost if direction == "BUY" else fill_bar["open"] - cost

    exit_price = None
    exit_reason = None
    exit_index = None
    for i in range(fill_index, len(bars)):
        bar = bars[i]
        if direction == "BUY":
            if bar["open"] <= stop_loss:
                exit_price, exit_reason = bar["open"], "stop_loss"
            elif bar["low"] <= stop_loss:
                exit_price, exit_reason = stop_loss, "stop_loss"
            elif bar["high"] >= take_profit:
                exit_price, exit_reason = take_profit, "take_profit"
        else:
            if bar["open"] >= stop_loss:
                exit_price, exit_reason = bar["open"], "stop_loss"
            elif bar["high"] >= stop_loss:
                exit_price, exit_reason = stop_loss, "stop_loss"
            elif bar["low"] <= take_profit:
                exit_price, exit_reason = take_profit, "take_profit"

        if exit_price is not None:
            exit_index = i
            break

    if exit_price is None:
        exit_index = len(bars) - 1
        exit_price = bars[-1]["close"]
        exit_reason = "end_of_data"

    sign = 1.0 if direction == "BUY" else -1.0
    net_pnl = sign * (exit_price - fill_price) * lot_size  # point_value=1.0

    return _NaiveResult(
        fill_price=fill_price, exit_price=exit_price, exit_reason=exit_reason,
        exit_bar_index=exit_index, net_pnl=net_pnl,
    )


def test_clean_take_profit_hit_matches_the_independent_reference_and_could_only_pass_with_next_bar_open_fill():
    # Signal fires at bar 3, whose close is 50 -- wildly unlike bar 4's open
    # of 200. If the engine (or the naive reference) wrongly filled at the
    # signal bar's close, or at the plan's nominal entry (200, matching by
    # coincidence here), the recorded fill/exit numbers would be
    # unmistakably wrong against the hand-computed expectation below.
    rows = [
        {"open": 150, "high": 151, "low": 149, "close": 150, "spread": 0},  # 0
        {"open": 150, "high": 151, "low": 149, "close": 150, "spread": 0},  # 1
        {"open": 150, "high": 151, "low": 149, "close": 150, "spread": 0},  # 2
        {"open": 150, "high": 155, "low": 145, "close": 50, "spread": 0},   # 3: signal bar, close=50
        {"open": 200, "high": 205, "low": 195, "close": 202, "spread": 4},  # 4: fill bar, open=200
        {"open": 210, "high": 215, "low": 205, "close": 212, "spread": 0},  # 5: no touch (SL 190/TP 220)
        {"open": 214, "high": 216, "low": 208, "close": 213, "spread": 0},  # 6: no touch
        {"open": 213, "high": 222, "low": 209, "close": 218, "spread": 0},  # 7: TP touched (high 222 >= 220)
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 8: filler (already closed)
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 9
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 10
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 11
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 12
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 13
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 14
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 15
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 16
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 17
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 18
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 19
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 20
        {"open": 218, "high": 219, "low": 217, "close": 218, "spread": 0},  # 21
    ]
    assert len(rows) == 22

    plan = OrderPlan(direction="BUY", entry=200.0, stop_loss=190.0, take_profit=220.0, stop_distance=10.0)
    naive = _naive_reference_backtest(rows, signal_index=3, direction="BUY", stop_loss=190.0, take_profit=220.0, lot_size=10.0)

    # Hand-computed expectation, independent of both implementations:
    # fill = bar4 open(200) + (4 spread + 4 defaulted slippage) * point(1.0) = 208.
    # exit = nominal TP 220 (bar7's high of 222 overshoots it, but the fill is
    # the resting-limit-order convention: nominal price, not the bar's high).
    # net_pnl = (220 - 208) * point_value(1.0) * lot(10.0) = 120.
    assert naive.fill_price == pytest.approx(208.0)
    assert naive.fill_price != pytest.approx(50.0)   # not the signal bar's close
    assert naive.exit_price == pytest.approx(220.0)
    assert naive.exit_reason == "take_profit"
    assert naive.net_pnl == pytest.approx(120.0)

    df = _bars(rows)
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=COST_MODEL, signal_fn=_fixed_signal_at(3, plan),
    )
    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.lot_size == pytest.approx(10.0)  # so naive's lot_size=10.0 above is the real, matching lot

    # The actual cross-check: real engine vs independent reference, identical
    # to the last decimal.
    assert trade.entry_price == pytest.approx(naive.fill_price)
    assert trade.exit_price == pytest.approx(naive.exit_price)
    assert trade.exit_reason == naive.exit_reason
    assert trade.net_pnl == pytest.approx(naive.net_pnl)
    assert trade.gross_pnl == pytest.approx(naive.net_pnl)  # commission=0 here, so gross==net


def test_gap_through_stop_loss_matches_the_independent_reference_sell_direction():
    # Both other cases in this file are BUY-only -- sign-handling bugs
    # (`_fill_entry_price`'s open-cost-vs-open+cost branch, `_check_exit`'s
    # flipped open/high/low comparisons for SELL, `_close_trade`'s sign
    # multiplier) are a classic "passes on BUY, silently wrong on SELL"
    # blind spot that a BUY-only cross-check can never catch. This mirrors
    # `test_gap_through_stop_loss_matches_the_independent_reference` above
    # (same stop_distance=50, same spread=2 fill bar, same weekend-gap
    # shape) but shorting into a gap-UP through the stop -- the real,
    # worse price a short pays when the market gaps against it.
    rows = [
        {"open": 1000, "high": 1001, "low": 999, "close": 1000, "spread": 0},  # 0
        {"open": 1000, "high": 1001, "low": 999, "close": 1000, "spread": 0},  # 1
        {"open": 1000, "high": 1001, "low": 999, "close": 1000, "spread": 0},  # 2
        {"open": 1000, "high": 1005, "low": 995, "close": 1700, "spread": 0},  # 3: signal bar, close=1700 (wild, unlike fill)
        {"open": 1000, "high": 1010, "low": 995, "close": 1005, "spread": 2},  # 4: fill bar, open=1000
        {"open": 995, "high": 1000, "low": 990, "close": 992, "spread": 0},    # 5: no touch (SL 1050/TP 900)
        {"open": 993, "high": 998, "low": 985, "close": 990, "spread": 0},     # 6: no touch
        {"open": 1100, "high": 1110, "low": 1095, "close": 1105, "spread": 0}, # 7: gapped above SL(1050) on open
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 8: filler (already closed)
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 9
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 10
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 11
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 12
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 13
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 14
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 15
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 16
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 17
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 18
        {"open": 1105, "high": 1106, "low": 1104, "close": 1105, "spread": 0}, # 19
    ]
    assert len(rows) == 20

    plan = OrderPlan(direction="SELL", entry=1000.0, stop_loss=1050.0, take_profit=900.0, stop_distance=50.0)
    # stop_distance=50 (same as the BUY gap case above) -> compute_lot_size
    # independently sizes this at risk_amount(100) / (50 * point_value(1.0))
    # = 2.0 lots, same as that case; passed explicitly to the naive
    # reference so both sides use the same lot the real engine will compute.
    naive = _naive_reference_backtest(rows, signal_index=3, direction="SELL", stop_loss=1050.0, take_profit=900.0, lot_size=2.0)

    # Hand-computed expectation:
    # fill = bar4 open(1000) - (2 spread + 2 defaulted slippage) * point(1.0) = 996
    #   (SELL fills at open MINUS cost: receive less to sell).
    # exit = bar7's actual open(1100), gapped above the nominal SL(1050).
    # net_pnl = sign(-1 for SELL) * (1100 - 996) * point_value(1.0) * lot(2.0)
    #         = -1 * 104 * 2.0 = -208 (a loss: price rose, bad for a short).
    assert naive.fill_price == pytest.approx(996.0)
    assert naive.exit_price == pytest.approx(1100.0)
    assert naive.exit_reason == "stop_loss"
    assert naive.net_pnl == pytest.approx(-208.0)

    df = _bars(rows)
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=COST_MODEL, signal_fn=_fixed_signal_at(3, plan),
    )
    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "SELL"
    assert trade.lot_size == pytest.approx(2.0)  # matches naive's lot_size=2.0 above

    assert trade.entry_price == pytest.approx(naive.fill_price)
    assert trade.exit_price == pytest.approx(naive.exit_price)
    assert trade.exit_reason == naive.exit_reason
    assert trade.net_pnl == pytest.approx(naive.net_pnl)
    assert trade.gross_pnl == pytest.approx(naive.net_pnl)


def test_gap_through_stop_loss_matches_the_independent_reference():
    # A weekend/session gap bar whose OPEN has already jumped past the
    # nominal SL (950) -- both implementations must exit at that bar's
    # actual open (900), the real worse price, not the nominal SL.
    rows = [
        {"open": 900, "high": 901, "low": 899, "close": 900, "spread": 0},  # 0
        {"open": 900, "high": 901, "low": 899, "close": 900, "spread": 0},  # 1
        {"open": 900, "high": 901, "low": 899, "close": 900, "spread": 0},  # 2
        {"open": 900, "high": 905, "low": 895, "close": 300, "spread": 0},  # 3: signal bar, close=300
        {"open": 1000, "high": 1010, "low": 995, "close": 1005, "spread": 2},  # 4: fill bar, open=1000
        {"open": 1010, "high": 1015, "low": 1005, "close": 1008, "spread": 0},  # 5: no touch (SL 950/TP 1100)
        {"open": 1005, "high": 1012, "low": 1000, "close": 1006, "spread": 0},  # 6: no touch
        {"open": 900, "high": 910, "low": 880, "close": 895, "spread": 0},  # 7: gapped below SL(950) on open
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 8: filler (already closed)
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 9
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 10
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 11
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 12
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 13
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 14
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 15
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 16
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 17
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 18
        {"open": 895, "high": 896, "low": 894, "close": 895, "spread": 0},  # 19
    ]
    assert len(rows) == 20

    plan = OrderPlan(direction="BUY", entry=1000.0, stop_loss=950.0, take_profit=1100.0, stop_distance=50.0)
    naive = _naive_reference_backtest(rows, signal_index=3, direction="BUY", stop_loss=950.0, take_profit=1100.0, lot_size=2.0)

    # Hand-computed expectation:
    # fill = bar4 open(1000) + (2 spread + 2 defaulted slippage) * point(1.0) = 1004.
    # exit = bar7's actual open(900), gapped below the nominal SL(950).
    # net_pnl = (900 - 1004) * point_value(1.0) * lot(2.0) = -208.
    assert naive.fill_price == pytest.approx(1004.0)
    assert naive.exit_price == pytest.approx(900.0)
    assert naive.exit_reason == "stop_loss"
    assert naive.net_pnl == pytest.approx(-208.0)

    df = _bars(rows)
    config = BacktestConfig(
        starting_equity=STARTING_EQUITY, risk_per_trade_pct=RISK_PCT,
        cost_model=COST_MODEL, signal_fn=_fixed_signal_at(3, plan),
    )
    trades = run_backtest(df, "XAUUSD", SYMBOL, config)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.lot_size == pytest.approx(2.0)  # matches naive's lot_size=2.0 above

    assert trade.entry_price == pytest.approx(naive.fill_price)
    assert trade.exit_price == pytest.approx(naive.exit_price)
    assert trade.exit_reason == naive.exit_reason
    assert trade.net_pnl == pytest.approx(naive.net_pnl)
    assert trade.gross_pnl == pytest.approx(naive.net_pnl)
