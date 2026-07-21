"""Tests for store/journal.py -- write/read round-trips and the MT5
server-day boundary convention (Appendix A §0)."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from autotrade.store import journal
from autotrade.store.models import get_engine


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "trade_journal.sqlite"


def test_wal_mode_enabled(db_path):
    engine = get_engine(db_path)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_record_and_query_closed_trade_round_trips_every_field(db_path):
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 10, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 12, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 12, 0, 1),
        entry_spread_points=12.0, actual_slippage=0.3, broker_ticket=555, db_path=db_path,
    )

    trades = journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.symbol == "XAUUSD"
    assert trade.direction == "BUY"
    assert trade.entry_time == datetime(2026, 7, 19, 10, 0)
    assert trade.entry_price == 2400.0
    assert trade.exit_time == datetime(2026, 7, 19, 12, 0)
    assert trade.exit_price == 2410.0
    assert trade.exit_reason == "take_profit"
    assert trade.lot_size == 0.1
    assert trade.gross_pnl == 100.0
    assert trade.cost == 2.0
    assert trade.net_pnl == 98.0
    assert trade.r_multiple == 1.96
    assert trade.entry_spread_points == 12.0
    assert trade.actual_slippage == 0.3
    assert trade.broker_ticket == 555


def test_record_closed_trade_returns_true_on_genuine_insert(db_path):
    result = journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 10, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 12, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 12, 0, 1), broker_ticket=555, db_path=db_path,
    )

    assert result is True


def test_record_closed_trade_returns_false_on_swallowed_duplicate_ticket(db_path):
    kwargs = dict(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 10, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 12, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 12, 0, 1), broker_ticket=555, db_path=db_path,
    )
    first = journal.record_closed_trade(**kwargs)
    second = journal.record_closed_trade(**kwargs)  # same broker_ticket -- hits the UNIQUE constraint

    assert first is True
    assert second is False
    assert len(journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path)) == 1


def test_record_and_query_blocked_signal_round_trips(db_path):
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 0), symbol="XAUUSD", block_source="shield",
        reason="RRR below minimum", direction="SELL", db_path=db_path,
    )

    counts = journal.count_blocked_signals_for_day(date(2026, 7, 19), db_path=db_path)

    assert counts == {"shield": 1}


def test_record_and_query_anomaly_event_round_trips(db_path):
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 9, 30), event_type="reconnect",
        details="MT5 connectivity lost for 6.0 minutes", db_path=db_path,
    )

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19), db_path=db_path)

    assert len(events) == 1
    assert events[0].event_type == "reconnect"
    assert events[0].details == "MT5 connectivity lost for 6.0 minutes"


def test_record_anomaly_event_notifies_once_with_event_details(db_path, monkeypatch):
    calls = []
    monkeypatch.setattr(journal, "notify", lambda text: calls.append(text))

    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 9, 30), event_type="reconnect",
        details="MT5 connectivity lost for 6.0 minutes", db_path=db_path,
    )

    assert len(calls) == 1
    assert "reconnect" in calls[0]
    assert "MT5 connectivity lost for 6.0 minutes" in calls[0]
    assert "2026-07-19T09:30:00" in calls[0]


def test_record_anomaly_event_still_persists_when_notify_raises(db_path, monkeypatch):
    def _boom(text):
        raise RuntimeError("simulated notify failure")

    monkeypatch.setattr(journal, "notify", _boom)

    with pytest.raises(RuntimeError):
        journal.record_anomaly_event(
            timestamp=datetime(2026, 7, 19, 9, 30), event_type="reconnect",
            details="still recorded before notify runs", db_path=db_path,
        )

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19), db_path=db_path)
    assert len(events) == 1  # the DB write already committed before notify() ran


def test_count_blocked_signals_groups_by_block_source(db_path):
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 0), symbol="XAUUSD", block_source="risk_voice",
        reason="spread too wide", db_path=db_path,
    )
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 5), symbol="XAUUSD", block_source="risk_voice",
        reason="news blackout", db_path=db_path,
    )
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 10), symbol="EURUSD", block_source="shield",
        reason="RRR below minimum", db_path=db_path,
    )
    journal.record_blocked_signal(
        timestamp=datetime(2026, 7, 19, 9, 15), symbol="XAUUSD",
        block_source="borderline_near_threshold", reason="near-threshold", db_path=db_path,
    )

    counts = journal.count_blocked_signals_for_day(date(2026, 7, 19), db_path=db_path)

    assert counts == {"risk_voice": 2, "shield": 1, "borderline_near_threshold": 1}


# --- server-day boundary -----------------------------------------------------


def test_trade_at_exact_day_start_belongs_to_that_day(db_path):
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 18, 23, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 0, 0, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=0.0, net_pnl=100.0,
        r_multiple=2.0, recorded_at=datetime(2026, 7, 19, 0, 0, 0), db_path=db_path,
    )

    assert len(journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path)) == 1
    assert len(journal.get_trades_for_day(date(2026, 7, 18), db_path=db_path)) == 0


def test_trade_one_microsecond_before_day_end_belongs_to_that_day_not_the_next(db_path):
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 10, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 23, 59, 59, 999999), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=0.0, net_pnl=100.0,
        r_multiple=2.0, recorded_at=datetime(2026, 7, 19, 23, 59, 59, 999999), db_path=db_path,
    )

    assert len(journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path)) == 1
    assert len(journal.get_trades_for_day(date(2026, 7, 20), db_path=db_path)) == 0


def test_trades_bucketed_by_exit_time_not_entry_time(db_path):
    # Opened late on the 19th, closed early on the 20th -- Appendix A §5.1's
    # daily report is about what CLOSED that day, so this belongs to the
    # 20th, not the 19th.
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 23, 30),
        entry_price=2400.0, exit_time=datetime(2026, 7, 20, 0, 30), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=0.0, net_pnl=100.0,
        r_multiple=2.0, recorded_at=datetime(2026, 7, 20, 0, 30), db_path=db_path,
    )

    assert len(journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path)) == 0
    assert len(journal.get_trades_for_day(date(2026, 7, 20), db_path=db_path)) == 1


def test_get_trades_in_range_spans_multiple_days(db_path):
    for day in (18, 19, 20, 21):
        journal.record_closed_trade(
            symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, day, 10, 0),
            entry_price=2400.0, exit_time=datetime(2026, 7, day, 12, 0), exit_price=2410.0,
            exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=0.0, net_pnl=100.0,
            r_multiple=2.0, recorded_at=datetime(2026, 7, day, 12, 0), db_path=db_path,
        )

    trades = journal.get_trades_in_range(
        datetime(2026, 7, 19), datetime(2026, 7, 21), db_path=db_path,
    )

    assert [t.entry_time.day for t in trades] == [19, 20]  # half-open [start, end)


def test_get_trades_for_day_with_no_trades_returns_empty_list(db_path):
    assert journal.get_trades_for_day(date(2026, 7, 19), db_path=db_path) == []


def test_get_anomaly_events_for_day_ordered_by_timestamp(db_path):
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 15, 0), event_type="order_reject",
        details="second", db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 9, 0), event_type="reconnect",
        details="first", db_path=db_path,
    )

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19), db_path=db_path)

    assert [e.details for e in events] == ["first", "second"]


def test_get_anomaly_events_for_day_includes_midnight_start_excludes_midnight_end(db_path):
    # Proves get_anomaly_events_for_day's [day 00:00, day+1 00:00) half-open
    # boundary is unchanged now that it delegates to
    # get_anomaly_events_in_range: an event exactly at the day's own
    # midnight must be included, one exactly at the NEXT day's midnight
    # (the exclusive end) must not be.
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 0, 0, 0), event_type="reconnect",
        details="exactly at day start -- included", db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 20, 0, 0, 0), event_type="reconnect",
        details="exactly at next day start -- excluded", db_path=db_path,
    )

    events = journal.get_anomaly_events_for_day(date(2026, 7, 19), db_path=db_path)

    assert [e.details for e in events] == ["exactly at day start -- included"]


def test_get_anomaly_events_in_range_spans_multiple_days(db_path):
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 19, 9, 0), event_type="reconnect", details="day1", db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 20, 9, 0), event_type="circuit_breaker_trigger",
        details="drawdown halt: equity drawdown from peak 9.00% >= 8.0%; halted, requires manual restart",
        db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 7, 25, 9, 0), event_type="reconnect", details="outside range", db_path=db_path,
    )

    events = journal.get_anomaly_events_in_range(
        datetime(2026, 7, 19, 0, 0), datetime(2026, 7, 21, 0, 0), db_path=db_path,
    )

    assert [e.details for e in events] == [
        "day1", "drawdown halt: equity drawdown from peak 9.00% >= 8.0%; halted, requires manual restart",
    ]
