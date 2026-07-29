"""Tests for dashboard/app.py (Flask test_client, no real server) and
dashboard/views.py's pure logic -- same seeded-tmp_path DB convention as
tests/unit/store/test_journal.py / tests/unit/test_run_auditor.py."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import urlencode

import pandas as pd
import pytest

from autotrade.auditor.daily_report import build_daily_report
from autotrade.dashboard import views
from autotrade.dashboard import app as dashboard_app
from autotrade.dashboard.app import create_app, get_current_server_time
from autotrade.store import journal

WEBAPP_BOT_TOKEN = "123456:test-bot-token"
WEBAPP_CHAT_ID = "8978823598"


def _build_init_data(bot_token, user_id, auth_date=None):
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Op"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _trades_url_with_init_data(init_data):
    # init_data is itself a `key=value&key=value...` query string -- must be
    # embedded as a single query PARAM VALUE (percent-encoded via urlencode),
    # not spliced directly into the URL, or its own `&`s would be parsed as
    # extra top-level params instead of part of the initData value.
    return "/trades?" + urlencode({"initData": init_data})


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


def test_trades_shows_seeded_trade_cost_value(db_path):
    _record_trade(db_path, cost=4.5, broker_ticket=1)
    client = create_app(db_path=db_path).test_client()
    body = client.get("/trades").get_data(as_text=True)

    assert "4.5" in body


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


# --- current_server_time (Feature A) ---------------------------------------


def test_current_server_time_displayed_when_available(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_current_server_time", lambda: datetime(2026, 7, 21, 17, 30))
    client = create_app(db_path=db_path).test_client()

    trades_body = client.get("/trades").get_data(as_text=True)
    daily_body = client.get("/daily").get_data(as_text=True)

    assert "2026-07-21 17:30" in trades_body
    assert "2026-07-21 17:30" in daily_body


def test_current_server_time_unavailable_shows_clear_message_not_error(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_current_server_time", lambda: None)
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades")

    assert resp.status_code == 200
    assert "unavailable" in resp.get_data(as_text=True).lower()


def test_get_current_server_time_returns_none_when_mt5_session_raises(monkeypatch):
    monkeypatch.setattr(dashboard_app, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_app, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    def _raise(creds, **kwargs):
        raise RuntimeError("MT5 terminal not running")

    monkeypatch.setattr(dashboard_app, "mt5_session", _raise)

    assert get_current_server_time() is None


def test_get_current_server_time_passes_a_short_timeout_ms_to_mt5_session(monkeypatch):
    monkeypatch.setattr(dashboard_app, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_app, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})
    captured = {}

    @contextmanager
    def _fake_session(creds, **kwargs):
        captured.update(kwargs)
        yield

    monkeypatch.setattr(dashboard_app, "mt5_session", _fake_session)
    monkeypatch.setattr(dashboard_app, "server_now", lambda symbol: datetime(2026, 7, 21, 17, 30))

    get_current_server_time()

    assert captured.get("timeout_ms") is not None
    assert captured["timeout_ms"] <= 5000


def test_page_still_renders_200_when_mt5_session_raises(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "load_mt5_credentials", lambda: object())
    monkeypatch.setattr(dashboard_app, "load_yaml_config", lambda name: {"symbols": {"XAUUSD": "XAUUSD.a"}})

    def _raise(creds, **kwargs):
        raise RuntimeError("MT5 terminal not running")

    monkeypatch.setattr(dashboard_app, "mt5_session", _raise)

    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades")

    assert resp.status_code == 200
    assert "unavailable" in resp.get_data(as_text=True).lower()


# --- open positions on /trades (Feature B) ---------------------------------
# Low-level get_open_positions_display() MT5-mocking tests now live in
# tests/unit/dashboard/test_positions.py, alongside dashboard/positions.py
# (moved when get_open_positions_display was extracted out of this module so
# notify/telegram_control.py could reuse it without a transitive Flask
# import). The tests below only exercise this module's own concern: how the
# /trades route renders whatever get_open_positions_display() returns.


def test_trades_page_shows_open_positions_when_available(db_path, monkeypatch):
    monkeypatch.setattr(
        dashboard_app, "get_open_positions_display",
        lambda: [
            views.OpenPositionRow(
                ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, price_open=2400.0,
                price_current=2410.0, sl=2390.0, tp=2420.0, profit=10.0,
            ),
        ],
    )
    client = create_app(db_path=db_path).test_client()
    body = client.get("/trades").get_data(as_text=True)

    assert "XAUUSD" in body
    assert "BUY" in body
    assert "2400.0" in body
    assert "2410.0" in body
    assert "10.0" in body
    assert "No open positions" not in body


def test_trades_page_shows_no_open_positions_message_for_empty_list(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_open_positions_display", lambda: [])
    client = create_app(db_path=db_path).test_client()
    body = client.get("/trades").get_data(as_text=True)

    assert "No open positions" in body


def test_trades_page_shows_unavailable_message_and_returns_200_when_positions_unavailable(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_open_positions_display", lambda: None)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades")

    assert resp.status_code == 200
    assert "Open positions unavailable" in resp.get_data(as_text=True)


# --- /trades/export --------------------------------------------------------


def _read_xlsx_rows(tmp_path, response_data, name="export.xlsx"):
    """Round-trips exported bytes through an on-disk tmp file (openpyxl/
    pandas both need a real path or file-like, and writing to tmp_path here
    is a test-only concern -- the route itself never touches disk)."""
    xlsx_path = tmp_path / name
    xlsx_path.write_bytes(response_data)
    return pd.read_excel(xlsx_path).to_dict(orient="records")


def test_trades_export_returns_valid_xlsx_with_seeded_trade_values(db_path, tmp_path):
    _record_trade(
        db_path, symbol="EURUSD", direction="SELL", net_pnl=-12.5,
        exit_reason="stop_loss", broker_ticket=1,
    )
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades/export")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    rows = _read_xlsx_rows(tmp_path, resp.data)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "EURUSD"
    assert rows[0]["direction"] == "SELL"
    assert rows[0]["net_pnl"] == -12.5
    assert rows[0]["exit_reason"] == "stop_loss"
    assert rows[0]["cost"] == 2.0


def test_trades_export_respects_date_range_filter(db_path, tmp_path):
    _record_trade(
        db_path, symbol="EURUSD", exit_time=datetime(2026, 7, 18, 10, 0), broker_ticket=1,
    )
    _record_trade(
        db_path, symbol="GBPUSD", exit_time=datetime(2026, 7, 19, 10, 0), broker_ticket=2,
    )
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades/export?start=2026-07-19&end=2026-07-20")

    rows = _read_xlsx_rows(tmp_path, resp.data)
    symbols = [r["symbol"] for r in rows]
    assert symbols == ["GBPUSD"]


def test_trades_export_empty_db_returns_valid_header_only_xlsx(db_path, tmp_path):
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades/export")

    assert resp.status_code == 200
    rows = _read_xlsx_rows(tmp_path, resp.data)
    assert rows == []


def test_trades_export_malformed_start_param_does_not_500(db_path, tmp_path):
    _record_trade(db_path)
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades/export?start=not-a-date")

    assert resp.status_code == 200
    rows = _read_xlsx_rows(tmp_path, resp.data)
    assert len(rows) == 1


def test_trades_export_end_param_includes_selected_day(db_path, tmp_path):
    _record_trade(
        db_path, symbol="EURUSD", exit_time=datetime(2026, 7, 19, 23, 30), broker_ticket=1,
    )
    client = create_app(db_path=db_path).test_client()
    resp = client.get("/trades/export?end=2026-07-19")

    rows = _read_xlsx_rows(tmp_path, resp.data)
    assert [r["symbol"] for r in rows] == ["EURUSD"]


def test_trades_end_param_includes_selected_day(db_path):
    _record_trade(
        db_path, symbol="EURUSD", exit_time=datetime(2026, 7, 19, 23, 30), broker_ticket=1,
    )
    client = create_app(db_path=db_path).test_client()
    body = client.get("/trades?end=2026-07-19").get_data(as_text=True)

    assert "EURUSD" in body


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

    # Checks the individual values reach the page (not a hardcoded combined
    # string) so this stays a genuine cross-check against build_daily_report
    # regardless of how the template lays them out.
    assert str(expected.trade_count) in body
    assert str(expected.win_count) in body
    assert str(expected.loss_count) in body
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


def test_to_open_position_row_buy_with_positive_pnl():
    data = views.OpenPositionData(
        ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, price_open=2400.0,
        price_current=2410.0, sl=2390.0, tp=2420.0, profit=10.0,
    )
    row = views.to_open_position_row(data)
    assert row == views.OpenPositionRow(
        ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, price_open=2400.0,
        price_current=2410.0, sl=2390.0, tp=2420.0, profit=10.0,
    )


def test_to_open_position_row_sell_with_negative_pnl():
    data = views.OpenPositionData(
        ticket=2, symbol="EURUSD", direction="SELL", volume=0.2, price_open=1.10,
        price_current=1.11, sl=1.12, tp=1.05, profit=-20.0,
    )
    row = views.to_open_position_row(data)
    assert row.direction == "SELL"
    assert row.profit == -20.0


def test_sort_open_positions_sorts_by_ticket_ascending():
    unsorted_rows = [
        views.OpenPositionRow(ticket=3, symbol="A", direction="BUY", volume=0.1, price_open=1.0,
                               price_current=1.0, sl=0.0, tp=0.0, profit=0.0),
        views.OpenPositionRow(ticket=1, symbol="B", direction="SELL", volume=0.1, price_open=1.0,
                               price_current=1.0, sl=0.0, tp=0.0, profit=0.0),
    ]
    sorted_rows = views.sort_open_positions(unsorted_rows)
    assert [r.ticket for r in sorted_rows] == [1, 3]


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


def test_trades_to_export_rows_matches_to_trade_row_field_values(db_path):
    _record_trade(
        db_path, symbol="EURUSD", direction="SELL", net_pnl=-12.5,
        exit_reason="stop_loss", exit_time=datetime(2026, 7, 19, 14, 30), broker_ticket=1,
    )
    _record_trade(
        db_path, symbol="XAUUSD", direction="BUY", net_pnl=98.0,
        exit_reason="take_profit", exit_time=datetime(2026, 7, 20, 9, 0), broker_ticket=2,
    )
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    rows = views.trades_to_export_rows(all_trades)

    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"EURUSD", "XAUUSD"}
    eurusd_row = next(r for r in rows if r["symbol"] == "EURUSD")
    assert eurusd_row["direction"] == "SELL"
    assert eurusd_row["net_pnl"] == -12.5
    assert eurusd_row["exit_reason"] == "stop_loss"
    assert eurusd_row["exit_time"] == "2026-07-19 14:30"
    assert eurusd_row["cost"] == 2.0


def test_to_trade_row_carries_cost_field_from_trade_record(db_path):
    _record_trade(db_path, cost=4.5, broker_ticket=1)
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    row = views.to_trade_row(all_trades[0])

    assert row.cost == 4.5


def test_to_trade_row_carries_broker_ticket(db_path):
    # 2026-07-29 user request: ticket column on the dashboard.
    _record_trade(db_path, broker_ticket=1826927585)
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    row = views.to_trade_row(all_trades[0])

    assert row.ticket == 1826927585


def test_to_trade_row_carries_entry_classification(db_path):
    # 2026-07-30 user request: entry-correctness column on the dashboard.
    _record_trade(db_path, broker_ticket=1, entry_classification="delayed_entry+high_slippage")
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    row = views.to_trade_row(all_trades[0])

    assert row.entry_classification == "delayed_entry+high_slippage"


def test_trades_page_shows_ok_for_normal_entry_and_flags_an_anomalous_one(db_path):
    _record_trade(db_path, broker_ticket=1, entry_classification="normal")
    _record_trade(
        db_path, broker_ticket=2, entry_classification="delayed_entry+high_slippage",
        entry_time=datetime(2026, 7, 19, 11, 0), exit_time=datetime(2026, 7, 19, 12, 0),
    )
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades")

    body = resp.get_data(as_text=True)
    assert ">OK<" in body
    assert "delayed_entry+high_slippage" in body


def test_trades_to_export_rows_empty_list():
    assert views.trades_to_export_rows([]) == []


def test_trades_to_export_rows_escapes_formula_injection_prefixes(db_path):
    _record_trade(db_path, exit_reason="=cmd|'/c calc'!A1", broker_ticket=1)
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    rows = views.trades_to_export_rows(all_trades)

    assert rows[0]["exit_reason"] == "'=cmd|'/c calc'!A1"


def test_trades_to_export_rows_does_not_touch_safe_string_values(db_path):
    _record_trade(db_path, exit_reason="take_profit", symbol="EURUSD", broker_ticket=1)
    all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=db_path)

    rows = views.trades_to_export_rows(all_trades)

    assert rows[0]["exit_reason"] == "take_profit"
    assert rows[0]["symbol"] == "EURUSD"


# --- Telegram Web App auth gate ---------------------------------------------
# Opt-in: only engages when load_telegram_credentials() returns a real pair --
# every test above this section relies on tests/conftest.py's autouse
# _block_real_telegram_notifications fixture blanking TELEGRAM_BOT_TOKEN/
# TELEGRAM_CHAT_ID, so the gate stays off and those 40+ route-behavior tests
# are unaffected by its existence. These tests explicitly configure it.


def _configure_webapp_auth(monkeypatch, bot_token=WEBAPP_BOT_TOKEN, chat_id=WEBAPP_CHAT_ID):
    monkeypatch.setattr(dashboard_app, "load_telegram_credentials", lambda: (bot_token, chat_id))


def test_dashboard_open_without_any_gate_when_telegram_not_configured(db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "load_telegram_credentials", lambda: None)
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades")

    assert resp.status_code == 200


def test_route_returns_401_with_no_session_and_no_init_data(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades")

    assert resp.status_code == 401


def test_unauthorized_response_includes_the_telegram_web_app_sdk_script(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades")

    assert b"telegram-web-app.js" in resp.data


def test_valid_init_data_grants_access_and_establishes_a_session(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=int(WEBAPP_CHAT_ID))
    client = create_app(db_path=db_path).test_client()

    first = client.get(_trades_url_with_init_data(init_data))
    second = client.get("/trades")  # no initData this time -- relies on the session cookie

    assert first.status_code == 200
    assert second.status_code == 200


def test_init_data_can_also_be_sent_as_a_header(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=int(WEBAPP_CHAT_ID))
    client = create_app(db_path=db_path).test_client()

    resp = client.get("/trades", headers={"X-Telegram-Init-Data": init_data})

    assert resp.status_code == 200


def test_tampered_init_data_is_rejected(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=int(WEBAPP_CHAT_ID))
    tampered = init_data.replace(WEBAPP_CHAT_ID, "111111111")
    client = create_app(db_path=db_path).test_client()

    resp = client.get(_trades_url_with_init_data(tampered))

    assert resp.status_code == 401


def test_init_data_for_a_non_operator_user_is_rejected(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=999999999)
    client = create_app(db_path=db_path).test_client()

    resp = client.get(_trades_url_with_init_data(init_data))

    assert resp.status_code == 401


def test_expired_init_data_is_rejected(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    stale_auth_date = time.time() - (dashboard_app.webapp_auth.MAX_INIT_DATA_AGE_SECONDS + 60)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=int(WEBAPP_CHAT_ID), auth_date=stale_auth_date)
    client = create_app(db_path=db_path).test_client()

    resp = client.get(_trades_url_with_init_data(init_data))

    assert resp.status_code == 401


def test_established_session_grants_access_to_every_route(db_path, monkeypatch):
    _configure_webapp_auth(monkeypatch)
    init_data = _build_init_data(WEBAPP_BOT_TOKEN, user_id=int(WEBAPP_CHAT_ID))
    client = create_app(db_path=db_path).test_client()
    client.get(_trades_url_with_init_data(init_data))

    assert client.get("/daily").status_code == 200
    assert client.get("/trades/export").status_code == 200
