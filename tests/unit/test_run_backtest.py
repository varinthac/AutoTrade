"""Unit tests for scripts/run_backtest.py -- MT5-free (symbol_spec/df are
passed in directly), same importlib-loading convention as
tests/unit/test_kill_switch_script.py (scripts/ has no __init__.py)."""
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

from autotrade.auditor.backtest_results import load_backtest_report_envelope
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.common.config import MT5Credentials
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.council.scoring import BullBearScore
from autotrade.shield.checkpoint import ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_backtest.py"
_spec = importlib.util.spec_from_file_location("run_backtest_script", SCRIPT_PATH)
run_backtest_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_backtest_script
_spec.loader.exec_module(run_backtest_script)

SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def _bars(rows: list[dict]) -> pd.DataFrame:
    times = pd.date_range("2026-01-06 00:00:00", periods=len(rows), freq="h")
    return pd.DataFrame([{"time": t, **row} for t, row in zip(times, rows)])


def _score(total: int) -> BullBearScore:
    return BullBearScore(
        score=total, trend_alignment=0, momentum_rsi=0, momentum_macd=0, market_structure=0, confluence=0
    )


def _council_signal_bars(n: int = 40) -> pd.DataFrame:
    """Flat OHLC with a confirmed swing low at index 10 -- mirrors
    tests/unit/backtest/test_engine.py's own fixture for the same purpose."""
    times = pd.date_range("2026-01-06 00:00:00", periods=n, freq="h")
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    lows[10] = 90.0
    return pd.DataFrame({
        "time": times, "open": closes, "high": highs, "low": lows, "close": closes,
        "spread": [5] * n,
    })


# --- filter_by_date_range -------------------------------------------------


def test_filter_by_date_range_start_only_keeps_bars_at_or_after():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(5)])
    # times: 2026-01-06 00:00, 01:00, 02:00, 03:00, 04:00
    filtered = run_backtest_script.filter_by_date_range(df, "2026-01-06 02:00:00", None)

    assert len(filtered) == 3
    assert filtered["time"].iloc[0] == df["time"].iloc[2]


def test_filter_by_date_range_end_only_is_exclusive():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(5)])
    filtered = run_backtest_script.filter_by_date_range(df, None, "2026-01-06 02:00:00")

    assert len(filtered) == 2  # bars at 00:00, 01:00 -- 02:00 itself excluded
    assert filtered["time"].iloc[-1] == df["time"].iloc[1]


def test_filter_by_date_range_both_bounds_narrows_to_the_slice():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(5)])
    filtered = run_backtest_script.filter_by_date_range(df, "2026-01-06 01:00:00", "2026-01-06 03:00:00")

    assert len(filtered) == 2
    assert list(filtered["time"]) == [df["time"].iloc[1], df["time"].iloc[2]]


def test_filter_by_date_range_no_bounds_returns_everything():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(3)])
    filtered = run_backtest_script.filter_by_date_range(df, None, None)

    assert len(filtered) == 3


def test_filter_by_date_range_out_of_range_returns_empty_not_an_error():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(3)])
    filtered = run_backtest_script.filter_by_date_range(df, "2099-01-01", None)

    assert filtered.empty


def test_filter_by_date_range_returns_reindexed_copy_does_not_mutate_input():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5} for _ in range(5)])
    original_len = len(df)

    filtered = run_backtest_script.filter_by_date_range(df, "2026-01-06 02:00:00", None)

    assert len(df) == original_len  # input untouched
    assert list(filtered.index) == list(range(len(filtered)))  # reindexed from 0


def test_build_envelope_cost_model_complete_true_when_commission_set_and_min_spread_convention():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=3.5, slippage_points=None),
        10_000.0, False, risk_voice_modeled=True, watchman_exits_modeled=True,
        shield_modeled=True, min_lot_risk_cap_pct=None,
    )
    assert envelope["cost_model_complete"] is True


def test_build_envelope_cost_model_complete_true_when_commission_zero_and_min_spread_convention():
    # 0.0 is a legitimate real commission for a commission-free account (e.g.
    # IC Markets Standard, which recovers cost via a wider spread instead) --
    # it must NOT be treated as an unconfigured placeholder by build_envelope
    # itself. The "was this consciously chosen" safeguard lives in main()'s
    # required --commission-per-lot CLI argument, not in this value check.
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=0.0, slippage_points=None),
        10_000.0, False, risk_voice_modeled=True, watchman_exits_modeled=True,
        shield_modeled=True, min_lot_risk_cap_pct=None,
    )
    assert envelope["cost_model_complete"] is True


def test_build_envelope_cost_model_complete_false_when_slippage_explicitly_overridden():
    # An explicit slippage_points override isn't guaranteed >= 1 spread --
    # cost_model_complete should not credit it as the min-1-spread convention.
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=3.5, slippage_points=2.0),
        10_000.0, False, risk_voice_modeled=True, watchman_exits_modeled=True,
        shield_modeled=True, min_lot_risk_cap_pct=None,
    )
    assert envelope["cost_model_complete"] is False


def test_run_and_persist_writes_a_loadable_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"

    out_path = run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir,
    )

    assert out_path.exists()
    envelope = load_backtest_report_envelope(out_path)
    assert envelope.symbol == "XAUUSD"
    assert envelope.report.trade_count == 1
    assert envelope.cost_model_complete is True
    assert envelope.risk_voice_modeled is False  # risk_voice_cfg omitted -> not modeled
    assert envelope.watchman_exits_modeled is False  # watchman_cfg omitted -> not modeled
    assert envelope.shield_modeled is False  # shield_cfg omitted -> not modeled
    assert envelope.min_lot_risk_cap_pct is None  # min_lot_risk_cap_pct omitted -> fallback disabled

    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert raw["bar_range"]["start"] == str(df["time"].iloc[0])
    assert raw["bar_range"]["end"] == str(df["time"].iloc[-1])


def test_run_and_persist_risk_voice_cfg_marks_envelope_as_modeled(tmp_path, monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"
    permissive_risk_voice_cfg = RiskVoiceConfig(
        max_spread_multiple=1e9, max_spread_points_xauusd=1e9,
        max_stop_atr_multiple=1e9, session_start_hour=0, session_end_hour=24,
        friday_close_hour=24, max_atr_panic_multiple=1e9,
    )

    out_path = run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir, risk_voice_cfg=permissive_risk_voice_cfg,
    )

    envelope = load_backtest_report_envelope(out_path)
    assert envelope.risk_voice_modeled is True


def test_run_and_persist_watchman_cfg_marks_envelope_as_modeled(tmp_path, monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"

    out_path = run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir, watchman_cfg=WatchmanConfig(),
    )

    envelope = load_backtest_report_envelope(out_path)
    assert envelope.watchman_exits_modeled is True


def test_run_and_persist_shield_cfg_marks_envelope_as_modeled(tmp_path, monkeypatch):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"

    out_path = run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir, shield_cfg=ShieldConfig(),
    )

    envelope = load_backtest_report_envelope(out_path)
    assert envelope.shield_modeled is True


def test_run_and_persist_threads_pivot_bars_into_backtest_config(tmp_path, monkeypatch):
    # Proves run_and_persist's pivot_bars= kwarg reaches BacktestConfig
    # itself (not just relying on BacktestConfig's own pivot_bars=3
    # default), by capturing the constructed config's field directly.
    captured = {}
    real_run_backtest = run_backtest_script.run_backtest

    def _capturing_run_backtest(df, symbol, symbol_spec, config):
        captured["pivot_bars"] = config.pivot_bars
        return real_run_backtest(df, symbol, symbol_spec, config)

    monkeypatch.setattr(run_backtest_script, "run_backtest", _capturing_run_backtest)
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"

    run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir, pivot_bars=7,
    )

    assert captured["pivot_bars"] == 7


def test_run_and_persist_threads_min_lot_risk_cap_pct_into_backtest_config_and_envelope(tmp_path, monkeypatch):
    # Proves run_and_persist's min_lot_risk_cap_pct= kwarg reaches
    # BacktestConfig itself (not just added to the dataclass and silently
    # ignored) AND is recorded in the written envelope for auditability.
    captured = {}
    real_run_backtest = run_backtest_script.run_backtest

    def _capturing_run_backtest(df, symbol, symbol_spec, config):
        captured["min_lot_risk_cap_pct"] = config.min_lot_risk_cap_pct
        return real_run_backtest(df, symbol, symbol_spec, config)

    monkeypatch.setattr(run_backtest_script, "run_backtest", _capturing_run_backtest)
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    df = _council_signal_bars()
    output_dir = tmp_path / "backtest_reports"

    out_path = run_backtest_script.run_and_persist(
        "XAUUSD", df, SYMBOL, 10_000.0, 1.0,
        CostModelConfig(commission_per_lot=2.0, slippage_points=None),
        False, output_dir, min_lot_risk_cap_pct=1.5,
    )

    assert captured["min_lot_risk_cap_pct"] == 1.5
    envelope = load_backtest_report_envelope(out_path)
    assert envelope.min_lot_risk_cap_pct == 1.5


# --- main() end-to-end wiring -----------------------------------------------
#
# The tests above exercise build_envelope/run_and_persist/_council_signal_fn
# in isolation -- none of them go through main()'s own argv parsing +
# config/base.yaml["risk_voice"] key-by-key mapping into RiskVoiceConfig.
# A typo'd YAML key there (e.g. "max_atr_panic_multiplier") would only raise
# a KeyError at actual CLI invocation, never in CI, without a test like this.


CREDS = MT5Credentials(login=1, password="pw", server="srv", terminal_path=None)


@contextmanager
def _fake_session(creds):
    yield


_FAKE_MAIN_CFG = {
    "global": {"swing_pivot_bars": 7},
    "cfo": {"risk_per_trade_pct": 0.75, "min_lot_risk_cap_pct": 1.25},
    "risk_voice": {
        "max_spread_multiple": 2.5, "max_spread_points_xauusd": 40.0,
        "news_blackout_before_min": 10.0, "news_blackout_after_min": 5.0,
        "max_stop_atr_multiple": 3.5, "session_start_hour": 1, "session_end_hour": 23,
        "friday_close_hour": 21, "max_atr_panic_multiple": 4.5,
    },
    "watchman": {
        "breakeven_at_r": 1.5, "trail_start_r": 2.0, "trail_distance_atr": 1.2,
        "time_stop_hours": 36.0, "dead_trade_r_band": 0.25,
        "breakeven_enabled": False, "trail_enabled": False,
    },
    "shield": {
        "min_rr": 1.8, "max_correlation": 0.6, "max_positions_per_symbol": 2,
        "max_positions_total": 5, "total_risk_ceiling_pct": 4.5,
        "duplicate_signal_cooldown_hours": 6.0,
    },
}


def _patch_main_wiring(monkeypatch, tmp_path, csv_df: pd.DataFrame) -> None:
    historical_dir = tmp_path / "historical"
    historical_dir.mkdir()
    csv_df.to_csv(historical_dir / "XAUUSD_H1.csv", index=False)
    monkeypatch.setattr(run_backtest_script, "HISTORICAL_DIR", historical_dir)
    monkeypatch.setattr(run_backtest_script, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(run_backtest_script, "mt5_session", _fake_session)
    monkeypatch.setattr(run_backtest_script, "get_symbol_spec", lambda symbol: SYMBOL)
    monkeypatch.setattr(run_backtest_script, "load_yaml_config", lambda name: _FAKE_MAIN_CFG)


def test_main_requires_commission_per_lot_argument(monkeypatch, tmp_path):
    # 0.0 is a legitimate real commission (e.g. a commission-free "Standard"
    # account) so it cannot be a silent argparse default -- the caller must
    # always pass this flag explicitly, even to choose 0.0.
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD"])

    with pytest.raises(SystemExit):
        run_backtest_script.main()


def test_main_constructs_risk_voice_cfg_from_config_with_every_field_mapped_correctly(monkeypatch, tmp_path):
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0"])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["risk_voice_cfg"] = kwargs.get("risk_voice_cfg")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    rv = captured["risk_voice_cfg"]
    assert rv is not None
    # Every field checked individually against a DISTINCT fake config value
    # -- a KeyError, a swapped/misassigned field, or a silently-ignored one
    # would fail at least one of these, not just "did it run without error".
    assert rv.max_spread_multiple == 2.5
    assert rv.max_spread_points_xauusd == 40.0
    assert rv.news_blackout_before_min == 10.0
    assert rv.news_blackout_after_min == 5.0
    assert rv.max_stop_atr_multiple == 3.5
    assert rv.session_start_hour == 1
    assert rv.session_end_hour == 23
    assert rv.friday_close_hour == 21
    assert rv.max_atr_panic_multiple == 4.5


def test_main_constructs_watchman_cfg_from_config_with_every_field_mapped_correctly(monkeypatch, tmp_path):
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0"])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["watchman_cfg"] = kwargs.get("watchman_cfg")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    wc = captured["watchman_cfg"]
    assert wc is not None
    # Every field checked individually against a DISTINCT fake config value --
    # a KeyError, a swapped/misassigned field, or a silently-ignored one would
    # fail at least one of these, not just "did it run without error".
    assert wc.breakeven_at_r == 1.5
    assert wc.trail_start_r == 2.0
    assert wc.trail_distance_atr == 1.2
    assert wc.time_stop_hours == 36.0
    assert wc.dead_trade_r_band == 0.25
    assert wc.breakeven_enabled is False
    assert wc.trail_enabled is False


def test_main_constructs_shield_cfg_from_config_with_every_field_mapped_correctly(monkeypatch, tmp_path):
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0"])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["shield_cfg"] = kwargs.get("shield_cfg")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    sc = captured["shield_cfg"]
    assert sc is not None
    # Every field checked individually against a DISTINCT fake config value --
    # a KeyError, a swapped/misassigned field, or a silently-ignored one would
    # fail at least one of these, not just "did it run without error".
    assert sc.min_rr == 1.8
    assert sc.max_correlation == 0.6
    assert sc.max_positions_per_symbol == 2
    assert sc.max_positions_total == 5
    assert sc.total_risk_ceiling_pct == 4.5
    assert sc.duplicate_signal_cooldown_hours == 6.0


def test_main_threads_pivot_bars_from_config_into_run_and_persist(monkeypatch, tmp_path):
    # A KeyError, a swapped field, or silently relying on run_and_persist's/
    # BacktestConfig's own pivot_bars=3 default (regardless of config) would
    # all fail this -- _FAKE_MAIN_CFG's global.swing_pivot_bars=7 is a
    # distinct value from that default specifically to catch that.
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0"])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["pivot_bars"] = kwargs.get("pivot_bars")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    assert captured["pivot_bars"] == 7


def test_main_threads_min_lot_risk_cap_pct_from_config_into_run_and_persist(monkeypatch, tmp_path):
    # A KeyError, a swapped field, or silently relying on run_and_persist's/
    # BacktestConfig's own min_lot_risk_cap_pct=None default (regardless of
    # config) would all fail this -- _FAKE_MAIN_CFG's cfo.min_lot_risk_cap_pct
    # =1.25 is a distinct value from that default specifically to catch that.
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0"])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["min_lot_risk_cap_pct"] = kwargs.get("min_lot_risk_cap_pct")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    assert captured["min_lot_risk_cap_pct"] == 1.25


def test_main_min_lot_risk_cap_pct_cli_override_takes_precedence_over_config(monkeypatch, tmp_path):
    _patch_main_wiring(monkeypatch, tmp_path, _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}]))
    monkeypatch.setattr(sys, "argv", [
        "run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0", "--min-lot-risk-cap-pct", "2.0",
    ])

    captured = {}

    def _fake_run_and_persist(*args, **kwargs):
        captured["min_lot_risk_cap_pct"] = kwargs.get("min_lot_risk_cap_pct")
        return tmp_path / "envelope.json"

    monkeypatch.setattr(run_backtest_script, "run_and_persist", _fake_run_and_persist)

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    assert captured["min_lot_risk_cap_pct"] == 2.0


def test_main_writes_an_envelope_with_risk_voice_modeled_true(monkeypatch, tmp_path):
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    _patch_main_wiring(monkeypatch, tmp_path, _council_signal_bars())
    output_dir = tmp_path / "backtest_reports"
    monkeypatch.setattr(sys, "argv", [
        "run_backtest.py", "XAUUSD", "--commission-per-lot", "5.0", "--output-dir", str(output_dir),
    ])

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    [out_path] = list(output_dir.glob("XAUUSD_*.json"))
    envelope = load_backtest_report_envelope(out_path)
    assert envelope.risk_voice_modeled is True
    assert envelope.watchman_exits_modeled is True
    assert envelope.shield_modeled is True
    assert envelope.min_lot_risk_cap_pct == 1.25  # _FAKE_MAIN_CFG's cfo.min_lot_risk_cap_pct


def test_main_with_commission_zero_writes_envelope_with_cost_model_complete_true(monkeypatch, tmp_path):
    # The real motivating scenario for this fix: a genuinely commission-free
    # account (e.g. IC Markets "Standard") passes 0.0 deliberately through
    # the CLI. This must flow all the way through to a written envelope with
    # cost_model_complete=True -- not just be true of build_envelope() in
    # isolation (a falsy-check regression, e.g. `args.commission_per_lot or
    # X`, would silently corrupt this exact path without tripping any other
    # main()-level test, since all of them use a truthy commission value).
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bull_voice", lambda *a, **k: _score(75))
    monkeypatch.setattr("autotrade.council.decision_matrix.score_bear_voice", lambda *a, **k: _score(30))
    _patch_main_wiring(monkeypatch, tmp_path, _council_signal_bars())
    output_dir = tmp_path / "backtest_reports"
    monkeypatch.setattr(sys, "argv", [
        "run_backtest.py", "XAUUSD", "--commission-per-lot", "0.0", "--output-dir", str(output_dir),
    ])

    exit_code = run_backtest_script.main()

    assert exit_code == 0
    [out_path] = list(output_dir.glob("XAUUSD_*.json"))
    envelope = load_backtest_report_envelope(out_path)
    assert envelope.cost_model.commission_per_lot == 0.0
    assert envelope.cost_model_complete is True
