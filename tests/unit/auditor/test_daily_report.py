"""Tests for auditor/daily_report.py -- built against a seeded tmp_path
journal DB (Appendix A §5.1)."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.store import journal


def test_build_daily_report_over_seeded_db(tmp_path):
    db_path = tmp_path / "journal.sqlite"
    day = date(2026, 7, 19)

    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1),
        entry_spread_points=12.0, actual_slippage=15.0, broker_ticket=1, db_path=db_path,
    )
    journal.record_closed_trade(
        symbol="XAUUSD", direction="SELL", entry_time=datetime(2026, 7, 19, 11, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 12, 0), exit_price=2405.0,
        exit_reason="stop_loss", lot_size=0.1, gross_pnl=-50.0, cost=1.0, net_pnl=-51.0,
        r_multiple=-1.0, recorded_at=datetime(2026, 7, 19, 12, 0, 1),
        entry_spread_points=10.0, actual_slippage=9.0, broker_ticket=2, db_path=db_path,
    )
    # trade outside the target day -- must not be counted
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 20, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 20, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 20, 10, 0, 1), broker_ticket=3, db_path=db_path,
    )

    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 8, 0), symbol="XAUUSD", block_source="risk_voice",
        reason="spread too wide", db_path=db_path,
    )
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 8, 30), symbol="XAUUSD", block_source="shield",
        reason="RRR below minimum", db_path=db_path,
    )
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 30), symbol="XAUUSD", block_source="borderline_near_threshold",
        reason="near threshold", db_path=db_path,
    )

    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 13, 0), event_type="reconnect",
        details="reconnected after 2 min", db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 14, 0), event_type="order_reject",
        details="requote", db_path=db_path,
    )

    report = build_daily_report(day, db_path=db_path)

    assert report.server_date == day
    assert report.trade_count == 2
    assert report.win_count == 1
    assert report.loss_count == 1
    assert report.net_pnl == 98.0 - 51.0
    assert report.avg_r_multiple == (1.96 + -1.0) / 2
    assert report.blocked_by_source == {
        "risk_voice": 1, "shield": 1, "borderline_near_threshold": 1,
    }
    assert report.blocked_total == 3
    assert report.avg_entry_spread_points == (12.0 + 10.0) / 2
    assert report.avg_actual_slippage == (15.0 + 9.0) / 2
    assert report.sl_trade_count == 1
    assert report.avg_sl_overshoot_r == 0.0  # the seeded stop_loss closed at exactly -1.0R
    assert report.anomaly_counts == {"reconnect": 1, "order_reject": 1}


def test_build_daily_report_with_no_data_returns_zeros_and_nones(tmp_path):
    db_path = tmp_path / "journal.sqlite"
    report = build_daily_report(date(2026, 7, 19), db_path=db_path)

    assert report.trade_count == 0
    assert report.win_count == 0
    assert report.loss_count == 0
    assert report.net_pnl == 0.0
    assert report.avg_r_multiple is None
    assert report.blocked_by_source == {}
    assert report.blocked_total == 0
    assert report.avg_entry_spread_points is None
    assert report.avg_actual_slippage is None
    assert report.sl_trade_count == 0
    assert report.avg_sl_overshoot_r is None
    assert report.anomaly_counts == {}


def test_sl_overshoot_averages_only_stop_loss_exits(tmp_path):
    """Overshoot = -r_multiple - 1.0, averaged over stop_loss exits ONLY --
    a -1.34R gap-through-SL day must surface as +0.34R, and non-SL exits
    (even deeply negative ones like structure_invalidation) must not dilute
    it. Mirrors the 2026-08-04 live finding (VPS journal ids 3/5/8)."""
    db_path = tmp_path / "journal.sqlite"
    day = date(2026, 8, 3)

    common = dict(
        symbol="XAUUSD", entry_price=4050.0, lot_size=0.01,
        entry_spread_points=5.0, actual_slippage=0.5, db_path=db_path,
    )
    journal.record_closed_trade(
        direction="BUY", entry_time=datetime(2026, 8, 3, 9, 0),
        exit_time=datetime(2026, 8, 3, 10, 0), exit_price=4030.0,
        exit_reason="stop_loss", gross_pnl=-39.5, cost=0.1, net_pnl=-39.6,
        r_multiple=-1.34, recorded_at=datetime(2026, 8, 3, 10, 0, 1),
        broker_ticket=11, **common,
    )
    journal.record_closed_trade(
        direction="SELL", entry_time=datetime(2026, 8, 3, 11, 0),
        exit_time=datetime(2026, 8, 3, 12, 0), exit_price=4070.0,
        exit_reason="stop_loss", gross_pnl=-33.1, cost=0.1, net_pnl=-33.2,
        r_multiple=-1.00, recorded_at=datetime(2026, 8, 3, 12, 0, 1),
        broker_ticket=12, **common,
    )
    # non-SL loser: must NOT count toward the overshoot average
    journal.record_closed_trade(
        direction="BUY", entry_time=datetime(2026, 8, 3, 13, 0),
        exit_time=datetime(2026, 8, 3, 14, 0), exit_price=4040.0,
        exit_reason="structure_invalidation", gross_pnl=-36.0, cost=0.1,
        net_pnl=-36.1, r_multiple=-0.94,
        recorded_at=datetime(2026, 8, 3, 14, 0, 1), broker_ticket=13, **common,
    )

    report = build_daily_report(day, db_path=db_path)
    assert report.sl_trade_count == 2
    assert report.avg_sl_overshoot_r == pytest.approx((0.34 + 0.0) / 2)

    text = format_daily_report(report)
    assert "Avg SL overshoot: +0.170R past -1R (2 stop_loss exits)" in text


def test_format_daily_report_shows_na_overshoot_when_no_stop_loss_exit(tmp_path):
    report = build_daily_report(date(2026, 8, 3), db_path=tmp_path / "journal.sqlite")
    assert "Avg SL overshoot: n/a (0 stop_loss exits)" in format_daily_report(report)


def test_format_daily_report_is_human_readable_and_does_not_crash_on_empty_data(tmp_path):
    db_path = tmp_path / "journal.sqlite"
    report = build_daily_report(date(2026, 7, 19), db_path=db_path)
    text = format_daily_report(report)
    assert "2026-07-19" in text
    assert "n/a" in text
