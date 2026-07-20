"""The Shield -- Portfolio Checkpoint, per trading_system_summary_v2.md
Appendix A §2 and spec.md §2.2's data-flow ordering: `council/ scores ... ->
shield/ filters on RRR / correlated exposure / directional bias -> risk/
sizes the position ...`. Shield evaluates the `OrderPlan` council already
produced (entry/stop_loss/take_profit/stop_distance are all fixed by then,
so R:R is directly computable) BEFORE lot sizing happens -- it never needs
or computes a lot size itself.

Six rules (Appendix A §2), lightest to heaviest:
  1. Minimum R:R -- reward:risk must be >= `min_rr`.
  2. Correlation guard -- no new same-direction position in a symbol whose
     static correlation (shield/correlation.py) to an already-open
     same-direction position exceeds `max_correlation`.
  3. Max positions per symbol -- at most `max_positions_per_symbol` open
     positions on the same symbol.
  4. Max positions total -- at most `max_positions_total` open positions
     across the whole portfolio.
  5. Total risk ceiling -- sum of open positions' `risk_pct` + the new
     trade's `new_trade_risk_pct` must not exceed `total_risk_ceiling_pct`.
  6. Duplicate-signal cooldown -- same symbol + direction must be >=
     `duplicate_signal_cooldown_hours` since the last trade attempt on that
     symbol+direction, UNLESS a new confirmed swing has formed since then
     (`swing_index` differs from the one recorded at that trade).

Pure decision logic only: no I/O, no MT5 -- consumes plain dataclasses
(`OrderPlan`, `OpenPositionInfo`) and an injected `Clock`, per spec.md §2.3's
dependency-direction invariant. Same "fed events, call check() for the
current state" shape as `risk/circuit_breaker.py`'s `CircuitBreaker`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from autotrade.common.clock import Clock
from autotrade.council.order_construction import OrderPlan
from autotrade.shield.correlation import get_correlation


@dataclass(frozen=True)
class OpenPositionInfo:
    """An open position, from Shield's point of view. `risk_pct` is "how
    much of current equity this position has at risk" -- computing that from
    raw broker state is the caller's job, not Shield's (see
    execution/adapter.py's `BrokerAdapter.get_open_positions()`)."""

    symbol: str
    direction: Literal["BUY", "SELL"]
    risk_pct: float


@dataclass(frozen=True)
class ShieldDecision:
    min_rr_blocked: bool
    min_rr_reason: str | None
    correlation_blocked: bool
    correlation_reason: str | None
    max_per_symbol_blocked: bool
    max_per_symbol_reason: str | None
    max_total_blocked: bool
    max_total_reason: str | None
    risk_ceiling_blocked: bool
    risk_ceiling_reason: str | None
    cooldown_blocked: bool
    cooldown_reason: str | None

    @property
    def blocked(self) -> bool:
        """True if any of the 6 rules blocks this trade idea."""
        return (
            self.min_rr_blocked
            or self.correlation_blocked
            or self.max_per_symbol_blocked
            or self.max_total_blocked
            or self.risk_ceiling_blocked
            or self.cooldown_blocked
        )

    @property
    def reasons(self) -> list[str]:
        """Every triggered rule's reason, for logging (empty if not
        `blocked`)."""
        return [
            reason
            for reason in (
                self.min_rr_reason, self.correlation_reason, self.max_per_symbol_reason,
                self.max_total_reason, self.risk_ceiling_reason, self.cooldown_reason,
            )
            if reason is not None
        ]


class Shield:
    """Fed successfully-placed trades via `record_trade_opened()`; call
    `check()` on every new trade idea (after council, before CFO sizing) to
    find out whether the portfolio-level checkpoint blocks it."""

    def __init__(
        self,
        min_rr: float,
        max_correlation: float,
        max_positions_per_symbol: int,
        max_positions_total: int,
        total_risk_ceiling_pct: float,
        duplicate_signal_cooldown_hours: float,
    ) -> None:
        self._min_rr = min_rr
        self._max_correlation = max_correlation
        self._max_positions_per_symbol = max_positions_per_symbol
        self._max_positions_total = max_positions_total
        self._total_risk_ceiling_pct = total_risk_ceiling_pct
        self._duplicate_signal_cooldown_hours = duplicate_signal_cooldown_hours

        # (symbol, direction) -> (opened_at, swing_index) of the last trade
        # attempt Shield approved and was told (via record_trade_opened) was
        # actually placed. In-memory only -- unlike CircuitBreaker's
        # drawdown halt this isn't a one-way safety latch, so it doesn't
        # need to survive a process restart.
        self._last_trade: dict[tuple[str, str], tuple[datetime, int]] = {}

    def record_trade_opened(
        self,
        symbol: str,
        direction: Literal["BUY", "SELL"],
        opened_at: datetime,
        swing_index: int,
    ) -> None:
        """Call after a trade Shield approved is successfully placed by the
        broker -- feeds rule 6's cooldown state. Must NOT be called for a
        trade Shield blocked, or one that failed later at CFO sizing/order
        placement."""
        self._last_trade[(symbol, direction)] = (opened_at, swing_index)

    def check(
        self,
        order_plan: OrderPlan,
        symbol: str,
        open_positions: list[OpenPositionInfo],
        new_trade_risk_pct: float,
        swing_index: int,
        clock: Clock,
    ) -> ShieldDecision:
        # Rule 1: minimum R:R, computed directly from the OrderPlan's
        # already-fixed entry/TP/stop_distance.
        reward = abs(order_plan.take_profit - order_plan.entry)
        rr = reward / order_plan.stop_distance if order_plan.stop_distance > 0 else 0.0
        min_rr_blocked = rr < self._min_rr
        min_rr_reason = (
            f"R:R {rr:.2f} below minimum {self._min_rr}" if min_rr_blocked else None
        )

        # Rule 2: correlation guard -- only same-direction open positions
        # count; a hedge in a correlated symbol is not what this rule
        # exists to catch.
        correlation_blocked = False
        correlation_reason = None
        for pos in open_positions:
            if pos.direction != order_plan.direction:
                continue
            corr = get_correlation(symbol, pos.symbol)
            if corr > self._max_correlation:
                correlation_blocked = True
                correlation_reason = (
                    f"same-direction {pos.direction} position already open on {pos.symbol}, "
                    f"correlation to {symbol} is {corr:.2f} > max {self._max_correlation}"
                )
                break

        # Rule 3: max positions per symbol -- regardless of direction.
        symbol_count = sum(1 for pos in open_positions if pos.symbol == symbol)
        max_per_symbol_blocked = symbol_count >= self._max_positions_per_symbol
        max_per_symbol_reason = (
            f"{symbol_count} open position(s) already on {symbol}, "
            f"max {self._max_positions_per_symbol} per symbol"
            if max_per_symbol_blocked else None
        )

        # Rule 4: max positions total, across the whole portfolio.
        total_count = len(open_positions)
        max_total_blocked = total_count >= self._max_positions_total
        max_total_reason = (
            f"{total_count} open position(s) across the portfolio, "
            f"max {self._max_positions_total} total"
            if max_total_blocked else None
        )

        # Rule 5: total risk ceiling -- existing open risk + this trade's
        # intended risk must not EXCEED the ceiling (equal to it is fine).
        existing_risk_pct = sum(pos.risk_pct for pos in open_positions)
        projected_risk_pct = existing_risk_pct + new_trade_risk_pct
        risk_ceiling_blocked = projected_risk_pct > self._total_risk_ceiling_pct
        risk_ceiling_reason = (
            f"existing open risk {existing_risk_pct:.2f}% + new trade risk "
            f"{new_trade_risk_pct:.2f}% = {projected_risk_pct:.2f}% would exceed ceiling "
            f"{self._total_risk_ceiling_pct}%"
            if risk_ceiling_blocked else None
        )

        # Rule 6: duplicate-signal cooldown. A different swing_index than
        # what was recorded bypasses the cooldown entirely, regardless of
        # elapsed time -- a genuinely new swing point means this isn't a
        # duplicate signal.
        cooldown_blocked = False
        cooldown_reason = None
        last = self._last_trade.get((symbol, order_plan.direction))
        if last is not None:
            last_opened_at, last_swing_index = last
            if last_swing_index == swing_index:
                elapsed_hours = (clock.now() - last_opened_at).total_seconds() / 3600
                if elapsed_hours < self._duplicate_signal_cooldown_hours:
                    cooldown_blocked = True
                    cooldown_reason = (
                        f"same confirmed swing (index {swing_index}) as the last "
                        f"{order_plan.direction} trade on {symbol}, only {elapsed_hours:.2f}h ago, "
                        f"cooldown is {self._duplicate_signal_cooldown_hours}h"
                    )

        return ShieldDecision(
            min_rr_blocked=min_rr_blocked, min_rr_reason=min_rr_reason,
            correlation_blocked=correlation_blocked, correlation_reason=correlation_reason,
            max_per_symbol_blocked=max_per_symbol_blocked, max_per_symbol_reason=max_per_symbol_reason,
            max_total_blocked=max_total_blocked, max_total_reason=max_total_reason,
            risk_ceiling_blocked=risk_ceiling_blocked, risk_ceiling_reason=risk_ceiling_reason,
            cooldown_blocked=cooldown_blocked, cooldown_reason=cooldown_reason,
        )
