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
from typing import Literal

import pandas as pd

from autotrade.common.symbol_spec import SymbolSpec


@dataclass(frozen=True)
class SwapModelConfig:
    """Overnight swap / rollover financing, the one cost component the live
    side ALREADY pays but the backtest historically did not -- a genuine
    backtest/live PARITY gap (see `store/models.py`'s `TradeRecord.cost`
    docstring and `execution/adapter.py`: on live, MT5's own swap figure is
    folded into `cost` alongside commission; this dataclass restores the same
    charge to the simulator). Added 2026-07-24 after a cross-project
    out-of-sample study (D:\\ForexTrade EXP-053..058) flagged that holding
    XAUUSD overnight ~1 night/trade makes swap material to expectancy in
    PF/R terms -- see `experiments/experiments_log.md` EXP-018.

    Sign convention (matches how a broker books it, and MT5's own sign):
    NEGATIVE = a debit charged to the trader, POSITIVE = a credit paid to the
    trader. The rates are per 1.0 (standard) lot per night, already in the
    account's deposit currency -- no point/tick conversion (unlike
    spread/slippage/commission arithmetic below), because a broker quotes swap
    directly in currency per lot.

    `triple_swap_weekday` (server-time weekday of the rollover boundary that
    is booked at 3x; Monday=0 .. Sunday=6, default Wednesday=2) models the
    standard "triple swap Wednesday" that compensates for the weekend's
    unbooked Sat/Sun carry. `rollover_hour` is the server-time hour of the
    daily rollover boundary (default 00:00 server). Saturday/Sunday rollover
    boundaries are booked at 0x here (the weekend carry is already recovered
    by the Wednesday 3x) -- the well-known consequence is that a position
    opened after Wednesday and closed the following Monday is under-charged
    for the weekend; this is faithful to how retail brokers actually book
    swap, not a modeling shortcut. Given the rates themselves are broker- and
    time-varying (~±20%), the ~1-day ambiguity in exactly which midnight
    carries the Wednesday 3x is immaterial and left configurable rather than
    hard-coded.

    There is intentionally NO "not modeled" default here: `None` on
    `CostModelConfig.swap_model` (and `BacktestConfig`/tooling that thread it)
    is the explicit "swap not modeled" placeholder, same honesty-over-
    convenience convention as `risk_voice_cfg`/`watchman_cfg`/`shield_cfg` in
    `backtest/engine.py`. A run that models swap must consciously supply real
    rates.
    """

    long_per_lot_per_night: float
    short_per_lot_per_night: float
    triple_swap_weekday: int = 2
    rollover_hour: int = 0


def effective_swap_nights(
    entry_time: pd.Timestamp, exit_time: pd.Timestamp, config: SwapModelConfig
) -> float:
    """Number of swap-charged nights between entry and exit, with the
    Wednesday rollover weighted 3x and Saturday/Sunday rollovers weighted 0x
    (see `SwapModelConfig` docstring). Counts every rollover boundary `t`
    (server midnight, or `config.rollover_hour`) with `entry_time < t <=
    exit_time` -- a position must be OPEN across the boundary to be charged,
    so an intraday trade that never crosses a rollover pays nothing."""
    if exit_time <= entry_time:
        return 0.0
    boundary = entry_time.normalize() + pd.Timedelta(hours=config.rollover_hour)
    if boundary <= entry_time:
        boundary += pd.Timedelta(days=1)
    nights = 0.0
    while boundary <= exit_time:
        dow = boundary.dayofweek
        if dow == config.triple_swap_weekday:
            nights += 3.0
        elif dow not in (5, 6):
            nights += 1.0
        # Sat(5)/Sun(6): 0x -- weekend carry is recovered by the Wednesday 3x.
        boundary += pd.Timedelta(days=1)
    return nights


def swap_cost(
    direction: Literal["BUY", "SELL"],
    lot_size: float,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    config: SwapModelConfig,
) -> float:
    """Overnight swap for one trade as a POSITIVE-when-charged currency amount
    to SUBTRACT from P&L (i.e. add into a `cost` field) -- mirroring
    `store/models.py`'s `TradeRecord.cost` convention exactly, so backtest and
    live agree. A swap CREDIT (e.g. the +short rate) returns a negative number
    that REDUCES total cost, again matching live. Signed the opposite of
    `SwapModelConfig`'s broker-booked rates because those are debit-negative
    while a `cost` is charge-positive."""
    rate = (
        config.long_per_lot_per_night if direction == "BUY" else config.short_per_lot_per_night
    )
    swap_pnl = rate * lot_size * effective_swap_nights(entry_time, exit_time, config)
    return -swap_pnl


@dataclass(frozen=True)
class CostModelConfig:
    """`commission_per_lot` defaults to `0.0` here only as a convenience for
    callers (tests, tooling) that don't need commission modeled at all --
    NOT every account genuinely pays commission (e.g. an IC Markets
    "Standard" account charges `0.0` and recovers its cost entirely through a
    wider spread, vs. a "Raw Spread" account's nonzero per-lot commission on
    top of a tighter spread). Because of this, `0.0` is NOT a reliable
    "forgot to configure this" signal -- a genuinely-zero-commission account
    and a never-configured placeholder are indistinguishable by value alone.
    `scripts/run_backtest.py`'s CLI enforces the real safeguard structurally
    instead: `--commission-per-lot` is a REQUIRED argument, so a promotion-
    relevant run can never silently inherit this dataclass default without
    the caller consciously choosing a value (`0.0` included) -- see that
    script's `build_envelope()` for how `cost_model_complete` is derived.

    `slippage_points` defaults to `None`, meaning "use the bar's own spread
    as the slippage assumption" -- Appendix A §5.2's "slippage สมมติขั้นต่ำ 1
    spread" (minimum 1 spread of assumed slippage). Pass an explicit value to
    override.
    """

    commission_per_lot: float = 0.0
    slippage_points: float | None = None
    swap_model: SwapModelConfig | None = None
    """`None` (the default) means overnight swap/rollover is NOT modeled in
    this run -- the explicit, honest placeholder (same convention as
    `slippage_points`/`commission_per_lot` above and
    `engine.py`'s `risk_voice_cfg`/`watchman_cfg`/`shield_cfg`). Live trading
    ALWAYS pays swap (`store/models.py`), so a promotion-relevant backtest of
    an overnight-holding strategy should supply a real `SwapModelConfig`;
    leaving this `None` is only appropriate for intraday tests/tooling or when
    deliberately isolating the pre-swap number. `scripts/run_backtest.py`
    surfaces this as `swap_modeled` in the report envelope, alongside the
    existing `risk_voice_modeled`/`watchman_exits_modeled`/`shield_modeled`
    honesty flags."""


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
