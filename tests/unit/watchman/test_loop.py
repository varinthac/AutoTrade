"""Unit tests for watchman/loop.py -- WatchmanLoop's per-cycle wiring:
connectivity watchdog integration, per-position error isolation (including
CorruptPositionMetadataError's explicit handling), and acting on both
evaluate_watchman's and news_protection's decisions via the adapter."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.execution.adapter import BrokerAdapter, BrokerPosition, OrderResult
from autotrade.watchman import loop as loop_module
from autotrade.watchman import position_metadata
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog, ConnectivityWatchdogConfig
from autotrade.watchman.evaluate import WatchmanConfig, WatchmanDecision
from autotrade.watchman.loop import WatchmanLoop
from autotrade.watchman.news_protection import NewsProtectionConfig, NewsProtectionDecision

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class SpyAdapter(BrokerAdapter):
    def __init__(self, open_positions):
        self._open_positions = open_positions
        self.close_calls: list[tuple] = []
        self.modify_calls: list[tuple] = []
        self.close_result = OrderResult(
            success=True, broker_ticket=None, filled_price=2400.0, filled_volume=0.05,
            retcode=None, message="closed",
        )
        self.modify_result = OrderResult(
            success=True, broker_ticket=None, filled_price=2400.0, filled_volume=None,
            retcode=None, message="modified",
        )

    def place_order(self, request, current_atr=None):
        raise NotImplementedError

    def modify_stop_loss(self, ticket, new_stop_loss):
        self.modify_calls.append((ticket, new_stop_loss))
        return self.modify_result

    def close_position(self, ticket, volume=None):
        self.close_calls.append((ticket, volume))
        return self.close_result

    def get_equity(self):
        raise NotImplementedError

    def get_balance(self):
        raise NotImplementedError

    def get_open_positions(self):
        return self._open_positions


class RaisingAdapter(SpyAdapter):
    def get_open_positions(self):
        raise RuntimeError("MT5 unreachable (simulated)")


def _position(ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, current_sl=2395.0, current_price=2400.0):
    return BrokerPosition(
        ticket=ticket, symbol=symbol, direction=direction, risk_pct=1.0,
        current_sl=current_sl, current_price=current_price, volume=volume,
    )


def _symbol_spec(symbol="XAUUSD", volume_min=0.01, volume_step=0.01):
    return SymbolSpec(
        canonical=symbol, broker_name=symbol, digits=2, point=0.01, tick_size=0.01,
        tick_value=1.0, contract_size=100.0, volume_min=volume_min, volume_max=50.0,
        volume_step=volume_step, trade_stops_level=0, freeze_level=0,
    )


def _history(n=5):
    times = [NOW - timedelta(hours=n - i) for i in range(n)]
    return pd.DataFrame({
        "time": times, "open": [100.0] * n, "high": [101.0] * n,
        "low": [99.0] * n, "close": [100.0] * n,
    })


def _loop(adapter, tmp_path, watchdog=None, news_provider=None) -> WatchmanLoop:
    return WatchmanLoop(
        adapter=adapter,
        watchman_config=WatchmanConfig(),
        news_provider=news_provider or _AllClearNewsProvider(),
        news_protection_config=NewsProtectionConfig(),
        connectivity_watchdog=watchdog or ConnectivityWatchdog(FakeClock(NOW)),
        resolve_symbol_spec=lambda symbol: _symbol_spec(symbol),
        state_path=tmp_path / "position_metadata.json",
    )


class _AllClearNewsProvider:
    def get_high_impact_events(self, currency, window_start, window_end):
        return []


def _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0):
    position_metadata.record_position_opened(
        ticket=ticket, symbol=symbol, direction=direction, entry_price=entry_price,
        initial_stop_distance=stop_distance, entry_swing_index=0, opened_at=NOW,
        state_path=tmp_path / "position_metadata.json",
    )


# --- connectivity watchdog integration --------------------------------------


def test_get_open_positions_success_records_connected_and_checks(tmp_path):
    adapter = SpyAdapter([])
    watchdog = ConnectivityWatchdog(FakeClock(NOW), ConnectivityWatchdogConfig(timeout_minutes=5.0))
    watchdog.record_connected = _CountingWrapper(watchdog.record_connected)
    watchdog.check = _CountingWrapper(watchdog.check)

    wl = _loop(adapter, tmp_path, watchdog=watchdog)
    wl.run_cycle({}, NOW)

    assert watchdog.record_connected.calls == 1
    assert watchdog.check.calls == 1


def test_get_open_positions_failure_skips_record_connected_but_still_checks(tmp_path, caplog):
    adapter = RaisingAdapter([])
    watchdog = ConnectivityWatchdog(FakeClock(NOW), ConnectivityWatchdogConfig(timeout_minutes=5.0))
    watchdog.record_connected = _CountingWrapper(watchdog.record_connected)
    watchdog.check = _CountingWrapper(watchdog.check)

    wl = _loop(adapter, tmp_path, watchdog=watchdog)
    with caplog.at_level(logging.ERROR):
        wl.run_cycle({}, NOW)

    assert watchdog.record_connected.calls == 0
    assert watchdog.check.calls == 1
    assert any("get_open_positions() failed" in r.message for r in caplog.records)


class _CountingWrapper:
    def __init__(self, fn):
        self._fn = fn
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._fn(*args, **kwargs)


# --- per-position error isolation -------------------------------------------


def test_corrupt_metadata_store_halts_management_but_does_not_crash(tmp_path, caplog):
    state_path = tmp_path / "position_metadata.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    assert adapter.close_calls == []
    assert adapter.modify_calls == []
    assert any("corrupt/unreadable" in r.message for r in caplog.records)


def test_one_position_raising_does_not_stop_others_from_being_evaluated(tmp_path, monkeypatch, caplog):
    _record_metadata(tmp_path, ticket=1)
    _record_metadata(tmp_path, ticket=2)
    adapter = SpyAdapter([_position(ticket=1), _position(ticket=2)])
    wl = _loop(adapter, tmp_path)

    def flaky_evaluate(*args, **kwargs):
        if kwargs["position_metadata"].ticket == 1:
            raise ValueError("simulated malformed position")
        return WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change")

    monkeypatch.setattr(loop_module, "evaluate_watchman", flaky_evaluate)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    assert any("unhandled exception managing ticket=1" in r.message for r in caplog.records)
    # ticket=2 still got evaluated (no error for it, no crash of the whole cycle)


def test_missing_metadata_skips_that_position_without_crashing(tmp_path, caplog):
    # ticket=1 has no recorded metadata at all (never opened by this system,
    # or metadata already removed) -- must skip, not raise.
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    with caplog.at_level(logging.WARNING):
        wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == []
    assert adapter.modify_calls == []
    assert any("no recorded entry-time metadata" in r.message for r in caplog.records)


def test_missing_history_for_symbol_skips_that_position(tmp_path, caplog):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD")
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD")])
    wl = _loop(adapter, tmp_path)

    with caplog.at_level(logging.WARNING):
        wl.run_cycle({}, NOW)  # no history for XAUUSD at all

    assert adapter.close_calls == []
    assert any("no seeded history" in r.message for r in caplog.records)


# --- acting on evaluate_watchman's decision ---------------------------------


def test_close_decision_closes_position_and_removes_metadata(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


def test_close_decision_failure_leaves_metadata_intact(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=99, message="close failed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is not None


def test_modify_sl_decision_calls_modify_stop_loss(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="MODIFY_SL", new_stop_loss=2398.0, reason="SL trail"),
    )
    # news_protection is real here (AllClear provider) -- must not fire.
    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.modify_calls == [(1, 2398.0)]
    assert adapter.close_calls == []


def test_no_action_decision_still_runs_news_protection_check(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_ALL", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]


def test_close_decision_skips_news_protection_entirely(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    news_calls = []
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: news_calls.append(1) or NewsProtectionDecision(action="NO_ACTION", reason=""),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert news_calls == []  # never called -- position already closed


# --- acting on news_protection's decision -----------------------------------


def test_close_half_and_breakeven_closes_half_and_moves_sl_to_entry(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, entry_price=2395.0)
    adapter = SpyAdapter([_position(ticket=1, volume=0.10)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_HALF_AND_BREAKEVEN", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, 0.05)]
    assert adapter.modify_calls == [(1, 2395.0)]


def test_close_half_volume_rounds_down_to_broker_step(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, entry_price=2395.0)
    adapter = SpyAdapter([_position(ticket=1, volume=0.03)])  # half=0.015, step=0.01 -> 0.01
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_HALF_AND_BREAKEVEN", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, 0.01)]


def test_close_half_below_volume_min_falls_back_to_full_close(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, entry_price=2395.0)
    # volume=0.01 (broker minimum) -> half=0.005 rounds to 0 -- below volume_min.
    adapter = SpyAdapter([_position(ticket=1, volume=0.01)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_HALF_AND_BREAKEVEN", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]  # full close fallback, not a half
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


def test_close_half_failure_does_not_attempt_breakeven_modify(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, entry_price=2395.0)
    adapter = SpyAdapter([_position(ticket=1, volume=0.10)])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=99, message="close failed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_HALF_AND_BREAKEVEN", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.modify_calls == []


def test_news_close_all_removes_metadata(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, entry_price=2395.0)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_ALL", reason="news incoming"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


# --- per-position error isolation: 3+ positions, middle one raises ---------


def test_middle_of_three_positions_raising_still_evaluates_before_and_after(tmp_path, monkeypatch, caplog):
    # test_one_position_raising_does_not_stop_others_from_being_evaluated
    # (above) only uses 2 positions (the raising one first, then a second
    # one) -- that can't distinguish "everything AFTER the bad one still
    # runs" from "everything BEFORE it already ran before the exception".
    # Ticket=2 here is sandwiched between 1 and 3 to prove BOTH directions.
    _record_metadata(tmp_path, ticket=1)
    _record_metadata(tmp_path, ticket=2)
    _record_metadata(tmp_path, ticket=3)
    adapter = SpyAdapter([_position(ticket=1), _position(ticket=2), _position(ticket=3)])
    wl = _loop(adapter, tmp_path)

    evaluated_tickets: list[int] = []

    def flaky_evaluate(*args, **kwargs):
        ticket = kwargs["position_metadata"].ticket
        evaluated_tickets.append(ticket)
        if ticket == 2:
            raise ValueError("simulated malformed position")
        return WatchmanDecision(action="MODIFY_SL", new_stop_loss=2398.0, reason="ok")

    monkeypatch.setattr(loop_module, "evaluate_watchman", flaky_evaluate)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    assert evaluated_tickets == [1, 2, 3]  # all three were attempted, in order
    assert any("unhandled exception managing ticket=2" in r.message for r in caplog.records)
    # The concrete proof both sides completed their action: ticket=1 (BEFORE
    # the raise) and ticket=3 (AFTER the raise) both actually got their
    # MODIFY_SL applied; ticket=2 (which raised) never did.
    assert adapter.modify_calls == [(1, 2398.0), (3, 2398.0)]


# --- news protection: repeats every cycle, no dedup (should-fix #3) --------


class _AlwaysNewsProvider:
    def get_high_impact_events(self, currency, window_start, window_end):
        from autotrade.council.news_calendar import NewsEvent

        return [NewsEvent(currency=currency, impact="high", event_time=window_start)]


class DrainableAdapter(BrokerAdapter):
    """A stateful fake -- unlike SpyAdapter's static position list, this
    actually shrinks its own tracked volume on every close_position() call
    and reports that live-shrunk volume back via get_open_positions(), the
    same way a real broker would. Needed to concretely demonstrate that
    WatchmanLoop calling check_news_protection() fresh every cycle (no
    "already protected this position for this news window" dedup state)
    repeatedly re-triggers CLOSE_HALF_AND_BREAKEVEN cycle after cycle, each
    time against whatever volume is left -- not a one-shot protective
    action."""

    def __init__(self, ticket, symbol, volume, current_sl, current_price, direction="BUY"):
        self._ticket = ticket
        self._symbol = symbol
        self._volume = volume
        self._current_sl = current_sl
        self._current_price = current_price
        self._direction = direction
        self.close_calls: list[tuple] = []
        self.modify_calls: list[tuple] = []

    def place_order(self, request, current_atr=None):
        raise NotImplementedError

    def modify_stop_loss(self, ticket, new_stop_loss):
        self.modify_calls.append((ticket, new_stop_loss))
        self._current_sl = new_stop_loss
        return OrderResult(
            success=True, broker_ticket=ticket, filled_price=new_stop_loss,
            filled_volume=None, retcode=None, message="modified",
        )

    def close_position(self, ticket, volume=None):
        self.close_calls.append((ticket, volume))
        close_amount = self._volume if volume is None else volume
        self._volume = round(self._volume - close_amount, 8)
        return OrderResult(
            success=True, broker_ticket=ticket, filled_price=self._current_price,
            filled_volume=close_amount, retcode=None, message="closed",
        )

    def get_equity(self):
        raise NotImplementedError

    def get_balance(self):
        raise NotImplementedError

    def get_open_positions(self):
        if self._volume <= 1e-9:
            return []
        return [BrokerPosition(
            ticket=self._ticket, symbol=self._symbol, direction=self._direction, risk_pct=1.0,
            current_sl=self._current_sl, current_price=self._current_price, volume=self._volume,
        )]


def test_news_protection_close_half_fires_once_then_suppressed_for_remainder_of_window(tmp_path, monkeypatch):
    # Third issue the code-reviewer separately flagged as should-fix, now
    # fixed: check_news_protection used to have no memory of already having
    # protected a position -- WatchmanLoop re-triggered CLOSE_HALF_AND_BREAKEVEN
    # on EVERY cycle for as long as the SAME 30-minute news window stayed
    # active, draining the position down to nothing within a handful of
    # cycles. Fixed via PositionMetadata.news_protected_until: the
    # protective action now fires ONCE per news window, and stays suppressed
    # until that window has genuinely passed.
    _record_metadata(tmp_path, ticket=1, entry_price=100.0, stop_distance=1.0)
    adapter = DrainableAdapter(ticket=1, symbol="XAUUSD", volume=0.10, current_sl=99.0, current_price=110.0)
    wl = _loop(adapter, tmp_path, news_provider=_AlwaysNewsProvider())

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )

    # 5 cycles, all still within the SAME 30-minute news window
    # (NewsProtectionConfig's default news_window_minutes) -- must fire
    # ONCE, not five times.
    for _ in range(5):
        wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert len(adapter.close_calls) == 1
    assert adapter.close_calls[0] == (1, pytest.approx(0.05))  # half of 0.10, once
    assert adapter.modify_calls == [(1, 100.0)]  # breakeven applied once, not repeatedly
    remaining = adapter.get_open_positions()
    assert len(remaining) == 1
    assert remaining[0].volume == pytest.approx(0.05)  # NOT drained to zero over 5 cycles

    state_path = tmp_path / "position_metadata.json"
    metadata = position_metadata.get_position_metadata(1, state_path)
    assert metadata.news_protected_until is not None
    assert metadata.news_protected_until > NOW

    # Once the SAME news window has genuinely passed, a still-incoming
    # high-impact event can trigger protection again -- this is suppression
    # WITHIN a window, not a permanent one-shot disable for the position.
    later = metadata.news_protected_until + timedelta(minutes=1)
    wl.run_cycle({"XAUUSD": _history()}, later)

    assert len(adapter.close_calls) == 2
    assert adapter.close_calls[1][0] == 1
