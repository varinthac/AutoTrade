"""Order construction — Entry / SL / TP, per trading_system_summary_v2.md
Appendix A §1.4.

Pure functions only: no I/O, no MT5, no Clock. Config values (`sl_buffer_atr`,
`sl_min_atr`, `sl_max_atr`, `tp_r_multiple`) are passed in explicitly by the
caller (`config/base.yaml`'s `order:` block) rather than read here, so this
module stays config-agnostic and reusable by both the Phase 3 trivial signal
and the full Bull/Bear Council (Phase 6).

Note (Appendix A §1.5): `sl_max_atr` (2.5×ATR) is also the ceiling the
(not-yet-built) Risk Voice independently vetoes against -- this module only
implements Council's own clamp to that same ceiling, not the Risk Voice veto
itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OrderPlan:
    direction: Literal["BUY", "SELL"]
    entry: float
    stop_loss: float
    take_profit: float
    stop_distance: float


def build_order_plan(
    direction: Literal["BUY", "SELL"],
    entry_price: float,
    swing_price: float | None,
    atr: float,
    sl_buffer_atr: float = 0.2,
    sl_min_atr: float = 0.8,
    sl_max_atr: float = 2.5,
    tp_r_multiple: float = 2.0,
) -> OrderPlan | None:
    """Build the fixed Entry/SL/TP for a signal, per Appendix A §1.4.

    `swing_price` is the latest *confirmed* swing low (BUY) or swing high
    (SELL) as returned by `features.swing.latest_confirmed_swing_low` /
    `latest_confirmed_swing_high` -- if `None` (no confirmed swing yet),
    this returns `None`: no trade, never guess a stop-loss location.

    Stop-loss distance is clamped to `[sl_min_atr * atr, sl_max_atr * atr]`
    (widened up to the floor if the raw swing-based distance is too tight,
    capped at the ceiling if too wide). Take-profit is always computed from
    the final, post-clamp `stop_distance` (fixed 2R per Appendix A §1.4).
    """
    if swing_price is None:
        return None

    if direction == "BUY":
        raw_stop = swing_price - sl_buffer_atr * atr
        raw_distance = entry_price - raw_stop
    elif direction == "SELL":
        raw_stop = swing_price + sl_buffer_atr * atr
        raw_distance = raw_stop - entry_price
    else:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got {direction!r}")

    min_distance = sl_min_atr * atr
    max_distance = sl_max_atr * atr
    stop_distance = min(max(raw_distance, min_distance), max_distance)

    if direction == "BUY":
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + tp_r_multiple * stop_distance
    else:
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - tp_r_multiple * stop_distance

    return OrderPlan(
        direction=direction,
        entry=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        stop_distance=stop_distance,
    )
