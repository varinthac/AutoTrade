"""Pure chart-rendering helpers for Telegram chart delivery (Phase 1 of the
Telegram bot UX upgrade -- charts now, inline keyboards/Web App dashboard are
later, separate phases). No network I/O, no MT5, no wall-clock (`Clock`/
`datetime.now()`) -- every input is an already-fetched `list[TradeRecord]`,
same "pure logic separate from the network/IO boundary" split
`notify/telegram_control.py`/`dashboard/views.py` already use.

`matplotlib`/`pyplot` are imported lazily (see `_get_pyplot`), not at module
level -- this module is imported eagerly by notify/telegram_control.py and
scripts/run_auditor.py, both of which handle far more than just chart
commands (e.g. /start, /status, /trades), so a matplotlib import/native-DLL
problem on a given machine must only break the /daily chart path, not every
other command those modules dispatch. `matplotlib.use("Agg")` is forced
before importing `pyplot` -- this process has no display server, and
`pyplot` picks (and may fail trying to initialize) a GUI backend if a
non-interactive one isn't selected first.
"""
from __future__ import annotations

import io
from datetime import date

from autotrade.store.models import TradeRecord

_FIGURE_SIZE = (8, 4.5)
_DPI = 100

_pyplot = None


def _get_pyplot():
    global _pyplot
    if _pyplot is None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _pyplot = plt
    return _pyplot


def _render_to_png(fig) -> bytes:
    plt = _get_pyplot()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI)
    plt.close(fig)
    return buf.getvalue()


def _cumulative_equity(trades: list[TradeRecord]) -> tuple[list, list[float]]:
    """`(exit_times, cumulative_net_pnl)`, both ordered by `exit_time` --
    split out from `build_equity_curve_png` so the aggregation arithmetic is
    testable on its own, without needing matplotlib to actually render."""
    ordered = sorted(trades, key=lambda t: t.exit_time)
    cumulative = []
    running = 0.0
    for trade in ordered:
        running += trade.net_pnl
        cumulative.append(running)
    return [t.exit_time for t in ordered], cumulative


def _daily_net_pnl(trades: list[TradeRecord]) -> tuple[list[date], list[float]]:
    """`(days, net_pnl_per_day)`, sorted by day -- SERVER time bucketing
    (`exit_time.date()`, never UTC/local, same convention as
    `store/journal.py`), split out for the same testability reason as
    `_cumulative_equity`."""
    daily: dict[date, float] = {}
    for trade in trades:
        day = trade.exit_time.date()
        daily[day] = daily.get(day, 0.0) + trade.net_pnl
    days = sorted(daily)
    return days, [daily[day] for day in days]


def build_equity_curve_png(trades: list[TradeRecord]) -> bytes:
    """Cumulative `net_pnl` ordered by `exit_time`, one point per closed
    trade (not per calendar day -- see `build_daily_pnl_png` for the
    day-bucketed view). An empty `trades` list produces a valid but empty
    chart rather than raising -- same "don't crash on no data" posture as
    `_handle_trades`'s "No trades recorded yet." text reply, just at the
    image layer instead of the text layer."""
    exit_times, cumulative = _cumulative_equity(trades)

    plt = _get_pyplot()
    fig, ax = plt.subplots(figsize=_FIGURE_SIZE)
    if exit_times:
        ax.plot(exit_times, cumulative, marker="o", markersize=3)
        fig.autofmt_xdate()
    ax.set_title("Equity Curve (Cumulative Net P/L)")
    ax.set_xlabel("Exit time (server)")
    ax.set_ylabel("Cumulative net P/L")
    ax.grid(True, alpha=0.3)
    return _render_to_png(fig)


def build_daily_pnl_png(trades: list[TradeRecord]) -> bytes:
    """Net `net_pnl` summed per calendar day of `exit_time` -- SERVER time,
    same day-bucketing convention as `store/journal.py` (never UTC/local).
    An empty `trades` list produces a valid but empty chart rather than
    raising, same as `build_equity_curve_png`."""
    days, values = _daily_net_pnl(trades)

    plt = _get_pyplot()
    fig, ax = plt.subplots(figsize=_FIGURE_SIZE)
    if days:
        colors = ["tab:green" if v >= 0 else "tab:red" for v in values]
        ax.bar(days, values, color=colors)
        fig.autofmt_xdate()
    ax.set_title("Daily Net P/L")
    ax.set_xlabel("Server date")
    ax.set_ylabel("Net P/L")
    ax.grid(True, alpha=0.3, axis="y")
    return _render_to_png(fig)
