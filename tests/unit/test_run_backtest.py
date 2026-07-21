"""Unit tests for scripts/run_backtest.py -- MT5-free (symbol_spec/df are
passed in directly), same importlib-loading convention as
tests/unit/test_kill_switch_script.py (scripts/ has no __init__.py)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from autotrade.auditor.backtest_results import load_backtest_report_envelope
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan
from autotrade.council.scoring import BullBearScore

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


def test_build_envelope_cost_model_complete_true_when_commission_set_and_min_spread_convention():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=3.5, slippage_points=None),
        10_000.0, False,
    )
    assert envelope["cost_model_complete"] is True


def test_build_envelope_cost_model_complete_false_when_commission_zero():
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=0.0, slippage_points=None),
        10_000.0, False,
    )
    assert envelope["cost_model_complete"] is False


def test_build_envelope_cost_model_complete_false_when_slippage_explicitly_overridden():
    # An explicit slippage_points override isn't guaranteed >= 1 spread --
    # cost_model_complete should not credit it as the min-1-spread convention.
    df = _bars([{"open": 100, "high": 101, "low": 99, "close": 100, "spread": 5}])
    report = run_backtest_script.generate_report([], 10_000.0)
    envelope = run_backtest_script.build_envelope(
        "XAUUSD", df, report, CostModelConfig(commission_per_lot=3.5, slippage_points=2.0),
        10_000.0, False,
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

    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert raw["bar_range"]["start"] == str(df["time"].iloc[0])
    assert raw["bar_range"]["end"] == str(df["time"].iloc[-1])
