"""Backtest reporting -- turns a raw `list[ClosedTrade]` (`backtest/engine.py`'s
output) into the summary metrics trading_system_summary_v2.md Appendix A
§5.2's "เกณฑ์เลื่อนขั้น" (Promotion Gates) Backtest→Paper row needs: profit
factor, max drawdown, trade count, and the top-5-trades-excluded profit-factor
recheck ("ผลไม่พึ่งไม้ top-5 (ตัด 5 ไม้ที่กำไรสูงสุดออกแล้วยังกำไร)").

This module only computes the numbers -- it does not apply the gate
thresholds (≥1.3 profit factor, ≤15% max DD, ≥200 trades) itself; that
pass/fail judgment is the Auditor's job (Phase 8, `auditor/`), consuming
`BacktestReport` as a plain dataclass.

Convention: `gross_profit` and `gross_loss` are both non-negative magnitudes
(the sum of winning `net_pnl`s, and the absolute value of the sum of losing
`net_pnl`s, respectively) -- the standard "profit factor = gross_profit /
gross_loss" definition reads naturally with both terms positive.
"""
from __future__ import annotations

from dataclasses import dataclass

from autotrade.backtest.engine import ClosedTrade

TOP_N_EXCLUDED = 5


@dataclass(frozen=True)
class BacktestReport:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float | None
    """`win_count / trade_count`, or `None` if `trade_count == 0` (no
    meaningful rate to report, not a silent `0.0`)."""
    gross_profit: float
    gross_loss: float
    """Non-negative magnitude -- see module docstring's convention note."""
    profit_factor: float | None
    """`gross_profit / gross_loss`. `None` if there are zero trades. `inf` if
    there are wins and zero losses (undefined-but-favorable, not a crash).
    `0.0` if there are zero wins (whether or not there are losses)."""
    total_net_pnl: float
    avg_r_multiple: float | None
    """`None` if `trade_count == 0`."""
    max_drawdown_pct: float | None
    """Max peak-to-trough percentage decline of the CLOSED-TRADE equity
    curve only -- built by walking realized `net_pnl` in `exit_time` order.
    This does NOT include intra-trade/intrabar drawdown (how far underwater
    a still-open trade went before recovering to close at a profit), so it
    should be understood as a lower bound on true risk, not an exact
    peak-to-trough measure. `None` if `trade_count == 0`."""
    profit_factor_excluding_top_5: float | None
    """`profit_factor` recomputed after removing the `TOP_N_EXCLUDED` trades
    with the highest `net_pnl`. `None` when `trade_count <= TOP_N_EXCLUDED`,
    since no trades remain after exclusion."""


def _profit_factor(gross_profit: float, gross_loss: float, trade_count: int) -> float | None:
    if trade_count == 0:
        return None
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _gross_profit_and_loss(trades: list[ClosedTrade]) -> tuple[float, float]:
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    return gross_profit, gross_loss


def _max_drawdown_pct(trades: list[ClosedTrade], starting_equity: float) -> float | None:
    """Max peak-to-trough percentage decline of the CLOSED-TRADE equity
    curve only -- see `BacktestReport.max_drawdown_pct` for the caveat that
    this excludes intra-trade/intrabar drawdown and is a lower bound on
    true risk, not an exact peak-to-trough measure."""
    if not trades:
        return None
    ordered = sorted(trades, key=lambda t: t.exit_time)
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        if peak > 0:
            drawdown = (peak - equity) / peak
            max_dd = max(max_dd, drawdown)
    return max_dd * 100


def generate_report(trades: list[ClosedTrade], starting_equity: float) -> BacktestReport:
    """Compute the full `BacktestReport` from a raw closed-trade list and the
    starting equity the drawdown curve should begin from."""
    trade_count = len(trades)
    win_count = sum(1 for t in trades if t.net_pnl > 0)
    loss_count = sum(1 for t in trades if t.net_pnl < 0)
    win_rate = win_count / trade_count if trade_count else None

    gross_profit, gross_loss = _gross_profit_and_loss(trades)
    profit_factor = _profit_factor(gross_profit, gross_loss, trade_count)

    total_net_pnl = sum(t.net_pnl for t in trades)
    avg_r_multiple = sum(t.r_multiple for t in trades) / trade_count if trade_count else None

    max_drawdown_pct = _max_drawdown_pct(trades, starting_equity)

    remaining = sorted(trades, key=lambda t: t.net_pnl, reverse=True)[TOP_N_EXCLUDED:]
    if remaining:
        remaining_profit, remaining_loss = _gross_profit_and_loss(remaining)
        profit_factor_excluding_top_5 = _profit_factor(remaining_profit, remaining_loss, len(remaining))
    else:
        profit_factor_excluding_top_5 = None

    return BacktestReport(
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        total_net_pnl=total_net_pnl,
        avg_r_multiple=avg_r_multiple,
        max_drawdown_pct=max_drawdown_pct,
        profit_factor_excluding_top_5=profit_factor_excluding_top_5,
    )


def format_report(report: BacktestReport) -> str:
    """Human-readable summary -- secondary to `BacktestReport` itself, which
    is what the Auditor and other callers consume structurally."""

    def _fmt(value: float | None, suffix: str = "") -> str:
        if value is None:
            return "n/a"
        if value == float("inf"):
            return "inf"
        return f"{value:.2f}{suffix}"

    return (
        f"Trades: {report.trade_count} (win {report.win_count} / loss {report.loss_count})\n"
        f"Win rate: {_fmt(report.win_rate)}\n"
        f"Gross profit: {report.gross_profit:.2f}  Gross loss: {report.gross_loss:.2f}\n"
        f"Profit factor: {_fmt(report.profit_factor)}\n"
        f"Profit factor (excl. top {TOP_N_EXCLUDED}): {_fmt(report.profit_factor_excluding_top_5)}\n"
        f"Total net P&L: {report.total_net_pnl:.2f}\n"
        f"Avg R multiple: {_fmt(report.avg_r_multiple)}\n"
        f"Max drawdown: {_fmt(report.max_drawdown_pct, '%')}"
    )
