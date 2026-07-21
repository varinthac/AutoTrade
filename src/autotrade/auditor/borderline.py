"""Borderline-case expectancy tracking (trading_system_summary_v2.md
Appendix A §5.4): replays every `council.decision_matrix.BorderlineCase`
`orchestrator/shadow_loop.py` logged to `data/db/borderline_log.jsonl`
against the actual historical price data that followed, using
`backtest/forward_walk.py`'s `simulate_order_forward` (which itself reuses
`backtest.engine.check_exit` for the SL/TP/gap-priority convention).

This module only computes the expectancy number (`avg_net_r`,
`meets_ai_consideration_signal`) -- it does NOT decide whether to actually
add AI escalation. That is a human decision informed by this data, per
Appendix A §5.4's "ตัดสินจากข้อมูลจริง ไม่ใช่ความรู้สึก" (decide from real
data, not a feeling) -- `meets_ai_consideration_signal` is a signal to
surface, not an instruction to act on automatically.

**Cost convention (§5.4 vs. §5.2, already documented on
`backtest/forward_walk.py` -- repeated here since it's the module callers
actually see):** a borderline case was never filled, so there is no real
slippage; cost is spread-at-log-time + commission only, no slippage term.

**`replayed_count` vs. `unresolved_count`:** a case is "unresolved" (not
counted toward `avg_net_r`/the AI-consideration signal) when its historical
price data is missing/insufficient to determine an outcome -- e.g. the
symbol has no entry in `price_data_by_symbol`, `as_of_time` isn't found in
that symbol's bar series, or the available data runs out before either an
exit or the time-stop cutoff (`ForwardWalkResult.outcome == "no_exit"`, see
`forward_walk.py`). This is different from a malformed/legitimately-missing
input, which is silently skipped entirely (not counted in either bucket) --
see `build_borderline_expectancy_report`'s per-case exception handling.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.forward_walk import simulate_order_forward
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan

logger = logging.getLogger(__name__)

DEFAULT_MIN_CASES_FOR_SIGNAL = 30
DEFAULT_MIN_AVG_R_FOR_SIGNAL = 0.2


@dataclass(frozen=True)
class BorderlineExpectancyReport:
    replayed_count: int
    unresolved_count: int
    tp_count: int
    sl_count: int
    time_stop_count: int
    avg_net_r: float | None
    """`None` if `replayed_count == 0`."""
    meets_ai_consideration_signal: bool | None
    """`True`/`False` once `replayed_count >= min_cases_for_signal`;
    `None` (not yet enough data) below that -- Appendix A §5.4's own
    "หลัง 3 เดือน... ที่ >= 30 เคส" qualifier, expressed as a case-count
    floor here rather than a hardcoded time window."""


def load_borderline_cases(path: Path) -> list[dict]:
    """Parse `data/db/borderline_log.jsonl` (one `BorderlineCase` JSON object
    per line, `orchestrator/shadow_loop.py`'s `_log_borderline_case` format).
    Returns `[]` if `path` doesn't exist yet (nothing logged so far)."""
    path = Path(path)
    if not path.exists():
        return []
    cases = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _find_bar_index(df: pd.DataFrame, as_of_time: datetime) -> int | None:
    matches = df.index[df["time"] == pd.Timestamp(as_of_time)]
    return int(matches[0]) if len(matches) else None


def build_borderline_expectancy_report(
    cases: list[dict],
    price_data_by_symbol: dict[str, pd.DataFrame],
    symbol_specs: dict[str, SymbolSpec],
    cost_model: CostModelConfig,
    time_stop_bars: int,
    min_cases_for_signal: int = DEFAULT_MIN_CASES_FOR_SIGNAL,
    min_avg_r_for_signal: float = DEFAULT_MIN_AVG_R_FOR_SIGNAL,
) -> BorderlineExpectancyReport:
    """Replay every case in `cases` (as returned by `load_borderline_cases`)
    forward against `price_data_by_symbol[case["symbol"]]` and aggregate the
    outcomes. A case whose symbol/`as_of_time` bar can't be located in the
    supplied price data is logged and skipped entirely (not counted in any
    bucket) -- distinct from `ForwardWalkResult.outcome == "no_exit"` (data
    exists but simply ran out before a resolution), which IS counted as
    `unresolved_count`."""
    replayed_count = 0
    unresolved_count = 0
    tp_count = 0
    sl_count = 0
    time_stop_count = 0
    net_rs: list[float] = []

    outcome_counts: dict[Literal["take_profit", "stop_loss", "time_stop"], int] = {
        "take_profit": 0, "stop_loss": 0, "time_stop": 0,
    }

    for case in cases:
        symbol = case.get("symbol")
        df = price_data_by_symbol.get(symbol)
        symbol_spec = symbol_specs.get(symbol)
        if df is None or symbol_spec is None:
            logger.warning("borderline case for symbol %r has no supplied price data/symbol spec -- skipping", symbol)
            continue

        try:
            as_of_time = datetime.fromisoformat(case["as_of_time"])
            plan = OrderPlan(**case["order_plan"])
            spread_at_evaluation = float(case["spread_at_evaluation"])
            if plan.stop_distance <= 0:
                # A real OrderPlan never has stop_distance <= 0 (see
                # council/order_construction.py) -- this is data corruption,
                # not a valid "flat" R-multiple, so it's treated the same as
                # any other malformed case (skipped, not counted as
                # unresolved) rather than silently producing a meaningless
                # R=0.0 outcome via forward_walk's own zero-guard.
                raise ValueError(f"stop_distance must be positive, got {plan.stop_distance!r}")
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("malformed borderline case %r -- skipping (%s)", case, exc)
            continue

        start_index = _find_bar_index(df, as_of_time)
        if start_index is None:
            logger.warning(
                "borderline case as_of_time=%s not found in supplied price data for %s -- skipping",
                as_of_time, symbol,
            )
            continue

        result = simulate_order_forward(
            df, start_index + 1, plan,
            entry_price=plan.entry, spread_points_at_entry=spread_at_evaluation,
            symbol_spec=symbol_spec, cost_model=cost_model, time_stop_bars=time_stop_bars,
        )

        if result.outcome == "no_exit":
            unresolved_count += 1
            continue

        replayed_count += 1
        outcome_counts[result.outcome] += 1
        net_rs.append(result.net_r)

    tp_count = outcome_counts["take_profit"]
    sl_count = outcome_counts["stop_loss"]
    time_stop_count = outcome_counts["time_stop"]

    avg_net_r = sum(net_rs) / len(net_rs) if net_rs else None

    if replayed_count < min_cases_for_signal:
        meets_ai_consideration_signal = None
    else:
        meets_ai_consideration_signal = avg_net_r is not None and avg_net_r >= min_avg_r_for_signal

    return BorderlineExpectancyReport(
        replayed_count=replayed_count,
        unresolved_count=unresolved_count,
        tp_count=tp_count,
        sl_count=sl_count,
        time_stop_count=time_stop_count,
        avg_net_r=avg_net_r,
        meets_ai_consideration_signal=meets_ai_consideration_signal,
    )
