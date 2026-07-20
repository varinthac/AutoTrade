"""Tests for shield/checkpoint.py -- The Shield's 6-rule portfolio
checkpoint (trading_system_summary_v2.md Appendix A §2).

Each single-rule test constructs the minimal state to trip exactly one rule,
and asserts the other 5 do NOT also fire for an unrelated reason -- these
default fixtures (rr=2.0, no correlated same-direction exposure, well under
every count/ceiling) are deliberately "clean" so nothing but the rule under
test can block.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from autotrade.council.order_construction import OrderPlan
from autotrade.shield.checkpoint import OpenPositionInfo, Shield

T0 = datetime(2026, 7, 19, 9, 0)


class FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _shield(**overrides) -> Shield:
    defaults = dict(
        min_rr=1.5,
        max_correlation=0.7,
        max_positions_per_symbol=1,
        max_positions_total=3,
        total_risk_ceiling_pct=3.0,
        duplicate_signal_cooldown_hours=4.0,
    )
    defaults.update(overrides)
    return Shield(**defaults)


def _plan(direction="BUY", entry=100.0, stop_loss=95.0, take_profit=110.0) -> OrderPlan:
    return OrderPlan(
        direction=direction, entry=entry, stop_loss=stop_loss, take_profit=take_profit,
        stop_distance=abs(entry - stop_loss),
    )


def _check(shield, plan=None, symbol="XAUUSD", open_positions=None, new_trade_risk_pct=0.5,
           swing_index=1, clock=None):
    return shield.check(
        order_plan=plan or _plan(),
        symbol=symbol,
        open_positions=open_positions or [],
        new_trade_risk_pct=new_trade_risk_pct,
        swing_index=swing_index,
        clock=clock or FixedClock(T0),
    )


def _assert_only(decision, blocked_field: str | None):
    """Assert exactly `blocked_field` (or none, if None) is the tripped
    rule."""
    fields = [
        "min_rr_blocked", "correlation_blocked", "max_per_symbol_blocked",
        "max_total_blocked", "risk_ceiling_blocked", "cooldown_blocked",
    ]
    for field in fields:
        expected = field == blocked_field
        assert getattr(decision, field) is expected, f"{field} expected {expected}"
    assert decision.blocked is (blocked_field is not None)


def test_clean_state_is_approved():
    decision = _check(_shield())
    _assert_only(decision, None)
    assert decision.reasons == []


def test_rule1_min_rr_blocks_when_reward_risk_below_minimum():
    # reward=4, risk=5 -> rr=0.8, below min_rr=1.5.
    plan = _plan(take_profit=104.0)
    decision = _check(_shield(), plan=plan)
    _assert_only(decision, "min_rr_blocked")
    assert "R:R" in decision.min_rr_reason


def test_rule2_correlation_blocks_same_direction_correlated_open_position():
    open_positions = [OpenPositionInfo(symbol="EURUSD", direction="BUY", risk_pct=0.5)]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="GBPUSD", open_positions=open_positions,
    )
    _assert_only(decision, "correlation_blocked")
    assert "EURUSD" in decision.correlation_reason


def test_rule2_correlation_does_not_block_opposite_direction():
    # Same correlated pair, but the open position is SELL while the new
    # trade is BUY -- rule 2 only guards same-direction correlated exposure.
    open_positions = [OpenPositionInfo(symbol="EURUSD", direction="SELL", risk_pct=0.5)]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="GBPUSD", open_positions=open_positions,
    )
    _assert_only(decision, None)


def test_rule3_max_per_symbol_blocks_second_position_same_symbol():
    # Opposite direction from the existing position, so rule 2's
    # same-direction correlation guard can't also fire here.
    open_positions = [OpenPositionInfo(symbol="XAUUSD", direction="SELL", risk_pct=0.3)]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="XAUUSD", open_positions=open_positions,
    )
    _assert_only(decision, "max_per_symbol_blocked")
    assert "XAUUSD" in decision.max_per_symbol_reason


def test_rule4_max_total_blocks_at_portfolio_wide_ceiling():
    # 3 open positions on 3 different (uncorrelated-by-direction) symbols,
    # max_positions_total=3 -- a 4th anywhere is blocked purely on count.
    open_positions = [
        OpenPositionInfo(symbol="EURUSD", direction="SELL", risk_pct=0.1),
        OpenPositionInfo(symbol="GBPUSD", direction="SELL", risk_pct=0.1),
        OpenPositionInfo(symbol="USDJPY", direction="SELL", risk_pct=0.1),
    ]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="XAUUSD", open_positions=open_positions,
    )
    _assert_only(decision, "max_total_blocked")
    assert "3" in decision.max_total_reason


def test_rule5_risk_ceiling_not_blocked_when_exactly_at_ceiling():
    # existing 2.5% + new 0.5% = 3.0% == ceiling -- "must not exceed" means
    # exactly at the ceiling is still allowed.
    open_positions = [OpenPositionInfo(symbol="USDJPY", direction="SELL", risk_pct=2.5)]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="XAUUSD",
        open_positions=open_positions, new_trade_risk_pct=0.5,
    )
    _assert_only(decision, None)


def test_rule5_risk_ceiling_blocks_when_pushed_over():
    # existing 2.6% + new 0.5% = 3.1% > 3.0% ceiling.
    open_positions = [OpenPositionInfo(symbol="USDJPY", direction="SELL", risk_pct=2.6)]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="XAUUSD",
        open_positions=open_positions, new_trade_risk_pct=0.5,
    )
    _assert_only(decision, "risk_ceiling_blocked")
    assert "3.10" in decision.risk_ceiling_reason or "3.1" in decision.risk_ceiling_reason


def test_rule6_cooldown_blocks_same_swing_index_within_window():
    shield = _shield()
    shield.record_trade_opened(symbol="XAUUSD", direction="BUY", opened_at=T0, swing_index=42)

    decision = _check(
        shield, plan=_plan(direction="BUY"), symbol="XAUUSD",
        swing_index=42, clock=FixedClock(T0 + timedelta(hours=3)),
    )
    _assert_only(decision, "cooldown_blocked")
    assert "42" in decision.cooldown_reason


def test_rule6_cooldown_approved_after_window_elapses_same_swing():
    shield = _shield()
    shield.record_trade_opened(symbol="XAUUSD", direction="BUY", opened_at=T0, swing_index=42)

    decision = _check(
        shield, plan=_plan(direction="BUY"), symbol="XAUUSD",
        swing_index=42, clock=FixedClock(T0 + timedelta(hours=4)),
    )
    _assert_only(decision, None)


def test_rule6_cooldown_bypassed_by_different_swing_index_within_window():
    shield = _shield()
    shield.record_trade_opened(symbol="XAUUSD", direction="BUY", opened_at=T0, swing_index=42)

    decision = _check(
        shield, plan=_plan(direction="BUY"), symbol="XAUUSD",
        swing_index=99, clock=FixedClock(T0 + timedelta(hours=1)),
    )
    _assert_only(decision, None)


def test_rule6_cooldown_only_applies_to_same_symbol_and_direction():
    shield = _shield()
    shield.record_trade_opened(symbol="XAUUSD", direction="BUY", opened_at=T0, swing_index=42)

    # Same symbol, opposite direction -- no recorded cooldown state for
    # (XAUUSD, SELL), so this is unaffected.
    decision = _check(
        shield, plan=_plan(direction="SELL", entry=100.0, stop_loss=105.0, take_profit=90.0),
        symbol="XAUUSD", swing_index=42, clock=FixedClock(T0 + timedelta(hours=1)),
    )
    _assert_only(decision, None)


def test_multiple_rules_can_block_simultaneously():
    plan = _plan(take_profit=104.0)  # rr=0.8, trips rule 1
    open_positions = [
        OpenPositionInfo(symbol="XAUUSD", direction="BUY", risk_pct=0.5),
    ]  # trips rule 3 (same symbol) via BUY/BUY, also rule 2 (self-correlation 1.0)
    decision = _check(_shield(), plan=plan, symbol="XAUUSD", open_positions=open_positions)

    assert decision.blocked
    assert decision.min_rr_blocked
    assert decision.max_per_symbol_blocked
    assert len(decision.reasons) >= 2


def test_multiple_rules_block_reasons_lists_every_triggered_rule_exactly():
    # Precisely 3 of the 6 rules trip here (min_rr, correlation via
    # same-symbol self-correlation=1.0, max_per_symbol) -- unlike the test
    # above (which only loosely asserts ">= 2"), this pins down that ALL
    # THREE fire and the other 3 do NOT, so `.reasons` can't silently drop
    # one of them without this test catching it.
    plan = _plan(take_profit=104.0)  # reward=4, risk=5 -> rr=0.8, trips rule 1
    open_positions = [
        OpenPositionInfo(symbol="XAUUSD", direction="BUY", risk_pct=0.5),
    ]
    decision = _check(_shield(), plan=plan, symbol="XAUUSD", open_positions=open_positions)

    assert decision.min_rr_blocked is True
    assert decision.correlation_blocked is True
    assert decision.max_per_symbol_blocked is True
    assert decision.max_total_blocked is False
    assert decision.risk_ceiling_blocked is False
    assert decision.cooldown_blocked is False
    assert len(decision.reasons) == 3
    assert decision.min_rr_reason in decision.reasons
    assert decision.correlation_reason in decision.reasons
    assert decision.max_per_symbol_reason in decision.reasons


def test_rule1_min_rr_exactly_at_minimum_is_not_blocked():
    # reward=7.5, risk=5 -> rr=1.5 == min_rr exactly. "R:R must be >= min_rr"
    # means exactly-at-minimum must NOT block (mirrors rule 5's
    # exactly-at-ceiling convention: boundary value is always the safe side).
    plan = _plan(entry=100.0, stop_loss=95.0, take_profit=107.5)
    decision = _check(_shield(), plan=plan)
    _assert_only(decision, None)


def test_rule1_min_rr_just_under_minimum_blocks():
    # reward=7.49, risk=5 -> rr=1.498, just barely below min_rr=1.5.
    plan = _plan(entry=100.0, stop_loss=95.0, take_profit=107.49)
    decision = _check(_shield(), plan=plan)
    _assert_only(decision, "min_rr_blocked")


def test_rule2_correlation_guard_finds_correlated_position_even_when_not_first_in_list():
    # 2 open positions: the FIRST is same-direction but NOT correlated
    # (USDJPY vs GBPUSD correlation is -0.20), the SECOND is same-direction
    # AND correlated (EURUSD vs GBPUSD correlation is 0.85). An
    # implementation that only inspected open_positions[0] (or stopped at
    # the first non-matching entry instead of scanning the whole list)
    # would wrongly approve this trade.
    open_positions = [
        OpenPositionInfo(symbol="USDJPY", direction="BUY", risk_pct=0.1),
        OpenPositionInfo(symbol="EURUSD", direction="BUY", risk_pct=0.1),
    ]
    decision = _check(
        _shield(), plan=_plan(direction="BUY"), symbol="GBPUSD", open_positions=open_positions,
    )
    _assert_only(decision, "correlation_blocked")
    assert "EURUSD" in decision.correlation_reason
    assert "USDJPY" not in decision.correlation_reason


def test_rule2_correlation_does_not_block_when_corr_exactly_equals_max_correlation():
    # checkpoint.py's rule 2 uses `corr > max_correlation`, matching
    # trading_system_summary_v2.md Appendix A §2 rule 2's exact wording
    # ("correlation > 0.7") and rule 5's strict boundary convention.
    # EURUSD/GBPUSD's table correlation is 0.85 -- set max_correlation to
    # exactly 0.85 so corr == max_correlation, and confirm this boundary
    # does NOT block (exactly-at-threshold is the safe side, same as rule 5's
    # exactly-at-ceiling convention).
    open_positions = [OpenPositionInfo(symbol="EURUSD", direction="BUY", risk_pct=0.1)]
    decision = _check(
        _shield(max_correlation=0.85), plan=_plan(direction="BUY"), symbol="GBPUSD",
        open_positions=open_positions,
    )
    _assert_only(decision, None)


def test_rule2_correlation_does_not_block_when_corr_just_below_max_correlation():
    # Same pair/correlation (0.85), but max_correlation set just above it
    # (0.86) -- corr < max_correlation, must NOT block. Contrasts directly
    # with the exactly-at-boundary test above.
    open_positions = [OpenPositionInfo(symbol="EURUSD", direction="BUY", risk_pct=0.1)]
    decision = _check(
        _shield(max_correlation=0.86), plan=_plan(direction="BUY"), symbol="GBPUSD",
        open_positions=open_positions,
    )
    _assert_only(decision, None)


def test_rule6_cooldown_never_blocks_brand_new_symbol_direction_even_with_other_state_recorded():
    # Cooldown state is keyed by (symbol, direction) -- recording a trade for
    # a DIFFERENT symbol (even with a swing_index that happens to match what
    # this check will be asked about) must never leak into a symbol+direction
    # that has never itself been recorded. A buggy implementation that keyed
    # only on swing_index (not the (symbol, direction) tuple) would wrongly
    # block this.
    shield = _shield()
    shield.record_trade_opened(symbol="EURUSD", direction="BUY", opened_at=T0, swing_index=5)

    decision = _check(
        shield, plan=_plan(direction="BUY"), symbol="XAUUSD",
        swing_index=5, clock=FixedClock(T0),  # 0 elapsed hours, well within any cooldown
    )
    _assert_only(decision, None)
