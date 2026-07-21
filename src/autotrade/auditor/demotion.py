"""The Auditor's demotion rules (trading_system_summary_v2.md Appendix A
§5.3 "เกณฑ์ถอดถอน"): when a LIVE strategy should be reverted to paper
trading, or halted for manual investigation.

**Two distinct "recent window" concepts, deliberately not unified:**
- "2 consecutive months of net loss" uses **calendar months** (Jan, Feb,
  ... as humans read a calendar) -- `_recent_calendar_months` buckets
  `exit_time.date()` by `(year, month)`.
- "Profit factor rolling 60 days < 1.0" is a **rolling server-day window**
  (the last N calendar days ending on `as_of`, not month-aligned at all).
These are separately specified in Appendix A §5.3's own bullet list and must
not be collapsed into one "recent period" concept -- a strategy could fail
one without failing the other (e.g. a bad final week of an otherwise-okay
month still trips the rolling-60-day rule without yet completing 2 losing
calendar months).

**Demotion precedence (this phase's resolved decision, not explicit in the
spec text):** if a revert-to-paper condition (calendar-months loss or
rolling-PF) and the halt-and-investigate condition (win-rate divergence)
both match in the same evaluation, `halt_and_investigate` wins -- it is the
more conservative action (a human must look before ANY further live
trading, whereas revert-to-paper still trades, just not with real money).
`DemotionResult.reasons` surfaces every matched reason from BOTH conditions
regardless of which action wins, so a human reviewing the result sees the
full picture, not just the winning branch's reasons.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Sequence

from autotrade.auditor.metrics import TradeLike, compute_trade_metrics
from autotrade.backtest.report import BacktestReport


@dataclass(frozen=True)
class DemotionThresholds:
    consecutive_loss_months: int = 2
    rolling_pf_window_days: int = 60
    rolling_pf_min: float = 1.0
    win_rate_divergence_pct_points: float = 15.0
    win_rate_divergence_min_trades: int = 50


@dataclass(frozen=True)
class DemotionResult:
    action: Literal["none", "revert_to_paper", "halt_and_investigate"]
    reasons: list[str]


def _recent_calendar_months(as_of: date, n: int) -> list[tuple[int, int]]:
    """The `n` most recent calendar months ending with (and including)
    `as_of`'s own month, oldest first."""
    months = []
    year, month = as_of.year, as_of.month
    for _ in range(n):
        months.append((year, month))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(months))


def _check_consecutive_loss_months(
    live_records: Sequence[TradeLike], as_of: date, thresholds: DemotionThresholds,
) -> str | None:
    months = _recent_calendar_months(as_of, thresholds.consecutive_loss_months)
    pnl_by_month: dict[tuple[int, int], float] = {}
    counts_by_month: dict[tuple[int, int], int] = {}
    for record in live_records:
        key = (record.exit_time.year, record.exit_time.month)
        if key in months:
            pnl_by_month[key] = pnl_by_month.get(key, 0.0) + record.net_pnl
            counts_by_month[key] = counts_by_month.get(key, 0) + 1

    if not all(counts_by_month.get(m, 0) > 0 for m in months):
        return None  # insufficient data for at least one of the months -- not a trigger

    if all(pnl_by_month[m] < 0 for m in months):
        month_strs = ", ".join(f"{y}-{mo:02d}" for y, mo in months)
        return f"{thresholds.consecutive_loss_months} consecutive calendar months of net loss ({month_strs})"
    return None


def _check_rolling_profit_factor(
    live_records: Sequence[TradeLike], as_of: date, thresholds: DemotionThresholds,
) -> str | None:
    # `[as_of - (N-1), as_of]` inclusive is exactly N calendar days --
    # `as_of - N` would make it N+1 days (both endpoints inclusive).
    window_start = datetime.combine(
        as_of - timedelta(days=thresholds.rolling_pf_window_days - 1), datetime.min.time(),
    )
    window_end = datetime.combine(as_of + timedelta(days=1), datetime.min.time())
    windowed = [r for r in live_records if window_start <= r.exit_time < window_end]

    metrics = compute_trade_metrics(windowed, starting_equity=0.0)
    if metrics.profit_factor is None:
        return None  # insufficient data in the window -- not a trigger
    if metrics.profit_factor < thresholds.rolling_pf_min:
        return (
            f"rolling {thresholds.rolling_pf_window_days}-server-day profit factor "
            f"{metrics.profit_factor:.2f} < {thresholds.rolling_pf_min}"
        )
    return None


def _check_win_rate_divergence(
    live_records: Sequence[TradeLike], backtest_report: BacktestReport, thresholds: DemotionThresholds,
) -> str | None:
    if len(live_records) < thresholds.win_rate_divergence_min_trades:
        return None  # insufficient sample -- not a trigger

    metrics = compute_trade_metrics(live_records, starting_equity=0.0)
    if metrics.win_rate is None or backtest_report.win_rate is None:
        return None

    divergence_pct_points = abs(metrics.win_rate - backtest_report.win_rate) * 100
    if divergence_pct_points > thresholds.win_rate_divergence_pct_points:
        return (
            f"live win rate diverges from backtest by {divergence_pct_points:.1f} percentage points "
            f"(> {thresholds.win_rate_divergence_pct_points}) at {len(live_records)} trades "
            f"(>= {thresholds.win_rate_divergence_min_trades} minimum sample)"
        )
    return None


def evaluate_demotion(
    live_records: Sequence[TradeLike],
    backtest_report: BacktestReport,
    as_of: date,
    thresholds: DemotionThresholds,
) -> DemotionResult:
    """Evaluate every §5.3 demotion rule as of `as_of` (server date). Returns
    `action="none"` when no rule fires (including simply "not enough data
    yet" for every rule -- the honest common case early in a live track
    record)."""
    revert_reasons = [
        reason for reason in (
            _check_consecutive_loss_months(live_records, as_of, thresholds),
            _check_rolling_profit_factor(live_records, as_of, thresholds),
        ) if reason is not None
    ]
    halt_reasons = [
        reason for reason in (
            _check_win_rate_divergence(live_records, backtest_report, thresholds),
        ) if reason is not None
    ]

    if halt_reasons:
        return DemotionResult(action="halt_and_investigate", reasons=[*halt_reasons, *revert_reasons])
    if revert_reasons:
        return DemotionResult(action="revert_to_paper", reasons=revert_reasons)
    return DemotionResult(action="none", reasons=[])
