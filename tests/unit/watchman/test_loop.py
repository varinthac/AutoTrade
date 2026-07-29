"""Unit tests for watchman/loop.py -- WatchmanLoop's per-cycle wiring:
connectivity watchdog integration, per-position error isolation (including
CorruptPositionMetadataError's explicit handling), and acting on both
evaluate_watchman's and news_protection's decisions via the adapter."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.execution.adapter import BrokerAdapter, BrokerPosition, ClosedTradeInfo, OrderResult
from autotrade.risk.circuit_breaker import CircuitBreaker
from autotrade.store import journal
from autotrade.watchman import loop as loop_module
from autotrade.watchman import position_metadata
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog, ConnectivityWatchdogConfig
from autotrade.watchman.evaluate import WatchmanConfig, WatchmanDecision
from autotrade.watchman.loop import WatchmanLoop
from autotrade.watchman.news_protection import NewsProtectionConfig, NewsProtectionDecision

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
OWN_MAGIC = 234_000  # matches execution.demo_adapter.DEFAULT_MAGIC


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
        self.get_closed_trade_info_calls: list[int] = []
        self.closed_trade_info: dict[int, ClosedTradeInfo | None] = {}
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

    def get_closed_trade_info(self, ticket):
        self.get_closed_trade_info_calls.append(ticket)
        return self.closed_trade_info.get(ticket)


class RaisingAdapter(SpyAdapter):
    def get_open_positions(self):
        raise RuntimeError("MT5 unreachable (simulated)")


def _position(
    ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, current_sl=2395.0, current_price=2400.0,
    magic=0,
):
    return BrokerPosition(
        ticket=ticket, symbol=symbol, direction=direction, risk_pct=1.0,
        current_sl=current_sl, current_price=current_price, volume=volume, magic=magic,
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


def _circuit_breaker(tmp_path) -> CircuitBreaker:
    return CircuitBreaker(
        daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0,
        state_path=tmp_path / "circuit_breaker_state.json",
    )


def _loop(adapter, tmp_path, watchdog=None, news_provider=None, circuit_breaker=None) -> WatchmanLoop:
    return WatchmanLoop(
        adapter=adapter,
        watchman_config=WatchmanConfig(),
        news_provider=news_provider or _AllClearNewsProvider(),
        news_protection_config=NewsProtectionConfig(),
        connectivity_watchdog=watchdog or ConnectivityWatchdog(FakeClock(NOW)),
        circuit_breaker=circuit_breaker or _circuit_breaker(tmp_path),
        own_magic=OWN_MAGIC,
        resolve_symbol_spec=lambda symbol: _symbol_spec(symbol),
        state_path=tmp_path / "position_metadata.json",
        journal_db_path=tmp_path / "trade_journal.sqlite",
    )


class _AllClearNewsProvider:
    def get_high_impact_events(self, currency, window_start, window_end):
        return []


def _record_metadata(
    tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0,
    entry_spread_points=None, actual_slippage=None, entry_classification="normal",
):
    position_metadata.record_position_opened(
        ticket=ticket, symbol=symbol, direction=direction, entry_price=entry_price,
        initial_stop_distance=stop_distance, entry_swing_index=0, opened_at=NOW,
        state_path=tmp_path / "position_metadata.json",
        entry_spread_points=entry_spread_points, actual_slippage=actual_slippage,
        entry_classification=entry_classification,
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


# --- 2026-07-25: CLOSE retry backoff after a failure ------------------------
# Real incident: with the market closed over a weekend, Watchman kept
# re-deciding CLOSE (structure invalidation still true) and re-attempting
# close_position() every ~5s poll cycle, each failure re-triggering
# journal.record_anomaly_event() (which itself notify()s unconditionally on
# every call) -- spamming Telegram continuously for two straight days.


def test_close_decision_failure_is_not_retried_within_backoff_window(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=10018, message="market closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)
    wl.run_cycle({"XAUUSD": _history()}, NOW + timedelta(seconds=5))
    wl.run_cycle({"XAUUSD": _history()}, NOW + timedelta(minutes=10))

    assert len(adapter.close_calls) == 1


def test_close_decision_retries_again_once_backoff_window_elapses(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=10018, message="market closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)
    wl.run_cycle({"XAUUSD": _history()}, NOW + timedelta(minutes=16))

    assert len(adapter.close_calls) == 2


def test_close_decision_success_after_a_prior_failure_clears_backoff_state(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=10018, message="market closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )
    wl.run_cycle({"XAUUSD": _history()}, NOW)  # fails, backoff starts

    adapter.close_result = OrderResult(
        success=True, broker_ticket=None, filled_price=2400.0, filled_volume=0.05,
        retcode=None, message="closed",
    )
    wl.run_cycle({"XAUUSD": _history()}, NOW + timedelta(minutes=16))  # succeeds

    assert len(adapter.close_calls) == 2
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


def test_close_decision_backoff_is_per_ticket_not_global(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    _record_metadata(tmp_path, ticket=2)
    adapter = SpyAdapter([_position(ticket=1), _position(ticket=2, symbol="EURUSD")])
    adapter.close_result = OrderResult(
        success=False, broker_ticket=None, filled_price=None, filled_volume=None,
        retcode=10018, message="market closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )
    wl.run_cycle({"XAUUSD": _history(), "EURUSD": _history()}, NOW)  # both fail, both backed off

    wl.run_cycle({"XAUUSD": _history(), "EURUSD": _history()}, NOW + timedelta(seconds=5))

    assert adapter.close_calls.count((1, None)) == 1
    assert adapter.close_calls.count((2, None)) == 1


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

    def get_closed_trade_info(self, ticket):
        raise NotImplementedError("not exercised by this test")


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


# --- Phase 8a: trade-journal reconciliation (two-path design) --------------


def test_reconciliation_writes_trade_record_for_ticket_gone_from_open_positions(tmp_path):
    _record_metadata(
        tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0,
        entry_spread_points=12.0, actual_slippage=0.3,
    )
    adapter = SpyAdapter([])  # ticket=1 is no longer open -- broker-side close
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=3.0, exit_reason="take_profit",
    )
    wl = _loop(adapter, tmp_path)

    wl.run_cycle({}, NOW)

    assert adapter.get_closed_trade_info_calls == [1]
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1
    trade = trades[0]
    assert trade.symbol == "XAUUSD"
    assert trade.exit_reason == "take_profit"
    assert trade.broker_ticket == 1
    assert trade.gross_pnl == 150.0
    assert trade.cost == 3.0
    assert trade.net_pnl == 147.0
    # Appendix A §5.1 daily-report fields -- carried through from the
    # PositionMetadata recorded at entry, not left None (should-fix #5).
    assert trade.entry_spread_points == 12.0
    assert trade.actual_slippage == 0.3
    assert trade.entry_classification == "normal"


def test_reconciliation_close_carries_entry_classification_through(tmp_path):
    # 2026-07-30: PositionMetadata.entry_classification must survive onto
    # the TradeRecord via the reconciliation close path too, not just the
    # explicit-close path.
    _record_metadata(
        tmp_path, ticket=2, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0,
        entry_classification="orphan_seeded",
    )
    adapter = SpyAdapter([])
    adapter.closed_trade_info[2] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=3.0, exit_reason="take_profit",
    )
    wl = _loop(adapter, tmp_path)

    wl.run_cycle({}, NOW)

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert trades[0].entry_classification == "orphan_seeded"


def test_reconciliation_no_history_found_yet_retains_metadata_and_does_not_crash(tmp_path):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([])  # ticket=1 gone, but no closing deal in MT5 history yet
    wl = _loop(adapter, tmp_path)

    wl.run_cycle({}, NOW)  # must not raise

    assert adapter.get_closed_trade_info_calls == [1]
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is not None
    assert journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite") == []


def test_reconciliation_skips_tickets_still_open(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1)
    adapter = SpyAdapter([_position(ticket=1)])
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.get_closed_trade_info_calls == []  # still open -- reconciliation never looks at it
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is not None


def test_partial_close_does_not_trigger_reconciliation_or_remove_metadata(tmp_path, monkeypatch):
    # A news-protection half-close leaves the SAME ticket in
    # get_open_positions() (just smaller) -- reconciliation must not treat
    # it as closed, must not query history for it, and must not write a
    # trade record or remove metadata.
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

    assert adapter.close_calls == [(1, 0.05)]  # the partial close itself did happen
    assert adapter.get_closed_trade_info_calls == []  # ticket=1 still "open" per SpyAdapter's static list
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is not None
    assert journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite") == []


def test_explicit_close_writes_trade_record_immediately_with_real_cost_from_history(tmp_path, monkeypatch):
    # 2026-07-29 audit fix: the explicit-close path now best-effort queries
    # get_closed_trade_info for the REAL commission/swap (previously always
    # cost=0.0, skewing net_pnl/r_multiple and the circuit breaker's
    # realized-P&L accounting on every overnight hold). Only `cost` is
    # taken from history -- exit_reason stays the Watchman decision's own.
    _record_metadata(
        tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0,
        entry_spread_points=8.0, actual_slippage=0.2,
    )
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=1.70, exit_reason="manual",  # history's own reason must NOT win
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]
    assert adapter.get_closed_trade_info_calls == [1]  # exactly one history query, for cost
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "structure_invalidation"  # Watchman's classification, not history's "manual"
    assert trade.broker_ticket == 1
    assert trade.exit_price == 2410.0
    assert trade.lot_size == 0.1
    assert trade.cost == 1.70  # the REAL commission/swap from history
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - 1.70)
    # Appendix A §5.1 daily-report fields -- carried through from the
    # PositionMetadata recorded at entry, not left None (should-fix #5).
    assert trade.entry_spread_points == 8.0
    assert trade.actual_slippage == 0.2
    assert trade.entry_classification == "normal"  # 2026-07-30: carried through from PositionMetadata


def test_explicit_close_carries_a_non_normal_entry_classification_through(tmp_path, monkeypatch):
    _record_metadata(
        tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0,
        entry_classification="high_slippage",
    )
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert trades[0].entry_classification == "high_slippage"


def test_explicit_close_falls_back_to_cost_zero_when_history_unavailable(tmp_path, monkeypatch):
    # get_closed_trade_info returning None (deal not visible yet / query
    # failed) must never block or delay recording the close -- fall back to
    # the pre-2026-07-29 cost=0.0 behavior.
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)  # SpyAdapter.closed_trade_info empty -> get returns None

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1
    assert trades[0].cost == 0.0


def test_explicit_close_history_query_raising_still_records_the_close(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )

    def _boom(ticket):
        raise RuntimeError("simulated MT5 history failure")

    adapter.get_closed_trade_info = _boom
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1
    assert trades[0].cost == 0.0
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


def test_explicit_close_is_not_double_counted_by_reconciliation_same_or_later_cycle(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    # SpyAdapter's open_positions list is static -- ticket=1 stays "open" in
    # it even after close_position() is called, same as the existing
    # CLOSE-decision tests above. This deliberately proves the SAME-cycle
    # non-double-counting guarantee: reconciliation (which runs after the
    # open-positions loop, in the same run_cycle() call) must see ticket=1's
    # metadata already gone (removed by the explicit-close path moments
    # earlier) and so never process it as a closed-tracked ticket. (The ONE
    # get_closed_trade_info call below is the explicit-close path's own
    # 2026-07-29 cost fetch, not reconciliation re-processing the ticket.)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)
    assert adapter.get_closed_trade_info_calls == [1]  # the explicit close's own cost fetch only

    # A later cycle: ticket=1 has no metadata anymore, so neither the
    # per-position management loop (no recorded metadata -> skip) nor
    # reconciliation (nothing tracked for it anymore) re-process it.
    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(1, None)]  # never closed a second time
    assert adapter.get_closed_trade_info_calls == [1]  # no further queries in the later cycle
    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1  # exactly one record total, no double-counting


def test_remove_position_metadata_failure_after_explicit_close_does_not_double_write(
    tmp_path, monkeypatch, caplog,
):
    # Regression test: if remove_position_metadata() throws right after
    # _record_explicit_close() already wrote a TradeRecord, the ticket stays
    # tracked (removal never completed) -- a LATER cycle's reconciliation
    # then re-observes the same now-closed ticket and tries to record it a
    # SECOND time. TradeRecord.broker_ticket's UNIQUE constraint plus
    # record_closed_trade's IntegrityError handling must ensure exactly ONE
    # TradeRecord survives and that no exception ever escapes run_cycle.
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    real_remove_position_metadata = position_metadata.remove_position_metadata

    def _simulate_crash_after_explicit_close_write(ticket, state_path=None):
        raise RuntimeError("simulated crash between explicit-close write and metadata removal")

    monkeypatch.setattr(loop_module, "remove_position_metadata", _simulate_crash_after_explicit_close_write)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    # The explicit-close path's TradeRecord write already succeeded before
    # the simulated crash -- but metadata removal never completed, so the
    # ticket is still tracked.
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is not None
    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1

    # A later cycle: the position has genuinely closed (gone from
    # get_open_positions()), so reconciliation re-observes the still-tracked
    # ticket and attempts to record it again.
    monkeypatch.setattr(loop_module, "remove_position_metadata", real_remove_position_metadata)
    adapter._open_positions = []
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=3.0, exit_reason="take_profit",
    )

    wl.run_cycle({}, NOW)  # must not raise despite the duplicate broker_ticket

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1  # still exactly one -- the duplicate write was rejected, not appended
    assert position_metadata.get_position_metadata(1, tmp_path / "position_metadata.json") is None


def test_duplicate_trade_record_write_does_not_also_double_notify(tmp_path, monkeypatch, caplog):
    # Same crash-then-duplicate-write race as
    # test_remove_position_metadata_failure_after_explicit_close_does_not_double_write,
    # but asserting on notify() instead of the TradeRecord count: the module
    # docstring promises "called exactly once per broker ticket" for
    # journal.record_closed_trade's IntegrityError-swallow guard, so a
    # rejected duplicate write must not ALSO fire a second Telegram
    # notification for the same real-world close.
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    real_remove_position_metadata = position_metadata.remove_position_metadata

    def _simulate_crash_after_explicit_close_write(ticket, state_path=None):
        raise RuntimeError("simulated crash between explicit-close write and metadata removal")

    monkeypatch.setattr(loop_module, "remove_position_metadata", _simulate_crash_after_explicit_close_write)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    assert len(calls) == 1  # the genuine first close notified

    # A later cycle: reconciliation re-observes the still-tracked ticket and
    # attempts to record (and would notify for) it again.
    monkeypatch.setattr(loop_module, "remove_position_metadata", real_remove_position_metadata)
    adapter._open_positions = []
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=3.0, exit_reason="take_profit",
    )

    wl.run_cycle({}, NOW)  # must not raise despite the duplicate broker_ticket

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1  # confirmed: still only one TradeRecord (the write was rejected)
    assert len(calls) == 1  # so notify() must also still have fired only once total


# --- notify() hook (mocked, not the HTTP layer) ---------------------------


def test_explicit_close_notifies_once_with_key_trade_facts(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)

    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )
    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert len(calls) == 1
    assert "XAUUSD" in calls[0]
    assert "BUY" in calls[0]
    assert "2395.00000" in calls[0]  # entry
    assert "2410.00000" in calls[0]  # exit
    assert "structure_invalidation" in calls[0]

    # Existing behavior (the whole point of Phase 8a) unaffected by notify being mocked.
    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1


def test_reconciliation_close_notifies_once_with_key_trade_facts(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([])  # ticket=1 no longer open -- broker-side close
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2410.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=150.0, cost=3.0, exit_reason="take_profit",
    )
    wl = _loop(adapter, tmp_path)

    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    wl.run_cycle({}, NOW)

    assert len(calls) == 1
    assert "XAUUSD" in calls[0]
    assert "take_profit" in calls[0]


# --- circuit breaker feed (2026-07-23 fix) ---------------------------------
#
# Real incident: two consecutive live losing trades in one day still showed
# circuit_breaker_state.json's consecutive_losses=0 / realized_pnl_today=0.0
# -- record_trade_close() was never called anywhere in the live loop. These
# tests assert the real CircuitBreaker's persisted state directly (same
# read-the-real-file convention as journal.get_trades_for_day above), not a
# mock, so a regression back to "never called" would show up as a state
# file that just never changes from its all-zero defaults.


def _cb_state(tmp_path) -> dict:
    return json.loads((tmp_path / "circuit_breaker_state.json").read_text(encoding="utf-8"))


def test_explicit_close_losing_trade_feeds_circuit_breaker(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2390.0, filled_volume=0.1,  # below entry -- a loss
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    trade = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")[0]
    assert trade.net_pnl < 0  # confirms the premise: this really is a losing trade
    state = _cb_state(tmp_path)
    assert state["consecutive_losses"] == 1
    assert state["realized_pnl_today"] == pytest.approx(trade.net_pnl)


def test_explicit_close_winning_trade_resets_consecutive_losses(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2410.0, filled_volume=0.1,  # above entry -- a win
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    trade = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")[0]
    assert trade.net_pnl > 0
    state = _cb_state(tmp_path)
    assert state["consecutive_losses"] == 0
    assert state["realized_pnl_today"] == pytest.approx(trade.net_pnl)


def test_reconciliation_close_feeds_circuit_breaker(tmp_path):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([])  # ticket=1 no longer open -- broker-side close
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2380.0, close_time=NOW, closed_volume=0.1,  # a loss
        gross_pnl=-150.0, cost=3.0, exit_reason="stop_loss",
    )
    wl = _loop(adapter, tmp_path)

    wl.run_cycle({}, NOW)

    trade = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")[0]
    state = _cb_state(tmp_path)
    assert state["consecutive_losses"] == 1
    assert state["realized_pnl_today"] == pytest.approx(trade.net_pnl)


def test_three_consecutive_losses_across_cycles_trips_the_halt(tmp_path):
    # End-to-end proof this fix actually restores the safety gate the
    # incident found silently dead: max_consecutive_losses=3 (the fixture's
    # own _circuit_breaker default) must genuinely halt after the 3rd real
    # loss in a row, reconciled one at a time across separate tickets/cycles
    # -- exactly the "3 losing trades in a day" shape the live incident was
    # one trade short of.
    adapter = SpyAdapter([])
    cb = _circuit_breaker(tmp_path)
    wl = _loop(adapter, tmp_path, circuit_breaker=cb)

    for ticket in (1, 2, 3):
        _record_metadata(tmp_path, ticket=ticket, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
        adapter.closed_trade_info[ticket] = ClosedTradeInfo(
            close_price=2380.0, close_time=NOW, closed_volume=0.1, gross_pnl=-150.0, cost=3.0,
            exit_reason="stop_loss",
        )
        wl.run_cycle({}, NOW)

    state = _cb_state(tmp_path)
    assert state["consecutive_losses"] == 3
    assert state["consecutive_loss_halt_until"] is not None  # the halt actually tripped
    assert cb.check(FakeClock(NOW)).blocks_new_entries is True


def test_duplicate_close_write_does_not_double_count_circuit_breaker(tmp_path, monkeypatch, caplog):
    # Same crash-then-duplicate-write race as
    # test_duplicate_trade_record_write_does_not_also_double_notify -- the
    # circuit breaker must see this real-world close exactly once, same as
    # the TradeRecord/notify() guarantees it piggybacks on.
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", direction="BUY", entry_price=2395.0, stop_distance=5.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", direction="BUY")])
    adapter.close_result = OrderResult(
        success=True, broker_ticket=1, filled_price=2390.0, filled_volume=0.1,  # a loss
        retcode=None, message="closed",
    )
    wl = _loop(adapter, tmp_path)
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(
            action="CLOSE", new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        ),
    )

    real_remove_position_metadata = position_metadata.remove_position_metadata

    def _simulate_crash_after_explicit_close_write(ticket, state_path=None):
        raise RuntimeError("simulated crash between explicit-close write and metadata removal")

    monkeypatch.setattr(loop_module, "remove_position_metadata", _simulate_crash_after_explicit_close_write)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    state_after_first = _cb_state(tmp_path)
    assert state_after_first["consecutive_losses"] == 1

    # A later cycle: reconciliation re-observes the still-tracked ticket and
    # attempts to record the SAME close again -- swallowed as a duplicate.
    monkeypatch.setattr(loop_module, "remove_position_metadata", real_remove_position_metadata)
    adapter._open_positions = []
    adapter.closed_trade_info[1] = ClosedTradeInfo(
        close_price=2390.0, close_time=NOW, closed_volume=0.1, gross_pnl=-50.0, cost=0.0,
        exit_reason="stop_loss",
    )

    wl.run_cycle({}, NOW)  # must not raise despite the duplicate broker_ticket

    state_after_duplicate = _cb_state(tmp_path)
    assert state_after_duplicate["consecutive_losses"] == 1  # unchanged -- not double-counted
    assert state_after_duplicate["realized_pnl_today"] == state_after_first["realized_pnl_today"]

    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1


# --- Fix C (2026-07-25): orphan-position reconciliation ---------------------
# A broker position that IS open, carries this system's own magic, but has
# NO PositionMetadata at all -- the mirror gap of the reconciliation above
# (which only handles "metadata says open, broker says closed").


def test_orphan_position_with_matching_magic_seeds_metadata_and_alerts(tmp_path, monkeypatch):
    adapter = SpyAdapter([
        _position(ticket=50, symbol="XAUUSD", direction="BUY", volume=0.1,
                  current_sl=2395.0, current_price=2400.0, magic=OWN_MAGIC),
    ])
    wl = _loop(adapter, tmp_path)
    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    metadata = position_metadata.get_position_metadata(50, tmp_path / "position_metadata.json")
    assert metadata is not None
    assert metadata.symbol == "XAUUSD"
    assert metadata.direction == "BUY"
    assert metadata.entry_price == pytest.approx(2400.0)
    assert metadata.initial_stop_distance == pytest.approx(5.0)  # |2400.0 - 2395.0|
    # 2026-07-30: seeded metadata's true entry is unknown/unreliable -- must
    # be labeled distinctly from a normal-pipeline entry.
    assert metadata.entry_classification == "orphan_seeded"

    assert len(calls) == 1
    assert "50" in calls[0]
    assert "NO recorded entry-time metadata" in calls[0]

    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert any(e.event_type == "orphan_position_found" for e in events)


def test_orphan_position_with_non_matching_magic_is_left_alone(tmp_path, monkeypatch):
    # Not this system's own -- could be a genuine manual trade placed
    # directly in the terminal. Must NOT be seeded/managed/alerted on.
    adapter = SpyAdapter([
        _position(ticket=51, symbol="XAUUSD", direction="BUY", magic=999_999),
    ])
    wl = _loop(adapter, tmp_path)
    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert position_metadata.get_position_metadata(51, tmp_path / "position_metadata.json") is None
    assert calls == []
    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert not any(e.event_type == "orphan_position_found" for e in events)


def test_orphan_position_default_magic_zero_is_left_alone(tmp_path, monkeypatch):
    # _position()'s own default (magic=0, matching BrokerPosition's default
    # for an adapter that never populated it) must also be treated as "not
    # confirmed ours" -- never silently seeded/managed.
    adapter = SpyAdapter([_position(ticket=52, symbol="XAUUSD", direction="BUY")])
    wl = _loop(adapter, tmp_path)
    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert position_metadata.get_position_metadata(52, tmp_path / "position_metadata.json") is None
    assert calls == []


def test_orphan_reconciliation_skips_already_tracked_tickets(tmp_path, monkeypatch):
    _record_metadata(tmp_path, ticket=1, symbol="XAUUSD", entry_price=2395.0)
    adapter = SpyAdapter([_position(ticket=1, symbol="XAUUSD", magic=OWN_MAGIC)])
    wl = _loop(adapter, tmp_path)
    calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: calls.append(text))
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert calls == []
    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert not any(e.event_type == "orphan_position_found" for e in events)


def test_orphan_reconciliation_corrupt_metadata_store_skips_without_crashing(tmp_path, caplog):
    state_path = tmp_path / "position_metadata.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    adapter = SpyAdapter([_position(ticket=53, symbol="XAUUSD", magic=OWN_MAGIC)])
    wl = _loop(adapter, tmp_path)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert not any(e.event_type == "orphan_position_found" for e in events)


def test_orphan_reconciliation_one_ticket_raising_does_not_stop_others(tmp_path, monkeypatch, caplog):
    adapter = SpyAdapter([
        _position(ticket=60, symbol="XAUUSD", magic=OWN_MAGIC),
        _position(ticket=61, symbol="XAUUSD", magic=OWN_MAGIC),
    ])
    wl = _loop(adapter, tmp_path)

    real_record_position_opened = position_metadata.record_position_opened

    def _flaky_record(ticket, *args, **kwargs):
        if ticket == 60:
            raise RuntimeError("simulated failure seeding orphan metadata")
        return real_record_position_opened(ticket, *args, **kwargs)

    monkeypatch.setattr(loop_module, "record_position_opened", _flaky_record)

    with caplog.at_level(logging.ERROR):
        wl.run_cycle({"XAUUSD": _history()}, NOW)  # must not raise

    assert position_metadata.get_position_metadata(60, tmp_path / "position_metadata.json") is None
    assert position_metadata.get_position_metadata(61, tmp_path / "position_metadata.json") is not None
    assert any("unhandled exception seeding metadata" in r.message for r in caplog.records)


def test_position_explicitly_closed_this_cycle_is_not_mis_flagged_as_orphan(tmp_path, monkeypatch):
    # 2026-07-29 audit finding (real occurrence: ticket=1825965537,
    # 2026-07-28): a tracked position explicitly closed by Watchman DURING
    # this cycle (metadata correctly removed on close) still appears in the
    # START-of-cycle open_positions snapshot that _reconcile_orphan_positions
    # iterates -- it used to be mis-flagged as an orphan 8ms after its own
    # successful close: spurious CRITICAL alert + anomaly event, and
    # approximate metadata re-seeded for an already-CLOSED position. The
    # position must carry OWN_MAGIC for this to fire (magic=0 is skipped),
    # which is exactly why the older orphan tests never caught it.
    _record_metadata(tmp_path, ticket=77)
    adapter = SpyAdapter([_position(ticket=77, magic=OWN_MAGIC)])
    wl = _loop(adapter, tmp_path)
    notify_calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: notify_calls.append(text))
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(77, None)]  # the close itself happened
    assert position_metadata.get_position_metadata(77, tmp_path / "position_metadata.json") is None  # NOT re-seeded
    assert not any("NO recorded entry-time metadata" in text for text in notify_calls)
    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert not any(e.event_type == "orphan_position_found" for e in events)


def test_position_closed_by_news_protection_this_cycle_is_not_mis_flagged_as_orphan(tmp_path, monkeypatch):
    # Same false-orphan scenario via the OTHER explicit-close path
    # (news-protection CLOSE_ALL -- the exact path in the 2026-07-28 real
    # occurrence).
    _record_metadata(tmp_path, ticket=78)
    adapter = SpyAdapter([_position(ticket=78, magic=OWN_MAGIC)])
    wl = _loop(adapter, tmp_path)
    notify_calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: notify_calls.append(text))
    monkeypatch.setattr(
        loop_module, "check_news_protection",
        lambda **kwargs: NewsProtectionDecision(action="CLOSE_ALL", reason="calendar unavailable fail-safe"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert adapter.close_calls == [(78, None)]
    assert position_metadata.get_position_metadata(78, tmp_path / "position_metadata.json") is None
    assert not any("NO recorded entry-time metadata" in text for text in notify_calls)
    events = journal.get_anomaly_events_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert not any(e.event_type == "orphan_position_found" for e in events)


def test_closed_this_cycle_suppression_resets_between_cycles(tmp_path, monkeypatch):
    # The suppression must describe only the CURRENT cycle's own closes: a
    # ticket closed in cycle 1 whose id somehow reappears open with no
    # metadata in a LATER cycle's fresh snapshot is a genuine orphan again
    # and must still be flagged.
    _record_metadata(tmp_path, ticket=79)
    adapter = SpyAdapter([_position(ticket=79, magic=OWN_MAGIC)])
    wl = _loop(adapter, tmp_path)
    notify_calls = []
    monkeypatch.setattr(loop_module, "notify", lambda text: notify_calls.append(text))
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="CLOSE", new_stop_loss=None, reason="structure invalidation"),
    )

    wl.run_cycle({"XAUUSD": _history()}, NOW)  # cycle 1: explicit close, no orphan flag
    assert not any("NO recorded entry-time metadata" in text for text in notify_calls)

    # cycle 2: the same ticket shows up open again in a FRESH snapshot with
    # no metadata (e.g. the close was reversed/reopened broker-side) -- a
    # genuine orphan now.
    monkeypatch.setattr(
        loop_module, "evaluate_watchman",
        lambda **kwargs: WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no action"),
    )
    wl.run_cycle({"XAUUSD": _history()}, NOW)

    assert any("NO recorded entry-time metadata" in text for text in notify_calls)
    assert position_metadata.get_position_metadata(79, tmp_path / "position_metadata.json") is not None


def test_orphan_position_seeded_metadata_lets_reconciliation_capture_its_eventual_close_next_cycle(
    tmp_path, monkeypatch,
):
    # The actual bug Fix C closes: WITHOUT seeding metadata here, this
    # ticket would be invisible to _reconcile_closed_positions forever (it
    # only walks get_all_tracked_tickets()) -- its eventual close would
    # never produce a trade_records row at all.
    adapter = SpyAdapter([
        _position(ticket=70, symbol="XAUUSD", direction="BUY", volume=0.1,
                  current_sl=2395.0, current_price=2400.0, magic=OWN_MAGIC),
    ])
    wl = _loop(adapter, tmp_path)

    wl.run_cycle({"XAUUSD": _history()}, NOW)  # cycle 1: discovers + seeds metadata
    assert position_metadata.get_position_metadata(70, tmp_path / "position_metadata.json") is not None
    assert journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite") == []

    # cycle 2: the position has now genuinely closed (broker-side SL hit)
    adapter._open_positions = []
    adapter.closed_trade_info[70] = ClosedTradeInfo(
        close_price=2395.0, close_time=NOW, closed_volume=0.1,
        gross_pnl=-50.0, cost=1.0, exit_reason="stop_loss",
    )
    wl.run_cycle({}, NOW)

    assert position_metadata.get_position_metadata(70, tmp_path / "position_metadata.json") is None
    trades = journal.get_trades_for_day(NOW.date(), db_path=tmp_path / "trade_journal.sqlite")
    assert len(trades) == 1
    assert trades[0].broker_ticket == 70
    assert trades[0].exit_reason == "stop_loss"
