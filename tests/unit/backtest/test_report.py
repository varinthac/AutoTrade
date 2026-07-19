"""Tests for backtest/report.py -- hand-computed known-input/known-output
cases for every metric, per trading_system_summary_v2.md Appendix A §5.2's
Backtest→Paper promotion-gate metrics (profit factor, max drawdown, sample
size, top-5-excluded profit factor)."""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.backtest.engine import ClosedTrade
from autotrade.backtest.report import format_report, generate_report


def _trade(net_pnl: float, exit_time: str, r_multiple: float = 0.0) -> ClosedTrade:
    """Minimal `ClosedTrade` -- only `net_pnl`, `exit_time`, `r_multiple`
    matter to report.py; the rest are dummy-but-valid values."""
    return ClosedTrade(
        symbol="XAUUSD",
        direction="BUY",
        entry_time=pd.Timestamp(exit_time) - pd.Timedelta(hours=1),
        entry_price=100.0,
        exit_time=pd.Timestamp(exit_time),
        exit_price=100.0,
        exit_reason="take_profit" if net_pnl >= 0 else "stop_loss",
        lot_size=1.0,
        gross_pnl=net_pnl,
        cost=0.0,
        spread_slippage_cost=0.0,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
    )


def test_empty_trades_returns_sensible_nones_and_zeros_not_a_crash():
    report = generate_report([], starting_equity=10_000.0)

    assert report.trade_count == 0
    assert report.win_count == 0
    assert report.loss_count == 0
    assert report.win_rate is None
    assert report.gross_profit == 0.0
    assert report.gross_loss == 0.0
    assert report.profit_factor is None
    assert report.total_net_pnl == 0.0
    assert report.avg_r_multiple is None
    assert report.max_drawdown_pct is None
    assert report.profit_factor_excluding_top_5 is None


def test_win_rate_and_counts():
    trades = [
        _trade(100, "2026-01-01"),
        _trade(-50, "2026-01-02"),
        _trade(200, "2026-01-03"),
        _trade(-30, "2026-01-04"),
        _trade(150, "2026-01-05"),
    ]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.trade_count == 5
    assert report.win_count == 3
    assert report.loss_count == 2
    assert report.win_rate == pytest.approx(0.6)
    assert report.win_rate * report.trade_count == pytest.approx(report.win_count)


def test_profit_factor_normal_case():
    trades = [
        _trade(100, "2026-01-01"),
        _trade(-50, "2026-01-02"),
        _trade(200, "2026-01-03"),
        _trade(-30, "2026-01-04"),
        _trade(150, "2026-01-05"),
    ]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.gross_profit == pytest.approx(450.0)
    assert report.gross_loss == pytest.approx(80.0)
    assert report.profit_factor == pytest.approx(450.0 / 80.0)
    assert report.total_net_pnl == pytest.approx(370.0)


def test_profit_factor_is_infinite_when_there_are_wins_and_zero_losses():
    trades = [_trade(100, "2026-01-01"), _trade(50, "2026-01-02")]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.gross_loss == 0.0
    assert report.profit_factor == float("inf")


def test_profit_factor_is_zero_when_there_are_only_losses():
    trades = [_trade(-100, "2026-01-01"), _trade(-50, "2026-01-02")]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.gross_profit == 0.0
    assert report.profit_factor == pytest.approx(0.0)


def test_avg_r_multiple():
    trades = [
        _trade(100, "2026-01-01", r_multiple=1.0),
        _trade(-50, "2026-01-02", r_multiple=-0.5),
        _trade(200, "2026-01-03", r_multiple=2.0),
    ]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.avg_r_multiple == pytest.approx((1.0 - 0.5 + 2.0) / 3)


def test_max_drawdown_pct_hand_traced_peak_and_trough():
    # starting_equity=1000 -> +200 (equity 1200, new peak) -> -600 (equity
    # 600, drawdown = (1200-600)/1200 = 50%) -> +100 (equity 700, still below
    # the 1200 peak but not a new trough) -> max_drawdown_pct = 50%.
    trades = [
        _trade(200, "2026-01-01"),
        _trade(-600, "2026-01-02"),
        _trade(100, "2026-01-03"),
    ]

    report = generate_report(trades, starting_equity=1000.0)

    assert report.max_drawdown_pct == pytest.approx(50.0)


def test_max_drawdown_pct_sorts_by_exit_time_defensively_even_if_input_is_out_of_order():
    trades = [
        _trade(100, "2026-01-03"),
        _trade(200, "2026-01-01"),  # actually first chronologically
        _trade(-600, "2026-01-02"),  # actually second chronologically
    ]

    report = generate_report(trades, starting_equity=1000.0)

    assert report.max_drawdown_pct == pytest.approx(50.0)


def test_profit_factor_excluding_top_5_with_fewer_than_5_trades_returns_none():
    trades = [_trade(500, "2026-01-01"), _trade(-100, "2026-01-02"), _trade(200, "2026-01-03")]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.profit_factor_excluding_top_5 is None


def test_profit_factor_excluding_top_5_with_more_than_5_trades():
    # 8 trades sorted by net_pnl desc: 500,400,300,200,100 (excluded top 5),
    # then 80,-30,-20 remain -> gross_profit=80, gross_loss=50, PF=1.6.
    trades = [
        _trade(500, "2026-01-01"),
        _trade(400, "2026-01-02"),
        _trade(300, "2026-01-03"),
        _trade(200, "2026-01-04"),
        _trade(100, "2026-01-05"),
        _trade(80, "2026-01-06"),
        _trade(-30, "2026-01-07"),
        _trade(-20, "2026-01-08"),
    ]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.profit_factor_excluding_top_5 == pytest.approx(80.0 / 50.0)


def test_profit_factor_excluding_top_5_with_exactly_5_trades_returns_none():
    trades = [_trade(v, f"2026-01-0{i + 1}") for i, v in enumerate([500, 400, 300, 200, 100])]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.profit_factor_excluding_top_5 is None


def test_max_drawdown_pct_picks_the_larger_of_two_separate_drawdown_episodes():
    # Two fully separate peak->trough->new-peak->trough episodes, so a wrong
    # implementation that reports "the first drawdown encountered", "the sum
    # of all drawdowns", or "peak-to-all-time-low measured from the single
    # highest and lowest equity points regardless of when they occurred"
    # would all disagree with the correct answer here.
    #
    # equity: 10000 -(+2000)-> 12000 (peak1) -(-1200)-> 10800
    #   episode 1 drawdown = (12000-10800)/12000 = 10%
    # equity: 10800 -(+3200)-> 14000 (new peak2, past peak1) -(-4200)-> 9800
    #   episode 2 drawdown = (14000-9800)/14000 = 30% (the larger one)
    # equity: 9800 -(+500)-> 10300 (partial recovery, not a new peak)
    #
    # "first encountered" would wrongly report 10%. "sum of both episodes"
    # would wrongly report 40%. "peak-to-all-time-low" (max equity 14000 vs
    # min equity 9800, ignoring order) happens to coincide with the correct
    # answer here (30%) only because the global min genuinely follows the
    # global max chronologically -- the real proof is that 10% and 40% are
    # both ruled out.
    trades = [
        _trade(2000, "2026-01-01"),
        _trade(-1200, "2026-01-02"),
        _trade(3200, "2026-01-03"),
        _trade(-4200, "2026-01-04"),
        _trade(500, "2026-01-05"),
    ]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.max_drawdown_pct == pytest.approx(30.0)
    assert report.max_drawdown_pct != pytest.approx(10.0)  # not "first episode encountered"
    assert report.max_drawdown_pct != pytest.approx(40.0)  # not "sum of both episodes"


def test_all_breakeven_trades_produce_sensible_not_nonsensical_metrics():
    # net_pnl == 0.0 exactly for every trade: win_count uses `> 0` (excludes
    # 0), loss_count uses `< 0` (excludes 0) -- so these trades are counted
    # in trade_count but neither wins nor losses. Confirm this documented
    # edge case doesn't produce NaN, a crash, or a misleading win_rate.
    trades = [_trade(0.0, "2026-01-01"), _trade(0.0, "2026-01-02"), _trade(0.0, "2026-01-03")]

    report = generate_report(trades, starting_equity=10_000.0)

    assert report.trade_count == 3
    assert report.win_count == 0
    assert report.loss_count == 0
    assert report.win_rate == pytest.approx(0.0)
    assert report.gross_profit == 0.0
    assert report.gross_loss == 0.0
    # Documented convention: "0.0 if there are zero wins (whether or not
    # there are losses)" -- gross_loss==0 here too, but the zero-wins branch
    # takes precedence, per _profit_factor's own zero-wins-first check.
    assert report.profit_factor == pytest.approx(0.0)
    assert report.total_net_pnl == 0.0
    assert report.max_drawdown_pct == pytest.approx(0.0)  # flat equity curve, never dips below start


def test_format_report_all_none_fields_renders_na_without_crashing():
    report = generate_report([], starting_equity=10_000.0)

    text = format_report(report)

    assert text == (
        "Trades: 0 (win 0 / loss 0)\n"
        "Win rate: n/a\n"
        "Gross profit: 0.00  Gross loss: 0.00\n"
        "Profit factor: n/a\n"
        "Profit factor (excl. top 5): n/a\n"
        "Total net P&L: 0.00\n"
        "Avg R multiple: n/a\n"
        "Max drawdown: n/a"
    )


def test_format_report_infinite_profit_factor_renders_literal_inf_not_garbled():
    trades = [_trade(100, "2026-01-01"), _trade(50, "2026-01-02")]
    report = generate_report(trades, starting_equity=10_000.0)
    assert report.profit_factor == float("inf")  # precondition for this test to be meaningful

    text = format_report(report)

    # "Profit factor: inf" immediately followed by a newline: the literal
    # word "inf", not a garbled concatenation with the next line, and not a
    # crash from `f"{float('inf'):.2f}"` (which formatting-fallthrough would
    # actually just render as "inf" too, but a literal "inf%" or "inf.00"
    # would indicate a fallthrough bug of a different shape).
    assert "Profit factor: inf\n" in text
    assert "inf.00" not in text
    assert "inf%" not in text
    # profit_factor_excluding_top_5 is None here (only 2 trades, < 5), so it
    # must render as "n/a", not silently inherit "inf" too.
    assert "Profit factor (excl. top 5): n/a\n" in text
