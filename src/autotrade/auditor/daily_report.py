"""The Auditor's daily trade-autopsy report (trading_system_summary_v2.md
Appendix A §5.1: "จำนวนไม้/win/loss/net P&L/ค่าเฉลี่ย R, signal ที่ถูก block
แยกตามเหตุผล, slippage จริง vs ที่คาด/spread เฉลี่ย, เหตุการณ์ผิดปกติ").

Built entirely from `store/journal.py`'s existing read functions
(`get_trades_for_day`, `count_blocked_signals_for_day`,
`get_anomaly_events_for_day`) -- no new journal.py query logic needed.

**"Expected" slippage, per this phase's resolved interpretation:**
`TradeRecord.entry_spread_points` IS the cost model's "expected" figure --
`backtest/cost_model.py`'s minimum-1-spread assumption means the cost model
expects realized slippage to be around 1 spread. This report surfaces the
delta between `avg_entry_spread_points` and `avg_actual_slippage` (in
`format_daily_report`) rather than adding a separate "expected slippage"
field, since `entry_spread_points` already *is* that expectation.

**"Day" = MT5 server day**, same convention as `store/journal.py`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from autotrade.store import journal


@dataclass(frozen=True)
class DailyReport:
    server_date: date
    trade_count: int
    win_count: int
    loss_count: int
    net_pnl: float
    avg_r_multiple: float | None
    """`None` if `trade_count == 0`."""
    blocked_by_source: dict[str, int]
    blocked_total: int
    avg_entry_spread_points: float | None
    """`None` if no trade this day recorded a spread (e.g. old data, or no
    trades at all)."""
    avg_actual_slippage: float | None
    """`None` if no trade this day recorded a slippage figure."""
    sl_trade_count: int
    avg_sl_overshoot_r: float | None
    """Mean of `(-r_multiple - 1.0)` over this day's `stop_loss` exits;
    `None` if the day had no stop_loss exit. Positive = the average SL fill
    realized worse than the intended -1R (gap/slippage past the stop; ~0 =
    fills at the nominal SL price). Derived from the net-based `r_multiple`,
    so commission/swap is included -- can go slightly negative on a
    better-than-SL fill or a swap credit. Added 2026-08-04: live SL exits
    were realizing -1.33R/-1.34R (intrabar slippage, weekend gap) and
    intrabar SL slippage is NOT modeled in the backtest, so this is the
    number to watch as the SL sample grows."""
    anomaly_counts: dict[str, int]


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_daily_report(server_date: date, db_path: Path | None = None) -> DailyReport:
    """Build one day's `DailyReport` from the live trade journal."""
    trades = journal.get_trades_for_day(server_date, db_path=db_path)
    trade_count = len(trades)
    win_count = sum(1 for t in trades if t.net_pnl > 0)
    loss_count = sum(1 for t in trades if t.net_pnl < 0)
    net_pnl = sum(t.net_pnl for t in trades)
    avg_r_multiple = sum(t.r_multiple for t in trades) / trade_count if trade_count else None

    avg_entry_spread_points = _average(
        [t.entry_spread_points for t in trades if t.entry_spread_points is not None]
    )
    avg_actual_slippage = _average(
        [t.actual_slippage for t in trades if t.actual_slippage is not None]
    )

    sl_trades = [t for t in trades if t.exit_reason == "stop_loss"]
    avg_sl_overshoot_r = _average([-t.r_multiple - 1.0 for t in sl_trades])

    blocked_by_source = journal.count_blocked_signals_for_day(server_date, db_path=db_path)
    blocked_total = sum(blocked_by_source.values())

    anomaly_counts: dict[str, int] = {}
    for event in journal.get_anomaly_events_for_day(server_date, db_path=db_path):
        anomaly_counts[event.event_type] = anomaly_counts.get(event.event_type, 0) + 1

    return DailyReport(
        server_date=server_date,
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        net_pnl=net_pnl,
        avg_r_multiple=avg_r_multiple,
        blocked_by_source=blocked_by_source,
        blocked_total=blocked_total,
        avg_entry_spread_points=avg_entry_spread_points,
        avg_actual_slippage=avg_actual_slippage,
        sl_trade_count=len(sl_trades),
        avg_sl_overshoot_r=avg_sl_overshoot_r,
        anomaly_counts=anomaly_counts,
    )


def format_daily_report(report: DailyReport) -> str:
    """Human-readable summary -- secondary to `DailyReport` itself, which is
    what any other caller consumes structurally."""

    def _fmt(value: float | None, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value:.2f}{suffix}"

    lines = [
        f"Daily report -- {report.server_date.isoformat()}",
        f"Trades: {report.trade_count} (win {report.win_count} / loss {report.loss_count})",
        f"Net P&L: {report.net_pnl:.2f}   Avg R multiple: {_fmt(report.avg_r_multiple)}",
        f"Blocked signals: {report.blocked_total} total",
    ]
    if report.blocked_by_source:
        for source, count in sorted(report.blocked_by_source.items()):
            lines.append(f"  {source}: {count}")
    lines.append(
        f"Avg entry spread (expected slippage): {_fmt(report.avg_entry_spread_points)} points"
    )
    lines.append(f"Avg actual slippage: {_fmt(report.avg_actual_slippage)} points")
    if report.avg_entry_spread_points is not None and report.avg_actual_slippage is not None:
        delta = report.avg_actual_slippage - report.avg_entry_spread_points
        lines.append(f"Slippage delta (actual - expected): {delta:+.2f} points")
    if report.avg_sl_overshoot_r is None:
        lines.append("Avg SL overshoot: n/a (0 stop_loss exits)")
    else:
        lines.append(
            f"Avg SL overshoot: {report.avg_sl_overshoot_r:+.3f}R past -1R "
            f"({report.sl_trade_count} stop_loss exit{'s' if report.sl_trade_count != 1 else ''})"
        )
    lines.append(f"Anomalies: {sum(report.anomaly_counts.values())} total")
    if report.anomaly_counts:
        for event_type, count in sorted(report.anomaly_counts.items()):
            lines.append(f"  {event_type}: {count}")
    return "\n".join(lines)
