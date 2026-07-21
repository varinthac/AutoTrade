"""Watchman decision entry point -- combines exit conditions and the SL
trail into a single decision, per trading_system_summary_v2.md Appendix A §4.

Priority order (exits are more urgent than a routine SL update): structure
invalidation -> CLOSE; time stop -> CLOSE; otherwise the monotonic SL trail
(`stop_logic.compute_updated_stop_loss`) -> MODIFY_SL if it produced a
change, NO_ACTION if not.

Phase 7b wires this into real MT5 position-modify/close calls, execution-
error handling, and a continuous loop -- this module only decides WHAT should
happen, it never touches execution/ or MT5.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from autotrade.watchman.exit_conditions import check_structure_invalidation, check_time_stop
from autotrade.watchman.position_metadata import PositionMetadata
from autotrade.watchman.stop_logic import compute_updated_stop_loss


@dataclass(frozen=True)
class WatchmanConfig:
    """`config/base.yaml`'s `watchman:` block (Appendix A §4 / §6). All
    values `[adjustable]` per the spec.

    `breakeven_enabled`/`trail_enabled` (default `True`, matching prior
    behavior) gate whether `compute_updated_stop_loss` ever generates the
    breakeven/trail candidates at all -- see that function's docstring.
    EXP-008 (`experiments/experiments_log.md`) found `False` for both is a
    real, evidence-backed candidate (isolating structure-invalidation/
    time-stop as the only remaining exit paths), not yet adopted here."""

    breakeven_at_r: float = 1.0
    trail_start_r: float = 1.5
    trail_distance_atr: float = 1.0
    time_stop_hours: float = 48.0
    dead_trade_r_band: float = 0.3
    breakeven_enabled: bool = True
    trail_enabled: bool = True


@dataclass(frozen=True)
class WatchmanDecision:
    action: Literal["CLOSE", "MODIFY_SL", "NO_ACTION"]
    new_stop_loss: float | None
    reason: str


def evaluate_watchman(
    position_metadata: PositionMetadata,
    current_sl: float,
    current_price: float,
    current_atr: float,
    df: pd.DataFrame,
    as_of_index: int,
    now: datetime,
    config: WatchmanConfig,
) -> WatchmanDecision:
    """One decision for one open position, given current market state.

    `as_of_index` must be the latest CLOSED H1 bar (structure invalidation
    is closed-bars-only per spec.md §3.4); `current_price`/`current_atr` may
    be live tick values for the SL trail, which reacts tick-by-tick per
    Appendix A §4's "hard protection" wording.

    `df` CONTRACT: passed straight through to `check_structure_invalidation`,
    which uses `position_metadata.entry_swing_index` as a positional `.iloc`
    lookup into it -- `df` MUST be the same fixed-origin, contiguous
    `RangeIndex` frame (or one only grown by appending rows at the end, e.g.
    `orchestrator/shadow_loop.py`'s `_append_bar` pattern) that was in use
    when `entry_swing_index` was recorded. See
    `exit_conditions.check_structure_invalidation`'s docstring for the full
    contract and why violating it silently produces a wrong decision.

    RAISES: this function (and the pure functions it calls --
    `check_structure_invalidation`, `check_time_stop`,
    `compute_updated_stop_loss`) can raise `ValueError` for malformed inputs
    (an invalid `direction`, a non-positive `initial_stop_distance`, an
    `entry_swing_index` after `as_of_index`, ...). A production caller
    iterating over multiple open positions MUST wrap EACH position's
    `evaluate_watchman` call in its own error boundary, so one malformed or
    inconsistent position's exception cannot prevent every other open
    position from being evaluated in the same pass -- match the
    `try/except Exception` isolation pattern `orchestrator/shadow_loop.py`'s
    `_process()` already uses around `_process_bar()` for exactly this kind
    of "one bad item shouldn't kill the whole loop" isolation.
    """
    if check_structure_invalidation(
        direction=position_metadata.direction,
        entry_swing_index=position_metadata.entry_swing_index,
        df=df,
        as_of_index=as_of_index,
    ):
        return WatchmanDecision(
            action="CLOSE",
            new_stop_loss=None,
            reason="structure invalidation: entry swing has been violated by a closed-bar close",
        )

    if check_time_stop(
        opened_at=position_metadata.opened_at,
        now=now,
        entry_price=position_metadata.entry_price,
        current_price=current_price,
        initial_stop_distance=position_metadata.initial_stop_distance,
        direction=position_metadata.direction,
        time_stop_hours=config.time_stop_hours,
        dead_trade_r_band=config.dead_trade_r_band,
    ):
        return WatchmanDecision(
            action="CLOSE",
            new_stop_loss=None,
            reason=(
                f"time stop: open >= {config.time_stop_hours}h with P&L within "
                f"+/-{config.dead_trade_r_band}R (dead trade)"
            ),
        )

    new_sl = compute_updated_stop_loss(
        direction=position_metadata.direction,
        current_sl=current_sl,
        entry_price=position_metadata.entry_price,
        initial_stop_distance=position_metadata.initial_stop_distance,
        current_price=current_price,
        current_atr=current_atr,
        breakeven_at_r=config.breakeven_at_r,
        trail_start_r=config.trail_start_r,
        trail_distance_atr=config.trail_distance_atr,
        breakeven_enabled=config.breakeven_enabled,
        trail_enabled=config.trail_enabled,
    )

    if new_sl != current_sl:
        return WatchmanDecision(
            action="MODIFY_SL",
            new_stop_loss=new_sl,
            reason=f"SL trail: moving stop from {current_sl} to {new_sl}",
        )

    return WatchmanDecision(action="NO_ACTION", new_stop_loss=None, reason="no change")
