"""Loads and validates a `scripts/run_backtest.py`-produced `BacktestReport`
JSON envelope (trading_system_summary_v2.md Appendix A §5.2) -- schema
checked, not just `json.load`, so a malformed/hand-edited envelope fails
loudly here rather than crashing deep inside `auditor/promotion.py`'s gate
evaluation with a confusing `KeyError`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.report import BacktestReport


class BacktestReportEnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class BacktestReportEnvelope:
    symbol: str
    bar_range_start: datetime
    bar_range_end: datetime
    starting_equity: float
    cost_model: CostModelConfig
    cost_model_complete: bool
    """See `scripts/run_backtest.py`'s `build_envelope` -- true iff
    `commission_per_lot > 0` AND `slippage_points is None` (the latter means
    the run used the bar's-own-spread minimum-1-spread convention documented
    on `CostModelConfig`, not an explicit possibly-too-small override)."""
    is_out_of_sample: bool
    """Human-set at `scripts/run_backtest.py` invocation time -- never
    auto-detected."""
    risk_voice_modeled: bool
    """True iff this run's `BacktestConfig.risk_voice_cfg` was set (not
    `None`) -- see `backtest/engine.py`'s module docstring. A run without
    Risk Voice modeled only exercised Bull/Bear scoring + the Decision
    Matrix, not the full veto gate live trading applies, so its trade
    count/profit factor may not be representative -- same "don't silently
    count an incomplete simulation" philosophy as `cost_model_complete`."""
    report: BacktestReport


_REPORT_FIELDS = (
    "trade_count", "win_count", "loss_count", "win_rate", "gross_profit", "gross_loss",
    "profit_factor", "total_net_pnl", "avg_r_multiple", "max_drawdown_pct",
    "profit_factor_excluding_top_5",
)
_TOP_LEVEL_FIELDS = (
    "symbol", "bar_range", "starting_equity", "cost_model", "cost_model_complete",
    "is_out_of_sample", "risk_voice_modeled", "report",
)


def _require(data: dict, keys: Iterable[str], context: str) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise BacktestReportEnvelopeError(f"{context}: missing required field(s) {missing}")


def load_backtest_report_envelope(path: Path) -> BacktestReportEnvelope:
    """Load + schema-validate one envelope file written by
    `scripts/run_backtest.py`. Raises `BacktestReportEnvelopeError` on
    unreadable/invalid JSON or any missing required field."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BacktestReportEnvelopeError(f"{path}: not readable/valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise BacktestReportEnvelopeError(f"{path}: top-level JSON must be an object")

    _require(data, _TOP_LEVEL_FIELDS, str(path))
    _require(data["bar_range"], ("start", "end"), f"{path}: bar_range")
    _require(data["cost_model"], ("commission_per_lot", "slippage_points"), f"{path}: cost_model")
    _require(data["report"], _REPORT_FIELDS, f"{path}: report")

    try:
        return BacktestReportEnvelope(
            symbol=data["symbol"],
            bar_range_start=datetime.fromisoformat(data["bar_range"]["start"]),
            bar_range_end=datetime.fromisoformat(data["bar_range"]["end"]),
            starting_equity=data["starting_equity"],
            cost_model=CostModelConfig(
                commission_per_lot=data["cost_model"]["commission_per_lot"],
                slippage_points=data["cost_model"]["slippage_points"],
            ),
            cost_model_complete=data["cost_model_complete"],
            is_out_of_sample=data["is_out_of_sample"],
            risk_voice_modeled=data["risk_voice_modeled"],
            report=BacktestReport(**{k: data["report"][k] for k in _REPORT_FIELDS}),
        )
    except (TypeError, ValueError) as exc:
        raise BacktestReportEnvelopeError(f"{path}: malformed field value ({exc})") from exc
