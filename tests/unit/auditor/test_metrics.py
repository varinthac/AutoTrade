"""Tests for auditor/metrics.py -- generic PF/max-DD/win-rate/avg-R
arithmetic, cross-checked against backtest/report.py's own numbers over an
equivalent ClosedTrade list (same inputs must produce the same numbers)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from autotrade.auditor.metrics import compute_trade_metrics
from autotrade.backtest.engine import ClosedTrade
from autotrade.backtest.report import generate_report
from autotrade.store.models import TradeRecord

STARTING_EQUITY = 10_000.0


def _closed_trade(net_pnl: float, r_multiple: float, exit_time: pd.Timestamp) -> ClosedTrade:
    return ClosedTrade(
        symbol="XAUUSD", direction="BUY", entry_time=exit_time, entry_price=100.0,
        exit_time=exit_time, exit_price=100.0, exit_reason="take_profit", lot_size=1.0,
        gross_pnl=net_pnl, cost=0.0, spread_slippage_cost=0.0, net_pnl=net_pnl, r_multiple=r_multiple,
    )


def _trade_record(net_pnl: float, r_multiple: float, exit_time: datetime) -> TradeRecord:
    return TradeRecord(
        symbol="XAUUSD", direction="BUY", entry_time=exit_time, entry_price=100.0,
        exit_time=exit_time, exit_price=100.0, exit_reason="take_profit", lot_size=1.0,
        gross_pnl=net_pnl, cost=0.0, net_pnl=net_pnl, r_multiple=r_multiple, recorded_at=exit_time,
    )


_PNL_R_SEQUENCE = [
    (200.0, 2.0), (-100.0, -1.0), (150.0, 1.5), (-50.0, -0.5), (300.0, 3.0),
    (-80.0, -0.8), (120.0, 1.2), (-40.0, -0.4), (90.0, 0.9), (-60.0, -0.6),
]


def test_compute_trade_metrics_matches_backtest_report_over_equivalent_closed_trades():
    times = pd.date_range("2026-01-01", periods=len(_PNL_R_SEQUENCE), freq="D")
    closed_trades = [
        _closed_trade(pnl, r, t) for (pnl, r), t in zip(_PNL_R_SEQUENCE, times)
    ]
    report = generate_report(closed_trades, STARTING_EQUITY)
    metrics = compute_trade_metrics(closed_trades, STARTING_EQUITY)

    assert metrics.trade_count == report.trade_count
    assert metrics.win_count == report.win_count
    assert metrics.loss_count == report.loss_count
    assert metrics.win_rate == pytest.approx(report.win_rate)
    assert metrics.profit_factor == pytest.approx(report.profit_factor)
    assert metrics.profit_factor_excluding_top_5 == pytest.approx(report.profit_factor_excluding_top_5)
    assert metrics.max_drawdown_pct == pytest.approx(report.max_drawdown_pct)
    assert metrics.avg_r_multiple == pytest.approx(report.avg_r_multiple)
    assert metrics.total_net_pnl == pytest.approx(report.total_net_pnl)


def test_compute_trade_metrics_over_trade_records_matches_same_numbers():
    times = [datetime(2026, 1, 1) for _ in _PNL_R_SEQUENCE]
    # Give each a distinct exit_time so drawdown ordering is deterministic.
    times = [datetime(2026, 1, i + 1) for i in range(len(_PNL_R_SEQUENCE))]
    records = [_trade_record(pnl, r, t) for (pnl, r), t in zip(_PNL_R_SEQUENCE, times)]

    metrics = compute_trade_metrics(records, STARTING_EQUITY)

    assert metrics.trade_count == 10
    assert metrics.win_count == 5
    assert metrics.loss_count == 5
    assert metrics.total_net_pnl == pytest.approx(sum(p for p, _ in _PNL_R_SEQUENCE))


def test_zero_trades_returns_none_for_rate_dependent_fields():
    metrics = compute_trade_metrics([], STARTING_EQUITY)
    assert metrics.trade_count == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.profit_factor_excluding_top_5 is None
    assert metrics.max_drawdown_pct is None
    assert metrics.avg_r_multiple is None
    assert metrics.total_net_pnl == 0.0


def test_profit_factor_excluding_top_5_is_none_when_trade_count_at_or_below_five():
    times = pd.date_range("2026-01-01", periods=5, freq="D")
    closed_trades = [_closed_trade(100.0, 1.0, t) for t in times]
    metrics = compute_trade_metrics(closed_trades, STARTING_EQUITY)
    assert metrics.profit_factor_excluding_top_5 is None


def test_profit_factor_is_inf_when_there_are_wins_and_zero_losses():
    times = pd.date_range("2026-01-01", periods=3, freq="D")
    closed_trades = [_closed_trade(100.0, 1.0, t) for t in times]
    metrics = compute_trade_metrics(closed_trades, STARTING_EQUITY)
    assert metrics.profit_factor == float("inf")


def test_profit_factor_is_zero_when_all_trades_are_exactly_breakeven():
    # gross_profit == 0 AND gross_loss == 0 with trade_count > 0 (every
    # trade net_pnl == 0.0 exactly) -- must not divide by zero/crash, and
    # per the module docstring "0.0 if there are zero wins" must return a
    # concrete 0.0, not None (None is reserved for trade_count == 0).
    times = pd.date_range("2026-01-01", periods=3, freq="D")
    closed_trades = [_closed_trade(0.0, 0.0, t) for t in times]
    metrics = compute_trade_metrics(closed_trades, STARTING_EQUITY)
    assert metrics.trade_count == 3
    assert metrics.gross_profit == 0.0
    assert metrics.gross_loss == 0.0
    assert metrics.profit_factor == 0.0
    assert metrics.win_count == 0
    assert metrics.loss_count == 0
