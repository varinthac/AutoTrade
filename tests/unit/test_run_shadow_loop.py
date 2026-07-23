"""Unit tests for scripts/run_shadow_loop.py -- the Phase 3d CLI entry point.
MT5 is mocked (same pattern as tests/unit/test_kill_switch_script.py); no live
terminal needed. scripts/ has no __init__.py, so the script is loaded
directly via importlib.

Prior to this test file, run_shadow_loop.py had zero direct test coverage --
only its downstream collaborator (orchestrator/shadow_loop.py) was tested.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autotrade.common import pid_file
from autotrade.common.clock import RealClock
from autotrade.common.config import MT5Credentials
from autotrade.common.mt5_time import ServerClock
from autotrade.execution.demo_adapter import ThrottledDemoAdapter
from autotrade.execution.noop_adapter import NoOpBrokerAdapter
from autotrade.risk.circuit_breaker import CircuitBreaker

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_shadow_loop.py"
_spec = importlib.util.spec_from_file_location("run_shadow_loop_script", SCRIPT_PATH)
run_shadow_loop = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_shadow_loop
_spec.loader.exec_module(run_shadow_loop)

mt5 = run_shadow_loop.mt5
CREDS = MT5Credentials(login=1, password="pw", server="srv", terminal_path=None)


# --- build_adapter() ------------------------------------------------------


def test_build_adapter_noop_returns_noop_broker_adapter():
    adapter = run_shadow_loop.build_adapter("noop", CREDS, RealClock())
    assert isinstance(adapter, NoOpBrokerAdapter)


def test_build_adapter_demo_returns_throttled_demo_adapter():
    adapter = run_shadow_loop.build_adapter("demo", CREDS, RealClock())
    assert isinstance(adapter, ThrottledDemoAdapter)


def test_build_adapter_demo_threads_journal_db_path_through():
    adapter = run_shadow_loop.build_adapter("demo", CREDS, RealClock(), journal_db_path="some/path.sqlite")
    assert adapter._journal_db_path == "some/path.sqlite"


def test_build_adapter_demo_threads_circuit_breaker_through():
    # 2026-07-23 fix: ThrottledDemoAdapter's own abnormal-slippage self-close
    # path needs the circuit breaker too (a third real P&L-bearing close
    # path, alongside WatchmanLoop's two) -- must not be silently dropped.
    cb = CircuitBreaker(daily_loss_limit_pct=2.0, max_consecutive_losses=3, max_drawdown_halt_pct=8.0)
    adapter = run_shadow_loop.build_adapter("demo", CREDS, RealClock(), circuit_breaker=cb)
    assert adapter._circuit_breaker is cb


def test_build_adapter_demo_circuit_breaker_defaults_to_none():
    adapter = run_shadow_loop.build_adapter("demo", CREDS, RealClock())
    assert adapter._circuit_breaker is None


def test_build_adapter_unknown_name_raises_value_error():
    with pytest.raises(ValueError, match="unknown"):
        run_shadow_loop.build_adapter("live", CREDS, RealClock())


# --- seed_history() --------------------------------------------------------


def _fake_rates(n: int, start_ts: int = 1_700_000_000):
    dtype = np.dtype([
        ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
    ])
    rows = [
        (start_ts + i * 3600, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10, 1, 0)
        for i in range(n)
    ]
    return np.array(rows, dtype=dtype)


def test_seed_history_converts_rates_to_dataframe_with_datetime_time_column(monkeypatch):
    monkeypatch.setattr(mt5, "copy_rates_from_pos", lambda symbol, tf, start, count: _fake_rates(5))

    df = run_shadow_loop.seed_history("XAUUSD", "H1", 5, {"XAUUSD": "XAUUSD"})

    assert len(df) == 5
    assert pd.api.types.is_datetime64_any_dtype(df["time"])
    assert list(df["close"]) == [100.5, 101.5, 102.5, 103.5, 104.5]


def test_seed_history_raises_when_copy_rates_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "copy_rates_from_pos", lambda symbol, tf, start, count: None)
    monkeypatch.setattr(mt5, "last_error", lambda: (1, "no connection"))

    with pytest.raises(RuntimeError, match="copy_rates_from_pos"):
        run_shadow_loop.seed_history("XAUUSD", "H1", 5, {"XAUUSD": "XAUUSD"})


def test_seed_history_raises_when_copy_rates_returns_empty(monkeypatch):
    monkeypatch.setattr(mt5, "copy_rates_from_pos", lambda symbol, tf, start, count: _fake_rates(0))
    monkeypatch.setattr(mt5, "last_error", lambda: (2, "no data"))

    with pytest.raises(RuntimeError, match="copy_rates_from_pos"):
        run_shadow_loop.seed_history("XAUUSD", "H1", 5, {"XAUUSD": "XAUUSD"})


def test_seed_history_uses_broker_mapped_symbol_name(monkeypatch):
    captured = {}

    def fake_copy_rates(symbol, tf, start, count):
        captured["symbol"] = symbol
        return _fake_rates(3)

    monkeypatch.setattr(mt5, "copy_rates_from_pos", fake_copy_rates)

    run_shadow_loop.seed_history("XAUUSD", "H1", 3, {"XAUUSD": "XAUUSD.a"})

    assert captured["symbol"] == "XAUUSD.a"


# --- _read_autotrading_state() ----------------------------------------------


def test_read_autotrading_state_returns_none_when_terminal_info_returns_none(monkeypatch):
    monkeypatch.setattr(mt5, "terminal_info", lambda: None)

    assert run_shadow_loop._read_autotrading_state() is None


def test_read_autotrading_state_returns_trade_allowed_field(monkeypatch):
    class _FakeTerminalInfo:
        trade_allowed = False

    monkeypatch.setattr(mt5, "terminal_info", lambda: _FakeTerminalInfo())

    assert run_shadow_loop._read_autotrading_state() is False


# --- main() wiring ----------------------------------------------------------


@contextmanager
def _fake_session(creds):
    yield


_BASE_CFG = {
    "symbols": {"XAUUSD": "XAUUSD"},
    "global": {"timeframe": "H1", "swing_pivot_bars": 7},
    "cfo": {
        "risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
        "max_consecutive_losses": 3, "max_drawdown_halt_pct": 8.0,
        "min_lot_risk_cap_pct": 1.5,
    },
    "order": {"sl_buffer_atr": 0.2, "sl_min_atr": 0.8, "sl_max_atr": 2.5, "tp_r_multiple": 2.0},
    "shield": {
        "min_rr": 1.5, "max_correlation": 0.7, "max_positions_per_symbol": 1,
        "max_positions_total": 3, "total_risk_ceiling_pct": 3.0,
        "duplicate_signal_cooldown_hours": 4.0,
    },
    "council": {"bull_threshold": 70, "bear_threshold": 70, "conflict_threshold": 55},
    "risk_voice": {
        "max_spread_multiple": 1.5, "max_spread_points_xauusd": 35,
        "news_blackout_before_min": 45, "news_blackout_after_min": 30,
        "max_stop_atr_multiple": 2.5, "session_start_hour": 14, "session_end_hour": 18,
        "friday_close_hour": 20, "max_atr_panic_multiple": 3.0,
    },
    "watchman": {
        "breakeven_at_r": 1.0, "trail_start_r": 1.5, "trail_distance_atr": 1.0,
        "time_stop_hours": 48, "dead_trade_r_band": 0.3, "news_window_minutes": 30,
        "news_profit_threshold_r": 0.5, "news_close_mode": "half", "connectivity_timeout_minutes": 5,
        "breakeven_enabled": False, "trail_enabled": False,
    },
}


def _patch_common_main_wiring(monkeypatch, tmp_path) -> None:
    """Everything test_main_*() needs mocked EXCEPT load_finnhub_api_key
    (each test controls that one directly, since it's what decides which
    news_provider gets wired in). The PID file is redirected to tmp_path --
    same "never let a test touch data/db/'s real files" rule as
    tests/conftest.py's trade-journal isolation -- so main()'s double-launch
    guard/PID-file write+remove never touches the repo's real
    data/db/shadow_loop.pid."""
    monkeypatch.setattr(sys, "argv", ["run_shadow_loop.py", "--adapter", "noop", "--max-iterations", "1"])
    monkeypatch.setattr(run_shadow_loop, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(run_shadow_loop, "load_yaml_config", lambda name: _BASE_CFG)
    monkeypatch.setattr(run_shadow_loop, "mt5_session", _fake_session)
    monkeypatch.setattr(run_shadow_loop, "seed_history", lambda symbol, timeframe, bars, symbol_map: pd.DataFrame({
        "time": [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0],
    }))
    monkeypatch.setattr(pid_file, "DEFAULT_PID_PATH", tmp_path / "shadow_loop.pid")
    monkeypatch.setattr(run_shadow_loop, "LOG_DIR", tmp_path / "logs")


def test_main_wires_noop_adapter_and_runs_shadow_loop_for_configured_symbols(monkeypatch, tmp_path):
    """Full integration of main()'s wiring, with MT5/credentials/config all
    mocked -- verifies the CLI actually builds a ShadowLoop for every
    configured symbol and calls .run() with the CLI's own args, rather than
    silently doing nothing or crashing before it gets there."""
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    run_calls = {}

    class _FakeShadowLoop:
        def __init__(self, **kwargs):
            run_calls["init_kwargs"] = kwargs

        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            run_calls["run_args"] = {
                "symbols": symbols, "timeframe": timeframe,
                "poll_interval_sec": poll_interval_sec, "max_iterations": max_iterations,
            }

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert run_calls["run_args"]["symbols"] == ["XAUUSD"]
    assert run_calls["run_args"]["timeframe"] == "H1"
    assert run_calls["run_args"]["max_iterations"] == 1
    assert isinstance(run_calls["init_kwargs"]["adapter"], NoOpBrokerAdapter)
    # The clock fed to ShadowLoop (circuit-breaker/daily-loss reset) must be
    # MT5 server time, not wall-clock RealClock (spec.md Appendix A §0).
    assert isinstance(run_calls["init_kwargs"]["clock"], ServerClock)
    assert run_calls["init_kwargs"]["clock"]._reference_symbol_broker_name == "XAUUSD"
    assert run_calls["init_kwargs"]["circuit_breaker"]._state_path == run_shadow_loop.DEFAULT_STATE_PATH
    # 2026-07-23 fix: WatchmanLoop must be wired to the SAME CircuitBreaker
    # instance ShadowLoop itself uses (not a second, separately-tracked one)
    # -- otherwise consecutive-loss/daily-P&L state would silently split
    # across two trackers that never agree.
    assert run_calls["init_kwargs"]["watchman_loop"]._circuit_breaker is run_calls["init_kwargs"]["circuit_breaker"]
    assert isinstance(run_calls["init_kwargs"]["shield"], run_shadow_loop.Shield)
    # Phase 6b: news_provider/risk_voice_cfg are wired in too -- with no
    # FINNHUB_API_KEY configured (mocked as None above), build_news_provider()
    # falls back to the stub. See test_main_uses_finnhub_provider_when_api_key_configured
    # below for the FINNHUB_API_KEY-set path.
    assert isinstance(run_calls["init_kwargs"]["news_provider"], run_shadow_loop.StubNewsCalendarProvider)
    risk_voice_cfg = run_calls["init_kwargs"]["risk_voice_cfg"]
    assert isinstance(risk_voice_cfg, run_shadow_loop.RiskVoiceConfig)
    assert risk_voice_cfg.session_start_hour == 14
    assert risk_voice_cfg.session_end_hour == 18
    assert run_calls["init_kwargs"]["cfg"].bull_threshold == 70
    # pivot_bars is read from config/base.yaml's global.swing_pivot_bars, not
    # silently defaulting to ShadowLoopConfig's own pivot_bars=3 default
    # regardless of config -- _BASE_CFG's swing_pivot_bars=7 is a distinct
    # value specifically to catch that.
    assert run_calls["init_kwargs"]["cfg"].pivot_bars == 7
    # min_lot_risk_cap_pct is read from config/base.yaml's cfo.min_lot_risk_cap_pct
    # into the real ShadowLoopConfig -- _BASE_CFG's value (1.5) is distinct
    # from ShadowLoopConfig's own None default specifically to catch a
    # silently-ignored field.
    assert run_calls["init_kwargs"]["cfg"].min_lot_risk_cap_pct == 1.5
    # breakeven_enabled/trail_enabled are read from config/base.yaml's
    # watchman: block into the real WatchmanConfig wired into WatchmanLoop --
    # _BASE_CFG sets both False (distinct from WatchmanConfig's own True
    # default) specifically to catch a silently-ignored field.
    watchman_config = run_calls["init_kwargs"]["watchman_loop"]._watchman_config
    assert watchman_config.breakeven_enabled is False
    assert watchman_config.trail_enabled is False
    # Phase 9: --mode defaults to "paper" -- trades from a plain
    # `AutoTrade_Start.bat` run must land in the DB run_auditor.py's
    # `--mode paper` gates actually read, not the generic default DB.
    assert run_calls["init_kwargs"]["journal_db_path"] == run_shadow_loop.DEFAULT_PAPER_DB_PATH
    assert run_calls["init_kwargs"]["watchman_loop"]._journal_db_path == run_shadow_loop.DEFAULT_PAPER_DB_PATH
    # AutoTradingWatchdog is actually constructed and threaded into ShadowLoop
    # (not just trusted) -- same server-clock/journal_db_path wiring as
    # ConnectivityWatchdog above, plus the small MT5-touching reader callable.
    autotrading_watchdog = run_calls["init_kwargs"]["autotrading_watchdog"]
    assert isinstance(autotrading_watchdog, run_shadow_loop.AutoTradingWatchdog)
    assert autotrading_watchdog._journal_clock is run_calls["init_kwargs"]["clock"]
    assert autotrading_watchdog._journal_db_path == run_shadow_loop.DEFAULT_PAPER_DB_PATH
    assert run_calls["init_kwargs"]["read_autotrading_state"] is run_shadow_loop._read_autotrading_state


def test_main_mode_live_routes_journal_writes_to_the_live_db(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_shadow_loop.py", "--adapter", "noop", "--max-iterations", "1", "--mode", "live",
    ])
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    run_calls = {}

    class _FakeShadowLoop:
        def __init__(self, **kwargs):
            run_calls["init_kwargs"] = kwargs

        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            pass

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert run_calls["init_kwargs"]["journal_db_path"] == run_shadow_loop.DEFAULT_LIVE_DB_PATH
    assert run_calls["init_kwargs"]["watchman_loop"]._journal_db_path == run_shadow_loop.DEFAULT_LIVE_DB_PATH


def test_main_mode_demo_adapter_also_gets_the_resolved_journal_db_path(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_shadow_loop.py", "--adapter", "demo", "--max-iterations", "1", "--mode", "live",
    ])
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    run_calls = {}

    class _FakeShadowLoop:
        def __init__(self, **kwargs):
            run_calls["init_kwargs"] = kwargs

        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            pass

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert run_calls["init_kwargs"]["adapter"]._journal_db_path == run_shadow_loop.DEFAULT_LIVE_DB_PATH
    # 2026-07-23 fix: the real ThrottledDemoAdapter (--adapter demo) must be
    # wired to the SAME CircuitBreaker instance as WatchmanLoop and ShadowLoop
    # itself -- not a second, separately-tracked one (same identity guarantee
    # already checked for the noop-adapter case above).
    assert run_calls["init_kwargs"]["adapter"]._circuit_breaker is run_calls["init_kwargs"]["circuit_breaker"]
    assert run_calls["init_kwargs"]["watchman_loop"]._circuit_breaker is run_calls["init_kwargs"]["circuit_breaker"]


def test_main_uses_finnhub_provider_when_api_key_configured(monkeypatch, tmp_path):
    """When FINNHUB_API_KEY is set (and the MQL5 calendar path can't be
    resolved -- no active MT5 session in this test), main() wires in
    FinnhubNewsCalendarProvider instead of the always-vetoing stub."""
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: "fake-key")

    run_calls = {}

    class _FakeShadowLoop:
        def __init__(self, **kwargs):
            run_calls["init_kwargs"] = kwargs

        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            pass

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert isinstance(run_calls["init_kwargs"]["news_provider"], run_shadow_loop.FinnhubNewsCalendarProvider)


def test_main_prefers_mql5_provider_over_finnhub_when_commondata_path_resolves(monkeypatch, tmp_path):
    """MQL5CalendarProvider is the top-priority candidate (see
    build_news_provider's docstring) -- even with FINNHUB_API_KEY also
    configured, main() wires in MQL5CalendarProvider whenever
    resolve_commondata_path() (an active MT5 session) succeeds."""
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: "fake-key")
    monkeypatch.setattr(run_shadow_loop, "resolve_commondata_path", lambda: r"C:\fake\Terminal\Common")

    run_calls = {}

    class _FakeShadowLoop:
        def __init__(self, **kwargs):
            run_calls["init_kwargs"] = kwargs

        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            pass

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert isinstance(run_calls["init_kwargs"]["news_provider"], run_shadow_loop.MQL5CalendarProvider)


# --- build_news_provider() -- direct, isolated tests of the priority chain -


def test_build_news_provider_selects_mql5_even_when_export_file_not_yet_written(monkeypatch, tmp_path):
    """The exact intermediate case flagged for review: `resolve_commondata_path()`
    resolves (an MT5 session is active) but the `NewsCalendarExporter.mq5`
    Service hasn't been started yet, so no export file exists under
    `tmp_path/Files/` at all. Per `build_news_provider`'s own docstring,
    MQL5CalendarProvider must still be SELECTED (not skipped in favor of
    Finnhub/stub) -- it fails safe (None) on every call until the Service
    starts, which is the intended "select now, it may start working later"
    semantics, not "select only once proven to already have data". This
    uses a real tmp_path (not a fake nonexistent Windows path) so the
    "file genuinely absent" condition is real filesystem behavior, not an
    assumption."""
    monkeypatch.setattr(run_shadow_loop, "resolve_commondata_path", lambda: str(tmp_path))
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    provider = run_shadow_loop.build_news_provider(RealClock())

    assert isinstance(provider, run_shadow_loop.MQL5CalendarProvider)
    # And it genuinely fails safe (None), not an exception or stale/empty data,
    # confirming "selected" does not mean "already functional".
    assert provider.get_high_impact_events(
        "USD", datetime(2026, 1, 1), datetime(2026, 1, 2),
    ) is None


def test_build_news_provider_selects_mql5_over_finnhub_even_without_finnhub_key(monkeypatch, tmp_path):
    """MQL5's priority over Finnhub must not depend on whether a Finnhub key
    happens to be configured at all -- distinct from
    test_main_prefers_mql5_provider_over_finnhub_when_commondata_path_resolves
    (which only covers the "Finnhub key IS configured" combination); this
    covers the remaining combination (commondata_path resolves, no Finnhub
    key present) directly against build_news_provider(), without needing
    the full main() wiring."""
    monkeypatch.setattr(run_shadow_loop, "resolve_commondata_path", lambda: str(tmp_path))
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    provider = run_shadow_loop.build_news_provider(RealClock())

    assert isinstance(provider, run_shadow_loop.MQL5CalendarProvider)


def test_build_news_provider_falls_back_to_stub_when_neither_mql5_nor_finnhub_available(monkeypatch):
    monkeypatch.setattr(run_shadow_loop, "resolve_commondata_path", lambda: None)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    provider = run_shadow_loop.build_news_provider(RealClock())

    assert isinstance(provider, run_shadow_loop.StubNewsCalendarProvider)


def test_main_unknown_adapter_choice_rejected_by_argparse(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_shadow_loop.py", "--adapter", "live"])
    monkeypatch.setattr(run_shadow_loop, "LOG_DIR", tmp_path / "logs")

    with pytest.raises(SystemExit):
        run_shadow_loop.main()


# --- Start/stop workflow: startup notify + PID double-launch guard ----------


class _FakeShadowLoop:
    def __init__(self, **kwargs):
        pass

    def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
        pass


def test_main_sends_startup_notify_after_mt5_connects(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)
    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)
    calls = []
    monkeypatch.setattr(run_shadow_loop, "notify", lambda text: calls.append(text))

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert len(calls) == 1
    assert "started" in calls[0]
    assert "noop" in calls[0]
    assert "XAUUSD" in calls[0]


def test_main_double_launch_guard_proceeds_when_no_pid_file(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)
    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)

    assert pid_file.read() is None
    exit_code = run_shadow_loop.main()

    assert exit_code == 0


def test_main_double_launch_guard_refuses_when_pid_is_alive(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)
    pid_file.write(4242)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: pid == 4242)

    mt5_session_calls = []
    monkeypatch.setattr(run_shadow_loop, "mt5_session", lambda creds: mt5_session_calls.append(creds) or _fake_session(creds))
    notify_calls = []
    monkeypatch.setattr(run_shadow_loop, "notify", lambda text: notify_calls.append(text))

    exit_code = run_shadow_loop.main()

    assert exit_code == 1
    assert mt5_session_calls == []  # never even attempted a second MT5 connection
    assert len(notify_calls) == 1
    assert "already running" in notify_calls[0]
    assert "4242" in notify_calls[0]
    assert pid_file.read() == 4242  # untouched -- refused before overwriting it


def test_main_double_launch_guard_proceeds_and_overwrites_stale_pid(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)
    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)
    pid_file.write(9999)
    monkeypatch.setattr(pid_file, "is_pid_running", lambda pid: False)  # stale -- not actually running

    exit_code = run_shadow_loop.main()

    assert exit_code == 0


def test_main_writes_pid_file_during_run_and_removes_it_on_clean_exit(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    written_pid_seen = {}

    class _ObservingShadowLoop(_FakeShadowLoop):
        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            written_pid_seen["pid"] = pid_file.read()  # PID file must exist WHILE the loop runs

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _ObservingShadowLoop)

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
    assert written_pid_seen["pid"] == os.getpid()
    assert pid_file.read() is None  # removed once main() returns (the stop-flag-triggered exit path)


def test_main_removes_pid_file_even_when_run_raises_keyboard_interrupt(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    class _InterruptingShadowLoop(_FakeShadowLoop):
        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            raise KeyboardInterrupt()

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _InterruptingShadowLoop)

    with pytest.raises(KeyboardInterrupt):
        run_shadow_loop.main()

    assert pid_file.read() is None  # finally still removed it despite the interrupt


def test_main_removes_pid_file_even_when_run_raises_unhandled_exception(monkeypatch, tmp_path):
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)

    class _CrashingShadowLoop(_FakeShadowLoop):
        def run(self, symbols, timeframe, poll_interval_sec, max_iterations):
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _CrashingShadowLoop)

    with pytest.raises(RuntimeError):
        run_shadow_loop.main()

    assert pid_file.read() is None


# --- _configure_logging() -- durable log file alongside console output ------


def test_configure_logging_creates_a_log_file_in_the_expected_location(tmp_path):
    log_dir = tmp_path / "logs"

    run_shadow_loop._configure_logging(log_dir)

    log_files = list(log_dir.glob("shadow_loop_*.log"))
    assert len(log_files) == 1


def test_configure_logging_creates_the_log_directory_if_missing(tmp_path):
    log_dir = tmp_path / "does_not_exist_yet"
    assert not log_dir.exists()

    run_shadow_loop._configure_logging(log_dir)

    assert log_dir.is_dir()


def test_configure_logging_console_handler_still_present_and_file_receives_records(tmp_path):
    """Adding the FileHandler must not come at the expense of the console
    output operators watching the shadow loop's console window rely on --
    both handlers must be attached, and a record logged through the module's
    logger must reach the file (proving it's actually wired up, not just
    present)."""
    log_dir = tmp_path / "logs"

    run_shadow_loop._configure_logging(log_dir)

    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in handlers)
    assert any(isinstance(h, logging.FileHandler) for h in handlers)

    logging.getLogger(run_shadow_loop.__name__).info("integration marker line")
    for handler in handlers:
        handler.flush()

    log_file = next(log_dir.glob("shadow_loop_*.log"))
    assert "integration marker line" in log_file.read_text(encoding="utf-8")


class _UnwritableLogDir:
    """Stand-in for an unwritable/uncreatable logs/ directory (e.g. a fresh
    machine with a permissions problem) -- duck-types the one method
    _configure_logging() calls before it would otherwise touch the
    filesystem."""

    def mkdir(self, parents=True, exist_ok=True):
        raise OSError("simulated: logs directory unwritable")


def test_configure_logging_falls_back_to_console_only_when_log_dir_unwritable():
    run_shadow_loop._configure_logging(_UnwritableLogDir())

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], logging.FileHandler)


def test_main_continues_running_when_log_directory_is_unwritable(monkeypatch, tmp_path):
    """The most important test here: a logging setup problem (an unwritable
    logs/ directory) must never prevent the shadow loop from actually
    running/trading."""
    _patch_common_main_wiring(monkeypatch, tmp_path)
    monkeypatch.setattr(run_shadow_loop, "load_finnhub_api_key", lambda: None)
    monkeypatch.setattr(run_shadow_loop, "ShadowLoop", _FakeShadowLoop)
    monkeypatch.setattr(run_shadow_loop, "LOG_DIR", _UnwritableLogDir())

    exit_code = run_shadow_loop.main()

    assert exit_code == 0
