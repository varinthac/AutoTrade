"""Watchman stop-loss update logic -- the monotonic SL trail, per
trading_system_summary_v2.md Appendix A §4 items 2-3 and spec.md §3.4's
"hard invariant: SL only ever moves in the favorable direction -- unit-tested".

`compute_updated_stop_loss` makes that invariant structural, not incidental,
the same way `features/swing.py`'s no-lookahead guarantee is structural (see
that module's docstring for the pattern): every candidate new SL -- including
`current_sl` itself -- is collected into one list, and the return value is
`max()` of that list for BUY / `min()` for SELL. Since `current_sl` is always
one of the candidates, the function can never return anything less favorable
than what was already there, by construction of the max/min itself -- no
caller-side "don't forget to compare" discipline required.

Breakeven candidate uses `entry_price` with no spread adjustment. Appendix A
§4.2 specifies "entry + spread", but this module has no spread parameter --
a real spread value is only available in the execution layer (Phase 7b),
which is expected to add it on top of (or otherwise apply it to) this
function's output. Documented here rather than silently guessed at.
"""
from __future__ import annotations

from typing import Literal


def compute_updated_stop_loss(
    direction: Literal["BUY", "SELL"],
    current_sl: float,
    entry_price: float,
    initial_stop_distance: float,
    current_price: float,
    current_atr: float,
    breakeven_at_r: float,
    trail_start_r: float,
    trail_distance_atr: float,
    breakeven_enabled: bool = True,
    trail_enabled: bool = True,
) -> float:
    """New SL for an open position -- always at least as favorable as
    `current_sl` (see module docstring for why this is structural).

    `initial_stop_distance` is the FIXED R-multiple denominator recorded at
    entry (`watchman.position_metadata.PositionMetadata.initial_stop_distance`)
    -- never the current, possibly-already-moved stop distance.

    `breakeven_enabled`/`trail_enabled` (default `True`, matching prior
    behavior) gate whether the breakeven/trail candidates are ever appended
    to `candidates` at all -- when `False`, the corresponding candidate is
    simply never generated, regardless of `profit_r`. When BOTH are `False`,
    `candidates == [current_sl]` always, so this function is provably a
    no-op (per EXP-008, `experiments/experiments_log.md`).
    """
    if initial_stop_distance <= 0:
        raise ValueError(f"initial_stop_distance must be positive, got {initial_stop_distance}")

    if direction == "BUY":
        profit_r = (current_price - entry_price) / initial_stop_distance
    elif direction == "SELL":
        profit_r = (entry_price - current_price) / initial_stop_distance
    else:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {direction!r}")

    candidates = [current_sl]

    if profit_r >= breakeven_at_r and breakeven_enabled:
        candidates.append(entry_price)

    if profit_r >= trail_start_r and trail_enabled:
        if direction == "BUY":
            candidates.append(current_price - trail_distance_atr * current_atr)
        else:
            candidates.append(current_price + trail_distance_atr * current_atr)

    return max(candidates) if direction == "BUY" else min(candidates)
