"""Generic trade-performance arithmetic (profit factor, max drawdown, win
rate, avg R, profit-factor-excluding-top-5) shared by both the live trade
journal (`store.models.TradeRecord`) and the backtest engine
(`backtest.engine.ClosedTrade`) -- Appendix A §5.2's promotion gates and
§5.1's daily report both need these same numbers computed from whichever
trade-record shape they're looking at, via the `TradeLike` structural
Protocol both shapes already satisfy (`net_pnl`, `r_multiple`, `exit_time`).

Deliberately does NOT delegate to/from `backtest/report.py`: that module's
arithmetic already exists and is tested for the backtest-only
`BacktestReport` consumer, and its docstring explicitly says the Auditor
(this package) is the one that applies gate thresholds to its numbers, not
the one that computes them differently. This module's formulas are a small,
deliberate duplication of `backtest/report.py`'s -- see
`tests/unit/auditor/test_metrics.py` for the cross-check that both produce
identical numbers over equivalent inputs -- so a future change to one does
not have to reason about the other's callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

TOP_N_EXCLUDED = 5


class TradeLike(Protocol):
    net_pnl: float
    r_multiple: float
    exit_time: datetime


@dataclass(frozen=True)
class TradeMetrics:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float | None
    """`None` if `trade_count == 0`."""
    gross_profit: float
    gross_loss: float
    """Non-negative magnitude -- see `_gross_profit_and_loss`."""
    profit_factor: float | None
    """`gross_profit / gross_loss`. `None` if there are zero trades. `inf` if
    there are wins and zero losses. `0.0` if there are zero wins."""
    profit_factor_excluding_top_5: float | None
    """`profit_factor` recomputed after removing the `TOP_N_EXCLUDED` trades
    with the highest `net_pnl`. `None` when `trade_count <= TOP_N_EXCLUDED`."""
    max_drawdown_pct: float | None
    """Max peak-to-trough percentage decline of the CLOSED-TRADE equity
    curve only (see `backtest/report.py`'s `max_drawdown_pct` for the same
    caveat: this excludes intra-trade/intrabar drawdown). `None` if
    `trade_count == 0`."""
    avg_r_multiple: float | None
    """`None` if `trade_count == 0`."""
    total_net_pnl: float


def _profit_factor(gross_profit: float, gross_loss: float, trade_count: int) -> float | None:
    if trade_count == 0:
        return None
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _gross_profit_and_loss(trades: Sequence[TradeLike]) -> tuple[float, float]:
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    return gross_profit, gross_loss


def _max_drawdown_pct(trades: Sequence[TradeLike], starting_equity: float) -> float | None:
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


def compute_trade_metrics(trades: Sequence[TradeLike], starting_equity: float) -> TradeMetrics:
    """Compute the full `TradeMetrics` from a trade sequence (either
    `list[store.models.TradeRecord]` or `list[backtest.engine.ClosedTrade]`)
    and the starting equity the drawdown curve should begin from."""
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

    return TradeMetrics(
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        profit_factor_excluding_top_5=profit_factor_excluding_top_5,
        max_drawdown_pct=max_drawdown_pct,
        avg_r_multiple=avg_r_multiple,
        total_net_pnl=total_net_pnl,
    )
