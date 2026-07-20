"""Tests for watchman/stop_logic.py, Appendix A §4 items 2-3 and spec.md
§3.4's "hard invariant: SL only ever moves in the favorable direction".

Fixed inputs throughout the hand-computed tests: entry_price = 100,
initial_stop_distance = 10 (so 1R = 10), breakeven_at_r = 1.0,
trail_start_r = 1.5, trail_distance_atr = 1.0.
"""
from __future__ import annotations

import random

import pytest

from autotrade.watchman.stop_logic import compute_updated_stop_loss

ENTRY = 100.0
STOP_DISTANCE = 10.0
BREAKEVEN_AT_R = 1.0
TRAIL_START_R = 1.5
TRAIL_DISTANCE_ATR = 1.0


def _buy(current_sl, current_price, current_atr=2.0, **overrides):
    kwargs = dict(
        direction="BUY",
        current_sl=current_sl,
        entry_price=ENTRY,
        initial_stop_distance=STOP_DISTANCE,
        current_price=current_price,
        current_atr=current_atr,
        breakeven_at_r=BREAKEVEN_AT_R,
        trail_start_r=TRAIL_START_R,
        trail_distance_atr=TRAIL_DISTANCE_ATR,
    )
    kwargs.update(overrides)
    return compute_updated_stop_loss(**kwargs)


def _sell(current_sl, current_price, current_atr=2.0, **overrides):
    kwargs = dict(
        direction="SELL",
        current_sl=current_sl,
        entry_price=ENTRY,
        initial_stop_distance=STOP_DISTANCE,
        current_price=current_price,
        current_atr=current_atr,
        breakeven_at_r=BREAKEVEN_AT_R,
        trail_start_r=TRAIL_START_R,
        trail_distance_atr=TRAIL_DISTANCE_ATR,
    )
    kwargs.update(overrides)
    return compute_updated_stop_loss(**kwargs)


def test_buy_below_breakeven_r_returns_current_sl_unchanged():
    # profit_r = (105 - 100) / 10 = 0.5, below breakeven_at_r=1.0.
    assert _buy(current_sl=90.0, current_price=105.0) == 90.0


def test_buy_at_breakeven_r_moves_sl_to_entry():
    # profit_r = (110 - 100) / 10 = 1.0, exactly breakeven_at_r -> boundary inclusive.
    assert _buy(current_sl=90.0, current_price=110.0) == 100.0


def test_buy_between_breakeven_and_trail_r_stays_at_breakeven():
    # profit_r = (114 - 100) / 10 = 1.4, past breakeven, below trail_start_r=1.5.
    assert _buy(current_sl=90.0, current_price=114.0) == 100.0


def test_buy_at_trail_start_r_trails_by_atr():
    # profit_r = (115 - 100) / 10 = 1.5, exactly trail_start_r -> boundary inclusive.
    # trail candidate = 115 - 1.0*2.0 = 113, beats breakeven candidate (100).
    assert _buy(current_sl=90.0, current_price=115.0, current_atr=2.0) == 113.0


def test_buy_trail_never_retreats_below_current_sl_even_if_trail_candidate_is_lower():
    # current_sl already at 112 (from a prior, higher price); price has
    # pulled back to 115 giving a trail candidate of 113 (still higher, fine)
    # but with a wider ATR the trail candidate can dip under current_sl --
    # max() must keep current_sl.
    result = _buy(current_sl=112.0, current_price=115.0, current_atr=5.0)
    # trail candidate = 115 - 5 = 110, breakeven candidate = 100, current_sl = 112
    assert result == 112.0


def test_sell_below_breakeven_r_returns_current_sl_unchanged():
    # profit_r = (100 - 95) / 10 = 0.5, below breakeven_at_r=1.0.
    assert _sell(current_sl=110.0, current_price=95.0) == 110.0


def test_sell_at_breakeven_r_moves_sl_to_entry():
    # profit_r = (100 - 90) / 10 = 1.0, exactly breakeven_at_r.
    assert _sell(current_sl=110.0, current_price=90.0) == 100.0


def test_sell_at_trail_start_r_trails_by_atr():
    # profit_r = (100 - 85) / 10 = 1.5, exactly trail_start_r.
    # trail candidate = 85 + 1.0*2.0 = 87, beats breakeven candidate (100).
    assert _sell(current_sl=110.0, current_price=85.0, current_atr=2.0) == 87.0


def test_sell_trail_never_retreats_above_current_sl():
    result = _sell(current_sl=88.0, current_price=85.0, current_atr=5.0)
    # trail candidate = 85 + 5 = 90, breakeven candidate = 100, current_sl = 88
    # min() must pick 88, not the worse (higher) candidates.
    assert result == 88.0


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        compute_updated_stop_loss(
            direction="HOLD", current_sl=90.0, entry_price=ENTRY,
            initial_stop_distance=STOP_DISTANCE, current_price=105.0,
            current_atr=2.0, breakeven_at_r=BREAKEVEN_AT_R,
            trail_start_r=TRAIL_START_R, trail_distance_atr=TRAIL_DISTANCE_ATR,
        )


def test_nonpositive_initial_stop_distance_raises():
    with pytest.raises(ValueError):
        compute_updated_stop_loss(
            direction="BUY", current_sl=90.0, entry_price=ENTRY,
            initial_stop_distance=0.0, current_price=105.0,
            current_atr=2.0, breakeven_at_r=BREAKEVEN_AT_R,
            trail_start_r=TRAIL_START_R, trail_distance_atr=TRAIL_DISTANCE_ATR,
        )


# --- The single most important test in this module -----------------------


def test_buy_sl_is_monotonically_non_decreasing_across_an_adversarial_price_path():
    """Feed a long, varied simulated price path -- including a big favorable
    move immediately followed by a sharp pullback -- through
    compute_updated_stop_loss() sequentially (each call's current_sl is the
    previous call's return value, as the real Watchman loop would do) and
    assert the returned SL never decreases for a BUY. This is what proves
    "SL only ever moves favorably" holds not just for isolated inputs but
    across a whole realistic sequence of ticks."""
    rng = random.Random(42)

    entry_price = 2400.0
    stop_distance = 15.0
    current_sl = entry_price - stop_distance

    # Build a price path: random walk, but with an explicit hand-placed
    # adversarial segment (big favorable rally, then a sharp pullback) spliced
    # in partway through, plus randomized ATR values (including some large
    # spikes) throughout.
    prices = [entry_price]
    price = entry_price
    for _ in range(150):
        price += rng.uniform(-3.0, 3.5)  # slight upward bias, still noisy
        prices.append(price)

    # Adversarial segment: a sharp rally of +40 over a few bars...
    rally_start = prices[-1]
    for step in range(1, 6):
        prices.append(rally_start + step * 8.0)
    peak_price = prices[-1]

    # ...immediately followed by a sharp pullback of -25.
    for step in range(1, 6):
        prices.append(peak_price - step * 5.0)

    # More random noise after the pullback.
    price = prices[-1]
    for _ in range(150):
        price += rng.uniform(-3.0, 3.5)
        prices.append(price)

    sl_history = [current_sl]
    for p in prices:
        atr = rng.uniform(0.5, 6.0)  # includes occasional large ATR spikes
        current_sl = compute_updated_stop_loss(
            direction="BUY",
            current_sl=current_sl,
            entry_price=entry_price,
            initial_stop_distance=stop_distance,
            current_price=p,
            current_atr=atr,
            breakeven_at_r=BREAKEVEN_AT_R,
            trail_start_r=TRAIL_START_R,
            trail_distance_atr=TRAIL_DISTANCE_ATR,
        )
        sl_history.append(current_sl)

    for prev_sl, next_sl in zip(sl_history, sl_history[1:]):
        assert next_sl >= prev_sl, (
            f"BUY SL retreated: {prev_sl} -> {next_sl} -- violates the hard "
            "monotonicity invariant"
        )

    # Sanity: the trail actually did move at some point (not a vacuously
    # true test where the SL just never triggered at all).
    assert sl_history[-1] > sl_history[0]


def test_buy_sl_locks_at_peak_trail_even_when_pullback_drops_below_breakeven_price():
    """A pullback so sharp it goes below the breakeven price itself (not just
    below the peak trailing level) must not un-clamp the SL -- current_sl
    stays in the candidate list regardless of what the *current* profit_r
    is, so a later low profit_r must not cost back any already-locked gain."""
    sl = 90.0
    # Rally: profit_r = (200-100)/10 = 10 -> trail candidate = 200 - 5*1 = 195.
    sl = _buy(current_sl=sl, current_price=200.0, current_atr=5.0)
    assert sl == 195.0
    # Sharp pullback to 105: profit_r = 0.5, BELOW breakeven_at_r=1.0 -- so
    # neither the breakeven nor the trail candidate is even generated this
    # call; only current_sl=195 is in the candidate list.
    sl = _buy(current_sl=sl, current_price=105.0, current_atr=2.0)
    assert sl == 195.0


def test_sell_sl_locks_at_peak_trail_even_when_pullback_drops_below_breakeven_price():
    sl = 110.0
    # profit_r = (100-20)/10 = 8 -> trail candidate = 20 + 4*1 = 24.
    sl = _sell(current_sl=sl, current_price=20.0, current_atr=4.0)
    assert sl == 24.0
    # Pullback to 105: profit_r = -0.5, below breakeven_at_r -- only
    # current_sl=24 is a candidate.
    sl = _sell(current_sl=sl, current_price=105.0, current_atr=1.0)
    assert sl == 24.0


def test_buy_sl_across_multiple_rally_pullback_cycles_with_an_exact_tie_candidate():
    """Three separate rally-then-pullback cycles (not just one), each
    pullback overshooting below the breakeven price, plus a cycle where the
    freshly-computed trail candidate happens to land EXACTLY on the current
    SL (an edge case for max()'s tie-breaking -- must not raise and must not
    treat equal-to as a retreat)."""
    sl = 90.0
    steps_and_expected = [
        (200.0, 5.0, 195.0),   # cycle 1 rally: profit_r=10, trail=200-5=195
        (95.0, 1.0, 195.0),    # cycle 1 pullback below breakeven (profit_r=-0.5): locked at 195
        (250.0, 3.0, 247.0),   # cycle 2 rally: profit_r=15, trail=250-3=247
        (98.0, 1.0, 247.0),    # cycle 2 pullback below breakeven: locked at 247
        (250.0, 3.0, 247.0),   # cycle 3 rally: trail=250-3=247 -- EXACT TIE with current_sl
        (300.0, 2.0, 298.0),   # cycle 3 continues rallying: trail=300-2=298
        (90.0, 1.0, 298.0),    # deep pullback (profit_r=-1.0), well below breakeven: locked at 298
    ]
    for price, atr, expected in steps_and_expected:
        sl = _buy(current_sl=sl, current_price=price, current_atr=atr)
        assert sl == expected, f"price={price} atr={atr}: expected {expected}, got {sl}"


def test_sell_sl_across_multiple_rally_pullback_cycles_with_an_exact_tie_candidate():
    """SELL-direction mirror with its own independently chosen numbers (not
    a trivial negation of the BUY case) -- same three-cycle-plus-tie shape."""
    sl = 110.0
    steps_and_expected = [
        (20.0, 4.0, 24.0),     # cycle 1 rally: profit_r=8, trail=20+4=24
        (105.0, 1.0, 24.0),    # cycle 1 pullback above breakeven (profit_r=-0.5): locked at 24
        (5.0, 2.5, 7.5),       # cycle 2 rally: profit_r=9.5, trail=5+2.5=7.5
        (102.0, 1.0, 7.5),     # cycle 2 pullback above breakeven: locked at 7.5
        (5.0, 2.5, 7.5),       # cycle 3 rally: trail=5+2.5=7.5 -- EXACT TIE with current_sl
        (-10.0, 3.0, -7.0),    # cycle 3 continues dropping: trail=-10+3=-7
        (108.0, 1.0, -7.0),    # deep pullback above breakeven: locked at -7
    ]
    for price, atr, expected in steps_and_expected:
        sl = _sell(current_sl=sl, current_price=price, current_atr=atr)
        assert sl == expected, f"price={price} atr={atr}: expected {expected}, got {sl}"


def test_buy_trail_candidate_wins_over_breakeven_well_beyond_trail_start_r():
    """Both breakeven (entry=100) and trail candidates are active
    (profit_r=3.0 > trail_start_r=1.5 > breakeven_at_r=1.0); the trail
    candidate (128) is more favorable than breakeven (100) and must win --
    not just at the trail_start_r boundary but well past it too."""
    assert _buy(current_sl=90.0, current_price=130.0, current_atr=2.0) == 128.0


def test_sell_trail_candidate_wins_over_breakeven_well_beyond_trail_start_r():
    assert _sell(current_sl=110.0, current_price=70.0, current_atr=2.0) == 72.0


def test_buy_breakeven_wins_when_trail_candidate_is_less_favorable_despite_being_active():
    """Both candidates are active (profit_r=3.0), but a huge ATR spike makes
    the trail candidate (130 - 50 = 80) WORSE than breakeven (100). This
    guards against an ordering bug that always prefers the trail candidate
    once trail_start_r is reached -- the correct behavior is "whichever is
    more favorable wins", proven here by breakeven winning instead."""
    assert _buy(current_sl=90.0, current_price=130.0, current_atr=50.0) == 100.0


def test_sell_breakeven_wins_when_trail_candidate_is_less_favorable_despite_being_active():
    assert _sell(current_sl=110.0, current_price=70.0, current_atr=50.0) == 100.0


@pytest.mark.parametrize("stop_distance", [0.01, 0.5, 1.0, 15.0, 100.0, 1000.0])
@pytest.mark.parametrize("direction,bias", [("BUY", 1), ("SELL", -1)])
def test_sl_monotonicity_holds_across_a_wide_range_of_initial_stop_distances(
    direction, bias, stop_distance
):
    """The single fixed-value stop_distance=15 used by the two big adversarial
    tests below does not prove the R-multiple math holds at other scales.
    Re-run a (smaller, deterministic) three-cycle rally/deep-pullback
    sequence -- scaled proportionally to stop_distance so the R-multiple
    thresholds are actually crossed regardless of how tight or wide the
    stop is -- across stop distances spanning 5 orders of magnitude
    (very tight/high-leverage through very wide/loose), for both
    directions, and assert the monotonicity invariant still holds."""
    entry_price = 2400.0
    rng = random.Random(999)

    prices = [entry_price]
    for cycle in range(3):
        # Rally to an increasing R-multiple each cycle (3R, 4R, 5R).
        peak = entry_price + bias * (3.0 + cycle) * stop_distance
        prices.append(peak)
        # Deep pullback to -0.5R -- well below the breakeven price.
        trough = entry_price - bias * 0.5 * stop_distance
        prices.append(trough)

    current_sl = entry_price - bias * stop_distance
    sl_history = [current_sl]
    for p in prices:
        atr = rng.uniform(0.1, 2.0) * stop_distance / 5.0
        current_sl = compute_updated_stop_loss(
            direction=direction,
            current_sl=current_sl,
            entry_price=entry_price,
            initial_stop_distance=stop_distance,
            current_price=p,
            current_atr=atr,
            breakeven_at_r=BREAKEVEN_AT_R,
            trail_start_r=TRAIL_START_R,
            trail_distance_atr=TRAIL_DISTANCE_ATR,
        )
        sl_history.append(current_sl)

    for prev_sl, next_sl in zip(sl_history, sl_history[1:]):
        if direction == "BUY":
            assert next_sl >= prev_sl, (
                f"BUY SL retreated at stop_distance={stop_distance}: "
                f"{prev_sl} -> {next_sl}"
            )
        else:
            assert next_sl <= prev_sl, (
                f"SELL SL retreated at stop_distance={stop_distance}: "
                f"{prev_sl} -> {next_sl}"
            )

    assert sl_history[-1] != sl_history[0]


def test_sell_sl_is_monotonically_non_increasing_across_an_adversarial_price_path():
    """Mirror of the BUY monotonicity test: a sharp favorable drop followed
    by a sharp pullback (price rising back up), asserting the SL never
    increases for a SELL."""
    rng = random.Random(1337)

    entry_price = 2400.0
    stop_distance = 15.0
    current_sl = entry_price + stop_distance

    prices = [entry_price]
    price = entry_price
    for _ in range(150):
        price += rng.uniform(-3.5, 3.0)  # slight downward bias, still noisy
        prices.append(price)

    # Adversarial segment: a sharp drop of -40 (favorable for SELL)...
    drop_start = prices[-1]
    for step in range(1, 6):
        prices.append(drop_start - step * 8.0)
    trough_price = prices[-1]

    # ...immediately followed by a sharp pullback (price rising) of +25.
    for step in range(1, 6):
        prices.append(trough_price + step * 5.0)

    price = prices[-1]
    for _ in range(150):
        price += rng.uniform(-3.5, 3.0)
        prices.append(price)

    sl_history = [current_sl]
    for p in prices:
        atr = rng.uniform(0.5, 6.0)
        current_sl = compute_updated_stop_loss(
            direction="SELL",
            current_sl=current_sl,
            entry_price=entry_price,
            initial_stop_distance=stop_distance,
            current_price=p,
            current_atr=atr,
            breakeven_at_r=BREAKEVEN_AT_R,
            trail_start_r=TRAIL_START_R,
            trail_distance_atr=TRAIL_DISTANCE_ATR,
        )
        sl_history.append(current_sl)

    for prev_sl, next_sl in zip(sl_history, sl_history[1:]):
        assert next_sl <= prev_sl, (
            f"SELL SL retreated: {prev_sl} -> {next_sl} -- violates the hard "
            "monotonicity invariant"
        )

    assert sl_history[-1] < sl_history[0]
