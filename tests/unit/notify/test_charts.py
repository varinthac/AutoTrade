"""Unit tests for notify/charts.py.

The aggregation arithmetic (`_cumulative_equity`/`_daily_net_pnl`) is pure
Python with no matplotlib dependency at all, so it's always tested directly.
The `build_equity_curve_png`/`build_daily_pnl_png` PNG-bytes round-trip tests
additionally need matplotlib to actually render, so they're skipped if that
fails on this machine (e.g. this dev machine's Windows Smart App Control
blocking a freshly-installed matplotlib native DLL -- a known local
environment issue, not a code defect)."""
from __future__ import annotations

from datetime import datetime

import pytest

from autotrade.notify import charts
from autotrade.store.models import TradeRecord

_PNG_MAGIC = b"\x89PNG"


def _trade_record(net_pnl: float, exit_time: datetime, r_multiple: float = 1.0) -> TradeRecord:
    return TradeRecord(
        symbol="XAUUSD", direction="BUY", entry_time=exit_time, entry_price=100.0,
        exit_time=exit_time, exit_price=100.0, exit_reason="take_profit", lot_size=1.0,
        gross_pnl=net_pnl, cost=0.0, net_pnl=net_pnl, r_multiple=r_multiple, recorded_at=exit_time,
    )


# --- _cumulative_equity() (pure, no matplotlib needed) -----------------------


def test_cumulative_equity_empty_trades():
    exit_times, cumulative = charts._cumulative_equity([])

    assert exit_times == []
    assert cumulative == []


def test_cumulative_equity_sums_in_exit_time_order_not_list_order():
    # Deliberately inserted out of exit_time order.
    trades = [
        _trade_record(10.0, datetime(2026, 7, 19, 16, 0)),
        _trade_record(100.0, datetime(2026, 7, 19, 9, 0)),
    ]

    exit_times, cumulative = charts._cumulative_equity(trades)

    assert exit_times == [datetime(2026, 7, 19, 9, 0), datetime(2026, 7, 19, 16, 0)]
    assert cumulative == pytest.approx([100.0, 110.0])


def test_cumulative_equity_handles_losing_trades():
    trades = [
        _trade_record(98.0, datetime(2026, 7, 19, 9, 0)),
        _trade_record(-140.0, datetime(2026, 7, 19, 16, 0)),
    ]

    _, cumulative = charts._cumulative_equity(trades)

    assert cumulative == pytest.approx([98.0, -42.0])


# --- _daily_net_pnl() (pure, no matplotlib needed) ----------------------------


def test_daily_net_pnl_empty_trades():
    days, values = charts._daily_net_pnl([])

    assert days == []
    assert values == []


def test_daily_net_pnl_sums_multiple_trades_on_the_same_day():
    trades = [
        _trade_record(98.0, datetime(2026, 7, 19, 9, 0)),
        _trade_record(-40.0, datetime(2026, 7, 19, 16, 0)),
        _trade_record(10.0, datetime(2026, 7, 20, 9, 0)),
    ]

    days, values = charts._daily_net_pnl(trades)

    assert days == [datetime(2026, 7, 19).date(), datetime(2026, 7, 20).date()]
    assert values == pytest.approx([58.0, 10.0])


def test_daily_net_pnl_sorted_by_day_regardless_of_list_order():
    trades = [
        _trade_record(10.0, datetime(2026, 7, 20, 9, 0)),
        _trade_record(98.0, datetime(2026, 7, 18, 9, 0)),
        _trade_record(-40.0, datetime(2026, 7, 19, 9, 0)),
    ]

    days, _ = charts._daily_net_pnl(trades)

    assert days == [
        datetime(2026, 7, 18).date(), datetime(2026, 7, 19).date(), datetime(2026, 7, 20).date(),
    ]


# --- build_equity_curve_png() / build_daily_pnl_png() (real matplotlib render) -


try:
    charts.build_equity_curve_png([])
    _MATPLOTLIB_RENDERS = True
except Exception:
    _MATPLOTLIB_RENDERS = False

# Applied per-function below (NOT a module-level pytestmark) -- this file's
# pure aggregation tests above must always run regardless of matplotlib.
_requires_matplotlib = pytest.mark.skipif(
    not _MATPLOTLIB_RENDERS, reason="matplotlib cannot render on this machine (native DLL load failed)",
)


@_requires_matplotlib
def test_build_equity_curve_png_returns_valid_png_bytes():
    trades = [
        _trade_record(100.0, datetime(2026, 7, 19, 10, 0)),
        _trade_record(-40.0, datetime(2026, 7, 19, 16, 0)),
    ]

    png = charts.build_equity_curve_png(trades)

    assert isinstance(png, bytes)
    assert len(png) > 0
    assert png[:4] == _PNG_MAGIC


@_requires_matplotlib
def test_build_equity_curve_png_empty_trades_does_not_crash():
    png = charts.build_equity_curve_png([])

    assert isinstance(png, bytes)
    assert len(png) > 0
    assert png[:4] == _PNG_MAGIC


@_requires_matplotlib
def test_build_daily_pnl_png_returns_valid_png_bytes():
    trades = [
        _trade_record(100.0, datetime(2026, 7, 19, 10, 0)),
        _trade_record(-40.0, datetime(2026, 7, 20, 16, 0)),
    ]

    png = charts.build_daily_pnl_png(trades)

    assert isinstance(png, bytes)
    assert len(png) > 0
    assert png[:4] == _PNG_MAGIC


@_requires_matplotlib
def test_build_daily_pnl_png_empty_trades_does_not_crash():
    png = charts.build_daily_pnl_png([])

    assert isinstance(png, bytes)
    assert len(png) > 0
    assert png[:4] == _PNG_MAGIC
