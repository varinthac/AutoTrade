"""Forward-walk simulation for the Auditor's borderline-order replay
(trading_system_summary_v2.md Appendix A §5.4): given a hypothetical
`OrderPlan` logged by `council.decision_matrix.evaluate_council` and the
*actual* historical bars that followed, replay whether it would have hit
its stop-loss, take-profit, or gone dead past a time-stop cutoff -- reusing
`backtest.engine.check_exit`'s SL/TP/gap-priority convention so this replay
is exactly consistent with how the real backtest engine would have closed
the same position, not a second, subtly-different simulation.

Cost convention differs from `backtest/cost_model.py`'s `spread_slippage_price`
(§5.2's "minimum 1 spread of slippage" backtest assumption): a borderline
case was never actually filled, so there is no real slippage to model --
Appendix A §5.4 costs it as spread-at-log-time + commission only, no
slippage term. `cost_r` therefore reads `spread_points_at_entry` directly
and converts `cost_model.commission_per_lot` to R units by dividing by
`stop_distance * point_value` -- this cancels `lot_size` out of the
currency-vs-R conversion (commission cost = commission_per_lot * lot;
risk_amount = stop_distance * point_value * lot; the `lot` factor cancels),
which is what makes a single per-R commission adjustment valid regardless of
what lot size a real order would have used.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import check_exit
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan


@dataclass(frozen=True)
class ForwardWalkResult:
    outcome: Literal["take_profit", "stop_loss", "time_stop", "no_exit"]
    exit_index: int | None
    exit_price: float | None
    gross_r: float | None
    net_r: float | None


def _r_multiples(
    plan: OrderPlan,
    entry_price: float,
    exit_price: float,
    spread_points_at_entry: float,
    symbol_spec: SymbolSpec,
    cost_model: CostModelConfig,
) -> tuple[float, float]:
    """`stop_distance == 0` (malformed input -- a real `OrderPlan` never
    constructs one, see `council/order_construction.py`) is guarded the same
    way `backtest/engine.py`'s `_close_trade` guards its own
    `risk_amount`-based division: `0.0` rather than a raised
    `ZeroDivisionError`, so one corrupt entry can't crash an entire replay
    batch. Callers that need to distinguish "genuinely flat" from
    "malformed" (e.g. `auditor/borderline.py`) should validate
    `stop_distance > 0` themselves before calling `simulate_order_forward`."""
    if not plan.stop_distance:
        return 0.0, 0.0
    sign = 1.0 if plan.direction == "BUY" else -1.0
    gross_r = sign * (exit_price - entry_price) / plan.stop_distance
    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    cost_r = (
        spread_points_at_entry * symbol_spec.point + cost_model.commission_per_lot / point_value
    ) / plan.stop_distance
    return gross_r, gross_r - cost_r


def simulate_order_forward(
    df: pd.DataFrame,
    start_index: int,
    plan: OrderPlan,
    *,
    entry_price: float,
    spread_points_at_entry: float,
    symbol_spec: SymbolSpec,
    cost_model: CostModelConfig,
    time_stop_bars: int,
) -> ForwardWalkResult:
    """Walk `df` from `start_index` (the bar AFTER the case's `as_of_time`,
    matching `backtest.engine.run_backtest`'s next-bar convention) up to
    `time_stop_bars` bars ahead, checking each bar with `check_exit` in the
    same SL/TP/gap-priority order the real engine uses.

    `outcome="no_exit"` means the available historical data ran out before
    either a real exit or the time-stop cutoff was reached -- an unresolved
    case, not a real outcome, distinct from `"time_stop"` (the cutoff
    genuinely elapsed with neither SL nor TP touched, so the hypothetical
    position is marked at that bar's close)."""
    if start_index >= len(df) or time_stop_bars <= 0:
        return ForwardWalkResult("no_exit", None, None, None, None)

    end_index = start_index + time_stop_bars
    limit = min(end_index, len(df))

    for i in range(start_index, limit):
        exit_result = check_exit(plan.direction, plan.stop_loss, plan.take_profit, df.iloc[i])
        if exit_result is not None:
            exit_price, reason = exit_result
            gross_r, net_r = _r_multiples(
                plan, entry_price, exit_price, spread_points_at_entry, symbol_spec, cost_model
            )
            return ForwardWalkResult(reason, i, exit_price, gross_r, net_r)

    if limit < end_index:
        return ForwardWalkResult("no_exit", None, None, None, None)

    last_index = limit - 1
    exit_price = float(df.iloc[last_index]["close"])
    gross_r, net_r = _r_multiples(
        plan, entry_price, exit_price, spread_points_at_entry, symbol_spec, cost_model
    )
    return ForwardWalkResult("time_stop", last_index, exit_price, gross_r, net_r)
