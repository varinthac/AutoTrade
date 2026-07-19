"""Cost model — spread + commission + slippage, per trading_system_summary_v2.md
Appendix A §5.2: "backtest ต้องรวม cost ครบ: spread เฉลี่ยจริงของโบรก +
commission + slippage สมมติขั้นต่ำ 1 spread — backtest ที่ไม่มี cost model =
ไม่นับ" (a backtest without a full cost model doesn't count toward any
promotion decision). This module is intentionally the one place that
arithmetic lives, so every backtest run pays it consistently.

Units: the `spread` column in `data/historical/*.csv` is in POINTS, matching
MT5's `SYMBOL_INFO`/rates convention (one "point" = `SymbolSpec.point` in
price terms, e.g. 0.01 for a 2-digit XAUUSD quote). Currency conversion uses
`point_value = tick_value / tick_size`, the same convention `risk/sizing.py`
uses.
"""
from __future__ import annotations

from dataclasses import dataclass

from autotrade.common.symbol_spec import SymbolSpec


@dataclass(frozen=True)
class CostModelConfig:
    """`commission_per_lot` defaults to `0.0` as an explicit PLACEHOLDER, not
    a real value -- it MUST be updated with this account's actual commission
    structure (currency per 1.0 lot round-trip) before any backtest result
    from this module is used for a promotion decision (Appendix A §5.2). A
    silently-wrong nonzero default would be worse than an honestly-flagged
    `0.0`: it would look like real costs were modeled when they weren't.

    `slippage_points` defaults to `None`, meaning "use the bar's own spread
    as the slippage assumption" -- Appendix A §5.2's "slippage สมมติขั้นต่ำ 1
    spread" (minimum 1 spread of assumed slippage). Pass an explicit value to
    override.
    """

    commission_per_lot: float = 0.0
    slippage_points: float | None = None


def spread_slippage_price(
    bar_spread_points: float, symbol: SymbolSpec, config: CostModelConfig
) -> float:
    """Spread + slippage, converted from points to price units (not
    currency) -- spread and slippage are additive components (Appendix A
    §5.2 lists them separately), so this is `(spread + slippage) * point`,
    not spread alone."""
    slippage_points = (
        config.slippage_points if config.slippage_points is not None else bar_spread_points
    )
    return (bar_spread_points + slippage_points) * symbol.point


def commission_cost(lot_size: float, config: CostModelConfig) -> float:
    """Commission in currency for one round-trip trade."""
    return config.commission_per_lot * lot_size


def round_trip_cost(
    entry_price: float,
    exit_price: float,
    lot_size: float,
    bar_spread_points: float,
    symbol: SymbolSpec,
    config: CostModelConfig,
) -> float:
    """Total round-trip cost in currency (spread + slippage + commission) to
    subtract from a NOMINAL gross P&L -- i.e. P&L computed from unadjusted
    entry/exit price levels that have not already had spread/slippage priced
    into them.

    `entry_price`/`exit_price` are accepted for exactly this use case: a
    caller (e.g. the Auditor's borderline-order replay, Appendix A §5.4,
    which replays a *hypothetical* order that was never actually filled and
    so has no fill-adjusted price) computes nominal gross P&L from these same
    two prices, then calls this function once to get the cost to subtract.
    They aren't needed by this particular (linear, non-notional) commission
    model's arithmetic -- only `lot_size` and `bar_spread_points` drive the
    currency amount -- but are kept in the signature so a future
    percentage-of-notional commission variant is a non-breaking addition.

    NOTE for `backtest/engine.py`: the engine does NOT call this function.
    It bakes spread/slippage directly into the recorded fill price instead
    (see `engine.py`'s entry-fill logic) and applies `commission_cost()`
    alone at trade close -- calling this function on top of that would
    double-count spread/slippage. This function is for nominal-price callers
    only.
    """
    del entry_price, exit_price
    point_value = symbol.tick_value / symbol.tick_size
    spread_slippage_currency = spread_slippage_price(bar_spread_points, symbol, config) * point_value * lot_size
    return spread_slippage_currency + commission_cost(lot_size, config)
