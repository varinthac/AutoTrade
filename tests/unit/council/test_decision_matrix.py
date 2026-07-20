"""Tests for council/decision_matrix.py -- the Decision Matrix, Appendix A
§1.3.

Bull/Bear Voice scoring itself is already covered by test_scoring.py, so
`score_bull_voice`/`score_bear_voice` are monkeypatched here to return exact,
hand-picked scores (mirrors `tests/unit/backtest/test_engine.py`'s own
"fake, fully-controlled signal_fn" convention) -- this lets every branch of
the decision table, the borderline near-threshold/conflicting cases, and the
tie-breaking convention be hand-verified without also depending on organic
OHLC-derived scores.
"""
from __future__ import annotations

import pandas as pd
import pytest

from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.decision_matrix import BorderlineCase, CouncilDecision, evaluate_council
from autotrade.council.scoring import BullBearScore

SYMBOL_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)
SYMBOL = "XAUUSD"


def _score(total: int) -> BullBearScore:
    """A `BullBearScore` with the given total and zeroed-out component
    breakdown -- the breakdown itself is exercised in test_scoring.py, not
    here."""
    return BullBearScore(
        score=total, trend_alignment=0, momentum_rsi=0, momentum_macd=0, market_structure=0, confluence=0
    )


def _patch_scores(monkeypatch, bull_total: int, bear_total: int) -> None:
    monkeypatch.setattr(
        "autotrade.council.decision_matrix.score_bull_voice",
        lambda *args, **kwargs: _score(bull_total),
    )
    monkeypatch.setattr(
        "autotrade.council.decision_matrix.score_bear_voice",
        lambda *args, **kwargs: _score(bear_total),
    )


def _order_capable_df(n: int = 40) -> pd.DataFrame:
    """Flat OHLC with one confirmed swing low (index 10, low=90) and one
    confirmed swing high (index 30, high=110), both confirmed well before
    `as_of_index = n - 1` -- so order construction succeeds regardless of
    which hypothetical direction the decision matrix picks."""
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    lows[10] = 90.0
    highs[30] = 110.0
    times = pd.date_range("2026-07-19 00:00", periods=n, freq="h")
    spreads = [12.0] * n
    return pd.DataFrame({"time": times, "high": highs, "low": lows, "close": closes, "spread": spreads})


def _no_swing_df(n: int = 10) -> pd.DataFrame:
    """Perfectly flat OHLC -- no fractal swing high/low ever forms, so
    order construction has no stop-loss anchor and must return None."""
    times = pd.date_range("2026-07-19 00:00", periods=n, freq="h")
    return pd.DataFrame(
        {"time": times, "high": [101.0] * n, "low": [99.0] * n, "close": [100.0] * n, "spread": [10.0] * n}
    )


AS_OF = 39  # last index of _order_capable_df()


def test_clean_buy_row_proposes_buy(monkeypatch):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction == "BUY"
    assert decision.is_borderline is False
    assert decision.borderline_reason is None
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"
    assert borderline is None


def test_clean_sell_row_proposes_sell(monkeypatch):
    _patch_scores(monkeypatch, bull_total=30, bear_total=75)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction == "SELL"
    assert decision.is_borderline is False
    assert decision.borderline_reason is None
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "SELL"
    assert borderline is None


def test_both_above_conflict_threshold_is_conflicting_no_trade(monkeypatch):
    _patch_scores(monkeypatch, bull_total=60, bear_total=65)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    # Bear scored higher (65 > 60) -> hypothetical direction is SELL.
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "SELL"
    assert borderline is not None
    assert borderline.hypothetical_direction == "SELL"


def test_neither_reaches_60_is_plain_no_conviction_not_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=20, bear_total=25)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert decision.order_plan is None
    assert borderline is None


def test_near_threshold_band_is_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=65, bear_total=10)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"


def test_exact_tie_hypothetical_direction_defaults_to_buy(monkeypatch):
    # An exact tie in the borderline band (60, 60) is also >= conflict
    # threshold (55), so it resolves as "conflicting signals" -- a pure
    # near-threshold tie can't occur since conflict_threshold (55) < the
    # near-threshold floor (60). The documented BUY tie-break default is
    # exercised here regardless of which borderline branch produced it.
    _patch_scores(monkeypatch, bull_total=60, bear_total=60)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline.hypothetical_direction == "BUY"
    assert decision.order_plan.direction == "BUY"


def test_documented_decision_table_gap_is_borderline_strong_but_not_negated(monkeypatch):
    # Bull clears its own 70 threshold but the clean-BUY row still fails
    # because Bear (45) isn't below 40; Bear also isn't >= 55 (not
    # conflicting) and neither score sits in [60, 70) (not near-threshold).
    # See decision_matrix.py's module docstring for why this documented gap
    # is now tagged as borderline ("strong-but-not-negated") rather than
    # plain "no conviction".
    _patch_scores(monkeypatch, bull_total=75, bear_total=45)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"


def test_clean_buy_direction_set_even_when_no_confirmed_swing_yet(monkeypatch):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _no_swing_df()

    decision, borderline = evaluate_council(df, len(df) - 1, SYMBOL, SYMBOL_SPEC)

    assert decision.direction == "BUY"
    assert decision.order_plan is None
    assert borderline is None


def test_borderline_case_not_logged_when_no_confirmed_swing_yet(monkeypatch):
    _patch_scores(monkeypatch, bull_total=65, bear_total=10)
    df = _no_swing_df()

    decision, borderline = evaluate_council(df, len(df) - 1, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.order_plan is None
    assert borderline is None


def test_borderline_case_carries_the_full_hypothetical_order_for_replay(monkeypatch):
    _patch_scores(monkeypatch, bull_total=65, bear_total=10)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert isinstance(borderline, BorderlineCase)
    assert borderline.symbol == SYMBOL
    assert borderline.as_of_time == df["time"].iloc[AS_OF]
    assert borderline.hypothetical_direction == "BUY"
    assert borderline.bull_score == 65
    assert borderline.bear_score == 10
    assert borderline.risk_voice_score is None  # Phase 6b's job -- left unset here
    assert borderline.order_plan == decision.order_plan
    assert borderline.spread_at_evaluation == df["spread"].iloc[AS_OF]


def test_evaluate_council_returns_council_decision_and_optional_borderline_case(monkeypatch):
    _patch_scores(monkeypatch, bull_total=75, bear_total=30)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert isinstance(decision, CouncilDecision)
    assert decision.bull_score.score == 75
    assert decision.bear_score.score == 30


# --- Table gap: exact boundaries ------------------------------------------
#
# The gap is exactly {bull >= 70, 40 <= bear < 55} (and its bull/bear mirror).
# It is NOT the full [40, 70) range on the trailing side: once the trailing
# side reaches 55 it also satisfies "both >= 55" (since the leading side is
# already >= 70 >= 55), so conflicting-signals fires first and the gap closes
# at 55, not 70. The whole rectangle is borderline ("strong-but-not-negated"),
# with the hypothetical direction always the leading (>= 70) side. These
# tests pin every edge of that rectangle down precisely.

def test_gap_bull_leads_lower_left_corner_is_borderline(monkeypatch):
    # bull at its own floor (70), bear at the gap's lower edge (40, where
    # clean-BUY's "bear < 40" just stops holding).
    _patch_scores(monkeypatch, bull_total=70, bear_total=40)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"


def test_gap_bull_leads_upper_right_corner_is_borderline(monkeypatch):
    # bull at its ceiling (100), bear at the gap's upper edge (54, one below
    # the conflict threshold) -- confirms the gap spans the full bull range
    # >= 70, not just values near 70.
    _patch_scores(monkeypatch, bull_total=100, bear_total=54)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"


def test_gap_bull_leads_full_bear_span_at_bull_floor(monkeypatch):
    # bull pinned exactly at its own floor (70) while bear sits at the gap's
    # upper edge (54) -- both corners of the rectangle meet here.
    _patch_scores(monkeypatch, bull_total=70, bear_total=54)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"


def test_gap_lower_boundary_bear_39_is_clean_buy(monkeypatch):
    # One point below the gap's lower edge -- bear=39 < 40 satisfies clean
    # BUY's own row again.
    _patch_scores(monkeypatch, bull_total=70, bear_total=39)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction == "BUY"
    assert decision.is_borderline is False
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "BUY"


def test_gap_upper_boundary_bear_55_is_conflicting_not_gap(monkeypatch):
    # One point above the gap's upper edge -- bear=55 makes "both >= 55"
    # true (bull is already 70), so this is conflicting, NOT the gap --
    # confirms the gap closes at 55, not at bull_threshold's own 70.
    _patch_scores(monkeypatch, bull_total=70, bear_total=55)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"  # bull (70) > bear (55)


def test_gap_neighbor_trailing_side_in_55_to_70_is_conflicting_not_gap(monkeypatch):
    # A trailing score well inside [55, 70) -- not just at the 55 edge --
    # confirms the whole [55, 70) trailing band is conflicting, not gap,
    # because it always also satisfies "both >= 55" once the leading side
    # is >= 70.
    _patch_scores(monkeypatch, bull_total=70, bear_total=65)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"


def test_gap_boundary_bull_69_is_near_threshold_not_gap(monkeypatch):
    # bull=69 (one below its own 70 floor) with bear inside what would be
    # the gap zone (45) if bull were >= 70 -- but bull=69 falls in the
    # near-threshold band [60, 70) instead, so this is near-threshold, not
    # the gap. Confirms the gap only exists once the leading side actually
    # clears 70, not merely nears it.
    _patch_scores(monkeypatch, bull_total=69, bear_total=45)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert borderline is not None
    assert borderline.hypothetical_direction == "BUY"  # bull (69) > bear (45)


def test_gap_bear_leads_lower_left_corner_is_borderline(monkeypatch):
    # Mirror of the bull-leads gap: bear at its floor (70), bull at the
    # gap's lower edge (40).
    _patch_scores(monkeypatch, bull_total=40, bear_total=70)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "SELL"
    assert borderline is not None
    assert borderline.hypothetical_direction == "SELL"


def test_gap_bear_leads_upper_right_corner_is_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=54, bear_total=100)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "strong-but-not-negated"
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "SELL"
    assert borderline is not None
    assert borderline.hypothetical_direction == "SELL"


def test_gap_bear_leads_lower_boundary_bull_39_is_clean_sell(monkeypatch):
    _patch_scores(monkeypatch, bull_total=39, bear_total=70)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction == "SELL"
    assert decision.is_borderline is False
    assert decision.order_plan is not None
    assert decision.order_plan.direction == "SELL"


def test_gap_bear_leads_upper_boundary_bull_55_is_conflicting_not_gap(monkeypatch):
    _patch_scores(monkeypatch, bull_total=55, bear_total=70)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline is not None
    assert borderline.hypothetical_direction == "SELL"  # bear (70) > bull (55)


def test_gap_bear_leads_boundary_bear_69_is_near_threshold_not_gap(monkeypatch):
    _patch_scores(monkeypatch, bull_total=45, bear_total=69)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert borderline is not None
    assert borderline.hypothetical_direction == "SELL"  # bear (69) > bull (45)


def test_both_scores_high_and_ge_70_is_conflicting_not_a_clean_row(monkeypatch):
    # Both Bull and Bear clear 70 -- neither clean row fires (each requires
    # the OTHER score to be < 40), so this falls to "both >= 55" ->
    # conflicting, even though both scores individually look strong.
    _patch_scores(monkeypatch, bull_total=80, bear_total=90)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline.hypothetical_direction == "SELL"  # bear (90) > bull (80)


def test_both_scores_high_and_tied_defaults_to_buy(monkeypatch):
    _patch_scores(monkeypatch, bull_total=80, bear_total=80)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline.hypothetical_direction == "BUY"  # exact tie -> BUY default


# --- Near-threshold floor boundary (60) ------------------------------------

def test_near_threshold_floor_59_bull_is_no_conviction(monkeypatch):
    _patch_scores(monkeypatch, bull_total=59, bear_total=20)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert borderline is None


def test_near_threshold_floor_60_bull_is_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=60, bear_total=20)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert borderline.hypothetical_direction == "BUY"


def test_near_threshold_floor_59_bear_is_no_conviction(monkeypatch):
    _patch_scores(monkeypatch, bull_total=20, bear_total=59)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert borderline is None


def test_near_threshold_floor_60_bear_is_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=20, bear_total=60)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert borderline.hypothetical_direction == "SELL"


def test_near_threshold_fires_even_when_trailing_score_is_in_gap_zone(monkeypatch):
    # bull=65 is near-threshold on its own; bear=45 is inside what would be
    # the "gap zone" if bull were >= 70 instead of merely near it. This
    # confirms near-threshold only cares whether EITHER score is in
    # [60, 70) and ignores what zone the other score is in.
    _patch_scores(monkeypatch, bull_total=65, bear_total=45)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "near-threshold"
    assert borderline.hypothetical_direction == "BUY"


# --- Conflict threshold boundary (55) ---------------------------------------

def test_conflict_threshold_54_54_is_no_conviction(monkeypatch):
    # Both one point below the conflict threshold, and both below the
    # near-threshold floor too -- clean "neither reaches 70" no-conviction.
    _patch_scores(monkeypatch, bull_total=54, bear_total=54)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert borderline is None


def test_conflict_threshold_55_55_is_conflicting(monkeypatch):
    _patch_scores(monkeypatch, bull_total=55, bear_total=55)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == "conflicting signals"
    assert borderline.hypothetical_direction == "BUY"  # exact tie -> BUY default


@pytest.mark.parametrize("bull_total,bear_total", [(55, 54), (54, 55)])
def test_conflict_threshold_asymmetric_54_55_is_no_conviction(monkeypatch, bull_total, bear_total):
    # Only one side reaches 55 -- conflicting needs BOTH >= 55 -- and
    # neither side reaches the near-threshold floor of 60 either.
    _patch_scores(monkeypatch, bull_total=bull_total, bear_total=bear_total)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert borderline is None


# --- Tied scores: exact-tie tie-break must not leak into non-borderline cases

def test_low_tied_scores_are_not_borderline(monkeypatch):
    # A tie alone must never trigger borderline/hypothetical-direction
    # logic -- only a tie that ALSO lands in a borderline region does.
    _patch_scores(monkeypatch, bull_total=20, bear_total=20)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.direction is None
    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert decision.order_plan is None
    assert borderline is None


def test_zero_tied_scores_are_not_borderline(monkeypatch):
    _patch_scores(monkeypatch, bull_total=0, bear_total=0)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert decision.borderline_reason == "no conviction"
    assert decision.order_plan is None
    assert borderline is None


# --- is_borderline / BorderlineCase agreement invariant ---------------------
#
# `decision.is_borderline` and the returned `BorderlineCase` must never
# disagree when an order can actually be constructed (the fixture below
# always has both a confirmed swing high and low, so construction never
# fails for lack of an anchor): is_borderline True <=> borderline is not
# None, across every borderline-triggering path.

@pytest.mark.parametrize(
    "bull_total,bear_total,expected_reason,expected_direction",
    [
        (60, 65, "conflicting signals", "SELL"),
        (65, 10, "near-threshold", "BUY"),
        (10, 65, "near-threshold", "SELL"),
        (60, 60, "conflicting signals", "BUY"),  # exact tie
        (80, 90, "conflicting signals", "SELL"),  # both >= 70, still conflicting
        (69, 45, "near-threshold", "BUY"),  # gap-adjacent near-threshold
        (45, 69, "near-threshold", "SELL"),  # gap-adjacent near-threshold, bear side
        (75, 45, "strong-but-not-negated", "BUY"),  # table gap, bull leads
        (45, 75, "strong-but-not-negated", "SELL"),  # table gap, bear leads
    ],
)
def test_borderline_paths_agree_between_decision_and_borderline_case(
    monkeypatch, bull_total, bear_total, expected_reason, expected_direction
):
    _patch_scores(monkeypatch, bull_total=bull_total, bear_total=bear_total)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is True
    assert decision.borderline_reason == expected_reason
    assert decision.direction is None
    assert borderline is not None
    assert isinstance(borderline, BorderlineCase)
    assert borderline.hypothetical_direction == expected_direction
    assert decision.order_plan is not None
    assert decision.order_plan.direction == expected_direction
    assert borderline.order_plan == decision.order_plan


@pytest.mark.parametrize(
    "bull_total,bear_total",
    [
        (75, 30),  # clean BUY
        (30, 75),  # clean SELL
        (20, 25),  # clean no conviction, both low
        (54, 54),  # just under both thresholds
        (20, 20),  # tied, low
    ],
)
def test_non_borderline_paths_never_produce_a_borderline_case(monkeypatch, bull_total, bear_total):
    _patch_scores(monkeypatch, bull_total=bull_total, bear_total=bear_total)
    df = _order_capable_df()

    decision, borderline = evaluate_council(df, AS_OF, SYMBOL, SYMBOL_SPEC)

    assert decision.is_borderline is False
    assert borderline is None
