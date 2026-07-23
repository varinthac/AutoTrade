"""Tests for auditor/backtest_results.py -- envelope loading/schema
validation (Appendix A §5.2)."""
from __future__ import annotations

import json

import pytest

from autotrade.auditor.backtest_results import (
    BacktestReportEnvelopeError,
    load_backtest_report_envelope,
)

_VALID_REPORT = {
    "trade_count": 200, "win_count": 120, "loss_count": 80, "win_rate": 0.6,
    "gross_profit": 5000.0, "gross_loss": 3000.0, "profit_factor": 1.6666,
    "total_net_pnl": 2000.0, "avg_r_multiple": 0.5, "max_drawdown_pct": 10.0,
    "profit_factor_excluding_top_5": 1.2,
}


def _valid_envelope() -> dict:
    return {
        "symbol": "XAUUSD",
        "bar_range": {"start": "2024-01-01 00:00:00", "end": "2026-01-01 00:00:00"},
        "starting_equity": 10000.0,
        "cost_model": {"commission_per_lot": 3.5, "slippage_points": None},
        "cost_model_complete": True,
        "is_out_of_sample": False,
        "risk_voice_modeled": True,
        "watchman_exits_modeled": True,
        "shield_modeled": True,
        "min_lot_risk_cap_pct": 1.5,
        "report": _VALID_REPORT,
    }


def test_load_valid_envelope_round_trips_every_field(tmp_path):
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(_valid_envelope()), encoding="utf-8")

    envelope = load_backtest_report_envelope(path)

    assert envelope.symbol == "XAUUSD"
    assert envelope.bar_range_start.isoformat() == "2024-01-01T00:00:00"
    assert envelope.bar_range_end.isoformat() == "2026-01-01T00:00:00"
    assert envelope.starting_equity == 10000.0
    assert envelope.cost_model.commission_per_lot == 3.5
    assert envelope.cost_model.slippage_points is None
    assert envelope.cost_model_complete is True
    assert envelope.is_out_of_sample is False
    assert envelope.risk_voice_modeled is True
    assert envelope.watchman_exits_modeled is True
    assert envelope.shield_modeled is True
    assert envelope.min_lot_risk_cap_pct == 1.5
    assert envelope.report.trade_count == 200
    assert envelope.report.profit_factor_excluding_top_5 == 1.2


def test_missing_top_level_field_raises(tmp_path):
    data = _valid_envelope()
    del data["cost_model_complete"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="cost_model_complete"):
        load_backtest_report_envelope(path)


def test_missing_risk_voice_modeled_field_raises(tmp_path):
    data = _valid_envelope()
    del data["risk_voice_modeled"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="risk_voice_modeled"):
        load_backtest_report_envelope(path)


def test_missing_watchman_exits_modeled_field_raises(tmp_path):
    data = _valid_envelope()
    del data["watchman_exits_modeled"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="watchman_exits_modeled"):
        load_backtest_report_envelope(path)


def test_missing_shield_modeled_field_raises(tmp_path):
    data = _valid_envelope()
    del data["shield_modeled"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="shield_modeled"):
        load_backtest_report_envelope(path)


def test_missing_min_lot_risk_cap_pct_field_raises(tmp_path):
    data = _valid_envelope()
    del data["min_lot_risk_cap_pct"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="min_lot_risk_cap_pct"):
        load_backtest_report_envelope(path)


def test_min_lot_risk_cap_pct_none_is_a_valid_value_not_a_missing_field(tmp_path):
    data = _valid_envelope()
    data["min_lot_risk_cap_pct"] = None
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    envelope = load_backtest_report_envelope(path)

    assert envelope.min_lot_risk_cap_pct is None


def test_missing_report_field_raises(tmp_path):
    data = _valid_envelope()
    del data["report"]["profit_factor"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError, match="profit_factor"):
        load_backtest_report_envelope(path)


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "envelope.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError):
        load_backtest_report_envelope(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(BacktestReportEnvelopeError):
        load_backtest_report_envelope(tmp_path / "does_not_exist.json")


def test_non_object_top_level_raises(tmp_path):
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(BacktestReportEnvelopeError):
        load_backtest_report_envelope(path)
