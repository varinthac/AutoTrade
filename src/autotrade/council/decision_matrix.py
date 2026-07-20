"""The Decision Matrix -- turns Bull/Bear Voice scores into a proposed trade
(or NO_TRADE), per trading_system_summary_v2.md Appendix A §1.3:

    | Condition                          | Result                        |
    |-------------------------------------|--------------------------------|
    | Bull >= 70 AND Bear < 40             | propose BUY                   |
    | Bear >= 70 AND Bull < 40             | propose SELL                  |
    | Both >= 55 (conflicting)             | NO_TRADE, "conflicting signals"|
    | Neither reaches 70                   | NO_TRADE, "no conviction"      |

Appendix A §1.3's note (translated): the point where the original design
called for "escalate to Claude" (scores 60-70, or conflicting) -- in the
rule-based version, this always resolves as NO_TRADE, but is logged under a
separate `borderline` tag as a *full* hypothetical order (direction, entry,
SL, TP, spread at that moment, and all voices' scores) so the (future, Phase
8) Auditor can replay/simulate the outcome. Logging scores alone would make
replay impossible -- see `BorderlineCase`.

`borderline` interpretation (the spec gives a table + a qualitative note, not
a single crisp boolean -- this is the concrete rule used here):

- "conflicting signals": both Bull and Bear score >= `conflict_threshold`
  (55). Takes priority over the two checks below.
- "strong-but-not-negated": the literal table's gap (see below) -- one side
  clears its own 70 threshold but the clean row still fails because the
  other side isn't below 40, and the pair isn't "conflicting" either.
- "near-threshold": neither of the two clean rows, "conflicting", nor
  "strong-but-not-negated" above matched, and either score falls in
  `[60, threshold)` -- a near-miss on the 70-point bar.
- Anything else that isn't a clean BUY/SELL and isn't one of the three cases
  above is plain "no conviction": NOT borderline, no hypothetical order is
  worth logging for replay.

Known table gap: the table has no row for e.g. Bull=75, Bear=45 -- Bull
clears its own threshold but the clean-BUY row still fails because Bear
isn't below 40, yet Bear isn't near 55+ either. Exact boundaries: the gap is
`leading >= 70` (that side's own threshold) paired with `40 <= trailing < 55`
on the other side (it does not extend past 55, since once the trailing side
reaches 55 "both >= 55" already fires as conflicting). This used to resolve
to plain "no conviction" here, on the reasoning that the literal table +
note describe only "conflicting" and "near-threshold" as borderline. A
code-review pass argued that resolution destroys observability: a score that
clears 70 is a genuinely strong, threshold-clearing signal, not a "nothing
happened" case like a flat 20/20, and Appendix A §5.4's Auditor needs real
occurrence data on near-miss patterns like this one to judge whether the
`<40` negation requirement is tuned too strictly -- silently dropping it into
the non-borderline bucket would hide that signal. So this gap is now tagged
`is_borderline=True` with its own reason, "strong-but-not-negated", and
logged as a full hypothetical order like the other borderline cases; the
outcome is still NO_TRADE either way (this is a pure observability/logging
change, not an execution-safety one).

Hypothetical direction for a borderline case: whichever of Bull/Bear scored
higher. On an exact tie, this defaults to BUY (arbitrary but documented; a
tie is symmetric so either choice is equally defensible). For
"strong-but-not-negated" specifically there is never a tie by construction
(the leading side is >= 70 and the trailing side is < 55), so this always
resolves, unambiguously, to the side that cleared 70.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.order_construction import OrderPlan, build_order_plan
from autotrade.council.scoring import BullBearScore, score_bear_voice, score_bull_voice
from autotrade.features.indicators import atr
from autotrade.features.swing import latest_confirmed_swing_high, latest_confirmed_swing_low

NEAR_THRESHOLD_FLOOR = 60
NO_CLEAN_SIGNAL_CEILING = 40


@dataclass(frozen=True)
class CouncilDecision:
    direction: Literal["BUY", "SELL"] | None
    bull_score: BullBearScore
    bear_score: BullBearScore
    is_borderline: bool
    borderline_reason: str | None
    order_plan: OrderPlan | None


@dataclass(frozen=True)
class BorderlineCase:
    """Logged for every `borderline` decision (Appendix A §1.3's note) so a
    future Auditor can replay it. `risk_voice_score` is intentionally left
    unset here -- the Risk Voice (Appendix A §1.5) is Phase 6b's job, a
    veto/gate rather than a 0-100 scored voice like Bull/Bear; this field
    exists now purely so the shape is forward-compatible for 6b to fill in."""

    symbol: str
    as_of_time: datetime
    hypothetical_direction: Literal["BUY", "SELL"]
    bull_score: int
    bear_score: int
    risk_voice_score: float | None
    order_plan: OrderPlan
    spread_at_evaluation: float


def _build_order_plan(
    df: pd.DataFrame,
    as_of_index: int,
    direction: Literal["BUY", "SELL"],
    sl_buffer_atr: float,
    sl_min_atr: float,
    sl_max_atr: float,
    tp_r_multiple: float,
    pivot_bars: int,
) -> OrderPlan | None:
    """Compose the same Entry/SL/TP construction as `trivial_signal.
    build_trade_idea`: current ATR + the latest confirmed swing (low for
    BUY, high for SELL) feed `order_construction.build_order_plan`. Returns
    `None` if no confirmed swing is available yet -- never guess a
    stop-loss anchor."""
    closes = df["close"].iloc[: as_of_index + 1]
    highs = df["high"].iloc[: as_of_index + 1]
    lows = df["low"].iloc[: as_of_index + 1]
    current_atr = atr(highs, lows, closes).iloc[-1]

    if direction == "BUY":
        swing = latest_confirmed_swing_low(df, as_of_index, pivot_bars=pivot_bars)
    else:
        swing = latest_confirmed_swing_high(df, as_of_index, pivot_bars=pivot_bars)

    if swing is None:
        return None
    swing_price = swing[1]
    entry_price = df["close"].iloc[as_of_index]

    return build_order_plan(
        direction=direction,
        entry_price=entry_price,
        swing_price=swing_price,
        atr=current_atr,
        sl_buffer_atr=sl_buffer_atr,
        sl_min_atr=sl_min_atr,
        sl_max_atr=sl_max_atr,
        tp_r_multiple=tp_r_multiple,
    )


def evaluate_council(
    df: pd.DataFrame,
    as_of_index: int,
    symbol: str,
    symbol_spec: SymbolSpec,
    bull_threshold: int = 70,
    bear_threshold: int = 70,
    conflict_threshold: int = 55,
    sl_buffer_atr: float = 0.2,
    sl_min_atr: float = 0.8,
    sl_max_atr: float = 2.5,
    tp_r_multiple: float = 2.0,
    pivot_bars: int = 3,
) -> tuple[CouncilDecision, BorderlineCase | None]:
    """Score Bull/Bear Voices, apply the Decision Matrix, and construct the
    order (real, for a clean BUY/SELL; hypothetical, for a borderline case)
    -- the single entry point for the (not-yet-wired, Phase 6b) Council.

    Returns `(decision, borderline_case)`: `borderline_case` is `None`
    unless `decision.is_borderline` is `True` *and* a hypothetical order
    could actually be constructed (i.e. a confirmed swing exists) -- per
    Appendix A §1.3's note, logging just the scores without a full
    hypothetical order would make replay impossible, so a borderline case
    with no constructible order is not logged as one.
    """
    bull = score_bull_voice(df, as_of_index, symbol_spec, pivot_bars=pivot_bars)
    bear = score_bear_voice(df, as_of_index, symbol_spec, pivot_bars=pivot_bars)
    bull_s, bear_s = bull.score, bear.score

    direction: Literal["BUY", "SELL"] | None = None
    is_borderline = False
    borderline_reason: str | None = None

    if bull_s >= bull_threshold and bear_s < NO_CLEAN_SIGNAL_CEILING:
        direction = "BUY"
    elif bear_s >= bear_threshold and bull_s < NO_CLEAN_SIGNAL_CEILING:
        direction = "SELL"
    elif bull_s >= conflict_threshold and bear_s >= conflict_threshold:
        is_borderline = True
        borderline_reason = "conflicting signals"
    elif (bull_s >= bull_threshold and NO_CLEAN_SIGNAL_CEILING <= bear_s < conflict_threshold) or (
        bear_s >= bear_threshold and NO_CLEAN_SIGNAL_CEILING <= bull_s < conflict_threshold
    ):
        is_borderline = True
        borderline_reason = "strong-but-not-negated"
    elif NEAR_THRESHOLD_FLOOR <= bull_s < bull_threshold or NEAR_THRESHOLD_FLOOR <= bear_s < bear_threshold:
        is_borderline = True
        borderline_reason = "near-threshold"
    else:
        borderline_reason = "no conviction"

    order_plan: OrderPlan | None = None
    borderline_case: BorderlineCase | None = None

    if direction is not None:
        order_plan = _build_order_plan(
            df, as_of_index, direction, sl_buffer_atr, sl_min_atr, sl_max_atr, tp_r_multiple, pivot_bars
        )
    elif is_borderline:
        if bull_s > bear_s:
            hypothetical_direction: Literal["BUY", "SELL"] = "BUY"
        elif bear_s > bull_s:
            hypothetical_direction = "SELL"
        else:
            hypothetical_direction = "BUY"  # exact-tie default, see module docstring

        order_plan = _build_order_plan(
            df, as_of_index, hypothetical_direction, sl_buffer_atr, sl_min_atr, sl_max_atr, tp_r_multiple, pivot_bars
        )
        if order_plan is not None:
            borderline_case = BorderlineCase(
                symbol=symbol,
                as_of_time=df["time"].iloc[as_of_index],
                hypothetical_direction=hypothetical_direction,
                bull_score=bull_s,
                bear_score=bear_s,
                risk_voice_score=None,
                order_plan=order_plan,
                spread_at_evaluation=float(df["spread"].iloc[as_of_index]),
            )

    decision = CouncilDecision(
        direction=direction,
        bull_score=bull,
        bear_score=bear,
        is_borderline=is_borderline,
        borderline_reason=borderline_reason,
        order_plan=order_plan,
    )
    return decision, borderline_case
