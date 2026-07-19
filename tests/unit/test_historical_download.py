"""Unit tests for feed/historical.py's download_historical() — the
copy_rates_range() error paths and the dedup/gap-counting pipeline around
the already-tested _is_weekend_gap() pure function. MT5 is mocked; nothing
here touches _is_weekend_gap's classification logic itself.

Bar times are naive MT5 broker-server-time readings (see common/mt5_time.py)
— `server_now()` is monkeypatched per test rather than hitting a live
terminal."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autotrade.feed import historical
from autotrade.feed.historical import HistoricalDownloadError, download_historical


def _epoch(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def _rate(y, m, d, h, close=100.0):
    return {
        "time": _epoch(y, m, d, h),
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "tick_volume": 10,
        "spread": 2,
    }


def test_download_historical_raises_when_copy_rates_range_returns_none(monkeypatch):
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: None)
    monkeypatch.setattr(historical.mt5, "last_error", lambda: (5, "no data"))

    with pytest.raises(HistoricalDownloadError, match="returned nothing"):
        download_historical("XAUUSD", "H1", days=30)


def test_download_historical_raises_when_copy_rates_range_returns_empty(monkeypatch):
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: [])
    monkeypatch.setattr(historical.mt5, "last_error", lambda: (6, "empty"))

    with pytest.raises(HistoricalDownloadError, match="returned nothing"):
        download_historical("XAUUSD", "H1", days=30)


def test_download_historical_dedups_counts_gaps_and_saves_csv(monkeypatch, tmp_path):
    # Fri 20:00, Fri 20:00 (exact duplicate), Fri 22:00 (unexplained gap,
    # missing 21:00 -- same weekday, not a weekend gap), then Mon 00:00
    # (Fri->Mon is the expected/explained weekend gap), then Mon 01:00
    # (adjacent, no gap).
    rates = [
        _rate(2026, 7, 24, 20, close=98.0),
        _rate(2026, 7, 24, 20, close=98.0),  # exact duplicate timestamp
        _rate(2026, 7, 24, 22, close=99.0),  # gap: missing hour 21 (same Friday)
        _rate(2026, 7, 27, 0, close=100.0),  # Monday open -> weekend gap (explained)
        _rate(2026, 7, 27, 1, close=101.0),
    ]
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: rates)
    monkeypatch.setattr(historical, "HISTORICAL_DIR", tmp_path)
    # Well past the last bar's close (2026-07-27 01:00 + 1h) so it isn't
    # dropped as still-forming.
    monkeypatch.setattr(historical, "server_now", lambda broker_symbol: datetime(2026, 7, 28))

    result = download_historical("XAUUSD", "H1", days=10, end=datetime(2026, 7, 28, tzinfo=timezone.utc))

    assert result.symbol == "XAUUSD"
    assert result.timeframe == "H1"
    assert result.duplicate_rows_dropped == 1
    assert result.rows == 4  # 5 input rows - 1 exact duplicate
    assert result.unexplained_gaps == 1  # only the Friday 20:00->22:00 gap; the Fri->Mon gap is weekend-explained
    assert result.start == datetime(2026, 7, 24, 20)
    assert result.end == datetime(2026, 7, 27, 1)
    assert result.path == tmp_path / "XAUUSD_H1.csv"
    assert result.path.exists()


def test_download_historical_reports_zero_unexplained_gaps_for_clean_data(monkeypatch, tmp_path):
    rates = [_rate(2026, 7, 20, h) for h in range(5)]
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: rates)
    monkeypatch.setattr(historical, "HISTORICAL_DIR", tmp_path)
    # Well past the last bar's close (2026-07-20 04:00 + 1h) so it isn't
    # dropped as still-forming.
    monkeypatch.setattr(historical, "server_now", lambda broker_symbol: datetime(2026, 7, 20, 10))

    result = download_historical("EURUSD", "H1", days=1)

    assert result.duplicate_rows_dropped == 0
    assert result.unexplained_gaps == 0
    assert result.rows == 5


def test_download_historical_drops_still_forming_last_bar(monkeypatch, tmp_path):
    rates = [_rate(2026, 7, 20, h) for h in range(5)]  # last closed bar at h=4
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: rates)
    monkeypatch.setattr(historical, "HISTORICAL_DIR", tmp_path)
    # "now" is mid-way through the h=4 bar's hour -> h=4 hasn't closed yet.
    monkeypatch.setattr(historical, "server_now", lambda broker_symbol: datetime(2026, 7, 20, 4, 30))

    result = download_historical("EURUSD", "H1", days=1)

    assert result.rows == 4  # the still-forming h=4 bar is dropped
    assert result.end == datetime(2026, 7, 20, 3)


def test_download_historical_keeps_last_bar_once_it_has_fully_closed(monkeypatch, tmp_path):
    rates = [_rate(2026, 7, 20, h) for h in range(5)]  # last closed bar at h=4
    monkeypatch.setattr(historical.mt5, "copy_rates_range", lambda *a, **k: rates)
    monkeypatch.setattr(historical, "HISTORICAL_DIR", tmp_path)
    # "now" is exactly at the h=4 bar's close boundary -> fully closed.
    monkeypatch.setattr(historical, "server_now", lambda broker_symbol: datetime(2026, 7, 20, 5))

    result = download_historical("EURUSD", "H1", days=1)

    assert result.rows == 5
    assert result.end == datetime(2026, 7, 20, 4)
