"""Tests for dashboard/app.py (Flask test_client, no real server) and
dashboard/views.py's pure logic -- same seeded-tmp_path DB convention as
tests/unit/store/test_journal.py / tests/unit/test_run_auditor.py."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from autotrade.auditor.daily_report import build_daily_report
from autotrade.dashboard import views
from autotrade.dashboard.app import create_app
from autotrade.store import journal


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "paper.sqlite"


def _record_trade(db_path, **overrides):
    kwargs = dict(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), db_path=db_path,
    )
    kwargs.update(overrides)
    journal.record_closed_trade(**kwargs)


# --- /trades --------------------------------------------------------------


def test_trades_empty_state(db_path):
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades")
    assert resp.status_code == 200
    assert b"No trades recorded yet" in resp.data


def test_trades_shows_seeded_trade_field_values(db_path):
    _record_trade(
        db_path, symbol="EURUSD", direction="SELL", net_pnl=-12.5,
        exit_reason="stop_loss", exit_time=datetime(2026, 7, 19, 14, 30), broker_ticket=1,
    )
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "EURUSD" in body
    assert "SELL" in body
    assert "-12.5" in body
    assert "stop_loss" in body
    assert "2026-07-19 14:30" in body


def test_trades_newest_first_ordering(db_path):
    _record_trade(
        db_path, symbol="EURUSD", exit_time=datetime(2026, 7, 19, 10, 0), broker_ticket=1,
    )
    _record_trade(
        db_path, symbol="GBPUSD", exit_time=datetime(2026, 7, 19, 15, 0), broker_ticket=2,
    )
    client = create_app(db_path=db_path).test_client()
    body = client.get("/trades").get_data(as_text=True)

    assert body.index("GBPUSD") < body.index("EURUSD")


def test_trades_and_daily_show_server_time_banner(db_path):
    client = create_app(db_path=db_path).test_client()

    trades_body = client.get("/trades").get_data(as_text=True)
    daily_body = client.get("/daily").get_data(as_text=True)

    banner = "All times are MT5 broker SERVER time, not your local time."
    assert banner in trades_body
    assert banner in daily_body


# --- /daily -----------------------------------------------------------


def test_daily_empty_db_shows_empty_state_not_error(db_path):
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/daily")
    assert resp.status_code == 200
    assert b"No trades recorded yet" in resp.data


def test_daily_cross_checks_against_build_daily_report_directly(db_path):
    day = date(2026, 7, 19)
    _record_trade(
        db_path, symbol="XAUUSD", exit_time=datetime(2026, 7, 19, 10, 0), net_pnl=98.0, broker_ticket=1,
    )
    _record_trade(
        db_path, symbol="XAUUSD", exit_time=datetime(2026, 7, 19, 16, 0), net_pnl=-40.0,
        exit_reason="stop_loss", broker_ticket=2,
    )

    expected = build_daily_report(day, db_path=db_path)

    client = create_app(db_path=db_path).test_client()
    body = client.get(f"/daily?date={day.isoformat()}").get_data(as_text=True)

    assert f"Trades: {expected.trade_count} (win {expected.win_count} / loss {expected.loss_count})" in body
    assert str(expected.net_pnl) in body


def test_daily_defaults_to_most_recent_trade_day(db_path):
    _record_trade(
        db_path, exit_time=datetime(2026, 7, 18, 9, 0), broker_ticket=1,
    )
    _record_trade(
        db_path, exit_time=datetime(2026, 7, 19, 9, 0), broker_ticket=2,
    )
    client = create_app(db_path=db_path).test_client()
    body = client.get("/daily").get_data(as_text=True)

    assert "2026-07-19" in body
    assert "showing the most recent recorded day" in body


# --- / ----------------------------------------------------------------


def test_index_redirects_to_trades(db_path):
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/")
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["Location"].endswith("/trades")


# --- Malformed query params must degrade gracefully, never 500 ------------


def test_trades_malformed_start_param_does_not_500(db_path):
    _record_trade(db_path)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades?start=not-a-date")
    assert resp.status_code == 200


def test_trades_malformed_end_param_does_not_500(db_path):
    _record_trade(db_path)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades?end=2026-13-99")
    assert resp.status_code == 200


def test_daily_malformed_date_param_does_not_500(db_path):
    _record_trade(db_path)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/daily?date=not-a-date")
    assert resp.status_code == 200


def test_trades_negative_page_clamped_not_shown_raw(db_path):
    _record_trade(db_path)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades?page=-5")
    assert resp.status_code == 200
    assert b"Page -5" not in resp.data
    assert b"Page 1" in resp.data


# --- views.py pure logic (no Flask test client needed) --------------------


class _FakeTrade:
    def __init__(self, exit_time):
        self.exit_time = exit_time


def test_newest_first_reverses_ascending_order():
    trades = [_FakeTrade(datetime(2026, 7, 19, 9, 0)), _FakeTrade(datetime(2026, 7, 19, 15, 0))]
    reversed_trades = views.newest_first(trades)
    assert reversed_trades[0].exit_time == datetime(2026, 7, 19, 15, 0)
    assert reversed_trades[1].exit_time == datetime(2026, 7, 19, 9, 0)


def test_paginate_slices_by_page():
    items = list(range(1, 121))
    assert views.paginate(items, page=1, per_page=50) == items[0:50]
    assert views.paginate(items, page=2, per_page=50) == items[50:100]
    assert views.paginate(items, page=3, per_page=50) == items[100:120]


def test_paginate_clamps_below_page_one():
    items = list(range(1, 11))
    assert views.paginate(items, page=0, per_page=5) == items[0:5]


def test_parse_date_param_none_and_valid():
    assert views.parse_date_param(None) is None
    assert views.parse_date_param("") is None
    assert views.parse_date_param("2026-07-19") == datetime(2026, 7, 19)


def test_default_daily_date_picks_latest_exit_time():
    trades = [_FakeTrade(datetime(2026, 7, 18, 9, 0)), _FakeTrade(datetime(2026, 7, 19, 15, 0))]
    assert views.default_daily_date(trades) == date(2026, 7, 19)


def test_default_daily_date_none_when_no_trades():
    assert views.default_daily_date([]) is None
