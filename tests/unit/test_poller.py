"""Unit tests for feed/poller.py — fetch_last_closed_bar() and the
poll_new_bars() dedup loop. MT5 itself is mocked; no live terminal needed."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autotrade.feed import poller
from autotrade.feed.poller import BarFetchError, fetch_last_closed_bar, poll_new_bars


def _naive_server_time(epoch_seconds: int) -> datetime:
    """Same conversion poller.py applies to r["time"]: reads the epoch-like
    integer as a naive server-time reading, no timezone attached."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(tzinfo=None)


def _fake_rate(epoch_seconds: int, close: float = 100.0):
    return {
        "time": epoch_seconds,
        "open": 99.0,
        "high": 101.0,
        "low": 98.5,
        "close": close,
        "tick_volume": 500,
        "spread": 3,
    }


def test_fetch_last_closed_bar_success_reads_position_1_not_0(monkeypatch):
    captured = {}

    def fake_copy_rates_from_pos(symbol, timeframe, start_pos, count):
        captured["args"] = (symbol, timeframe, start_pos, count)
        return [_fake_rate(1_700_000_000, close=123.45)]

    monkeypatch.setattr(poller.mt5, "copy_rates_from_pos", fake_copy_rates_from_pos)

    bar = fetch_last_closed_bar("XAUUSD", "H1")

    # position=1 (not 0) is the look-ahead-bias-avoidance invariant — must not
    # be weakened to 0 ever.
    assert captured["args"][2] == 1
    assert captured["args"][3] == 1
    assert bar.close == 123.45
    assert bar.time == _naive_server_time(1_700_000_000)
    assert bar.time.tzinfo is None
    assert bar.tick_volume == 500
    assert bar.spread == 3


def test_fetch_last_closed_bar_raises_when_copy_rates_returns_none(monkeypatch):
    monkeypatch.setattr(poller.mt5, "copy_rates_from_pos", lambda *a, **k: None)
    monkeypatch.setattr(poller.mt5, "last_error", lambda: (1, "no history"))

    with pytest.raises(BarFetchError, match="returned nothing"):
        fetch_last_closed_bar("XAUUSD", "H1")


def test_fetch_last_closed_bar_raises_when_copy_rates_returns_empty(monkeypatch):
    monkeypatch.setattr(poller.mt5, "copy_rates_from_pos", lambda *a, **k: [])
    monkeypatch.setattr(poller.mt5, "last_error", lambda: (2, "empty"))

    with pytest.raises(BarFetchError, match="returned nothing"):
        fetch_last_closed_bar("XAUUSD", "H1")


def test_fetch_last_closed_bar_unknown_timeframe_raises_key_error(monkeypatch):
    monkeypatch.setattr(
        poller.mt5, "copy_rates_from_pos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unreachable"))
    )
    with pytest.raises(KeyError):
        fetch_last_closed_bar("XAUUSD", "M30")  # not in TIMEFRAME_MAP


def test_poll_new_bars_calls_back_once_per_new_closed_bar(monkeypatch):
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    bar_sequence = [_fake_rate(1_700_000_000), _fake_rate(1_700_000_000), _fake_rate(1_700_003_600)]
    call_index = {"i": 0}

    def fake_fetch(broker_symbol, timeframe):
        rate = bar_sequence[min(call_index["i"], len(bar_sequence) - 1)]
        call_index["i"] += 1
        from autotrade.feed.snapshot import Bar

        return Bar(
            time=_naive_server_time(rate["time"]),
            open=rate["open"], high=rate["high"], low=rate["low"], close=rate["close"],
            tick_volume=rate["tick_volume"], spread=rate["spread"],
        )

    monkeypatch.setattr(poller, "fetch_last_closed_bar", fake_fetch)

    received = []
    poll_new_bars(["XAUUSD"], "H1", on_new_bar=received.append, poll_interval_sec=0, max_iterations=3)

    # bar.time changed on iteration 1 (new) -> 2 (same, no call) -> 3 (new)
    assert len(received) == 2
    assert received[0].bar.time == _naive_server_time(1_700_000_000)
    assert received[1].bar.time == _naive_server_time(1_700_003_600)
    assert received[0].symbol == "XAUUSD"
    assert received[0].timeframe == "H1"


def test_poll_new_bars_swallows_bar_fetch_error_and_continues(monkeypatch):
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    def always_fails(broker_symbol, timeframe):
        raise BarFetchError("boom")

    monkeypatch.setattr(poller, "fetch_last_closed_bar", always_fails)

    received = []
    # Must not raise/crash the loop even though every fetch fails.
    poll_new_bars(["XAUUSD"], "H1", on_new_bar=received.append, poll_interval_sec=0, max_iterations=2)

    assert received == []


def test_poll_new_bars_tracks_each_symbol_independently(monkeypatch):
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    from autotrade.feed.snapshot import Bar

    def fake_fetch(broker_symbol, timeframe):
        # XAUUSD always advances, EURUSD never does
        ts = 1_700_000_000 if broker_symbol == "EURUSD" else fake_fetch.counter
        if broker_symbol == "XAUUSD":
            fake_fetch.counter += 3600
        return Bar(time=_naive_server_time(ts), open=1, high=1, low=1, close=1,
                   tick_volume=1, spread=1)

    fake_fetch.counter = 1_700_000_000
    monkeypatch.setattr(poller, "fetch_last_closed_bar", fake_fetch)

    received = []
    poll_new_bars(["XAUUSD", "EURUSD"], "H1", on_new_bar=received.append, poll_interval_sec=0, max_iterations=3)

    xau_calls = [s for s in received if s.symbol == "XAUUSD"]
    eur_calls = [s for s in received if s.symbol == "EURUSD"]
    assert len(xau_calls) == 3  # advances every iteration
    assert len(eur_calls) == 1  # only the first iteration is "new"


def test_on_iteration_end_called_once_per_iteration_after_all_symbols(monkeypatch):
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    from autotrade.feed.snapshot import Bar

    def fake_fetch(broker_symbol, timeframe):
        return Bar(time=_naive_server_time(1_700_000_000), open=1, high=1, low=1, close=1, tick_volume=1, spread=1)

    monkeypatch.setattr(poller, "fetch_last_closed_bar", fake_fetch)

    iteration_end_calls = []
    poll_new_bars(
        ["XAUUSD", "EURUSD"], "H1", on_new_bar=lambda snapshot: None,
        poll_interval_sec=0, max_iterations=3,
        on_iteration_end=lambda: iteration_end_calls.append(1),
    )

    assert len(iteration_end_calls) == 3  # once per outer iteration, not once per symbol


def test_poll_new_bars_on_new_bar_sequence_unchanged_with_or_without_iteration_hook(monkeypatch):
    # Backward-compatibility regression for the on_iteration_end addition:
    # a caller that does NOT pass it must see the EXACT SAME on_new_bar call
    # sequence (which bars, in which order, deduped the same way) as a
    # caller that does -- the hook must be a strict, side-effect-free
    # add-on to the existing dedup loop, not something that alters it.
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    from autotrade.feed.snapshot import Bar

    bar_sequence = [
        _fake_rate(1_700_000_000), _fake_rate(1_700_000_000),
        _fake_rate(1_700_003_600), _fake_rate(1_700_003_600),
    ]

    def _make_fetch():
        call_index = {"i": 0}

        def fake_fetch(broker_symbol, timeframe):
            rate = bar_sequence[min(call_index["i"], len(bar_sequence) - 1)]
            call_index["i"] += 1
            return Bar(
                time=_naive_server_time(rate["time"]), open=rate["open"], high=rate["high"],
                low=rate["low"], close=rate["close"], tick_volume=rate["tick_volume"], spread=rate["spread"],
            )

        return fake_fetch

    monkeypatch.setattr(poller, "fetch_last_closed_bar", _make_fetch())
    received_without_hook = []
    poll_new_bars(["XAUUSD"], "H1", on_new_bar=received_without_hook.append, poll_interval_sec=0, max_iterations=4)

    monkeypatch.setattr(poller, "fetch_last_closed_bar", _make_fetch())
    received_with_hook = []
    poll_new_bars(
        ["XAUUSD"], "H1", on_new_bar=received_with_hook.append, poll_interval_sec=0, max_iterations=4,
        on_iteration_end=lambda: None,
    )

    assert len(received_without_hook) == 2  # sanity: dedup still worked as expected
    assert [s.bar.time for s in received_without_hook] == [s.bar.time for s in received_with_hook]
    assert [s.symbol for s in received_without_hook] == [s.symbol for s in received_with_hook]


def test_on_iteration_end_none_by_default_never_called(monkeypatch):
    monkeypatch.setattr(poller, "to_broker_name", lambda symbol: symbol)

    from autotrade.feed.snapshot import Bar

    monkeypatch.setattr(
        poller, "fetch_last_closed_bar",
        lambda broker_symbol, timeframe: Bar(
            time=_naive_server_time(1_700_000_000), open=1, high=1, low=1, close=1, tick_volume=1, spread=1,
        ),
    )

    # Must not raise even though on_iteration_end is never supplied.
    poll_new_bars(["XAUUSD"], "H1", on_new_bar=lambda snapshot: None, poll_interval_sec=0, max_iterations=2)
