"""Core event-driven backtest engine (spec.md §4 "Backtest engine" / §6 Phase
4). Replays through the *exact same* signal/order-construction/sizing
functions the live system uses (`council.decision_matrix.evaluate_council`,
via the `_council_signal_fn` adapter below, by default -- injected so a
different strategy is a drop-in replacement later) over historical
OHLC(+spread) data -- it does not reimplement any decision logic.

Risk Voice (`council/risk_voice.py`) IS wired into the default
`_council_signal_fn`, via an OPTIONAL `risk_voice_cfg: RiskVoiceConfig |
None` on `BacktestConfig` (default `None` -- explicitly means "not
modeled", same honesty-over-convenience placeholder convention as
`backtest/cost_model.py`'s `commission_per_lot=0.0`; `scripts/run_backtest.py`
always passes a real one for any run whose result might feed a promotion
decision, and `backtest/report.py`'s envelope records `risk_voice_modeled`
so a reader always knows which mode a given run used).

**One condition remains genuinely unmodeled even when `risk_voice_cfg` is
given**: Risk Voice's news-calendar veto. This project has no historical
economic-calendar dataset (see `backtest/news_stub.
NoHistoricalNewsDataProvider`'s docstring for the full reasoning) -- the
news condition always evaluates to "no event" here, never a real veto. The
other five conditions (spread, stop-distance, session, Friday-close,
ATR-panic) ARE modeled faithfully, using the bar's own spread/ATR and the
same 20-day rolling-average approximation (`features/indicators.
rolling_average`) `orchestrator/shadow_loop.py`'s live re-check uses, so
both stay consistent by construction. Unlike the live loop's twice-per-trade
re-check (signal-time + immediately-before-send), this engine only checks
once per signal -- there is no separate "order-send time" moment in a
backtest replay, so a second check would be identical to the first.

Watchman exits (`watchman/evaluate.evaluate_watchman`) ARE wired into this
engine too, via an OPTIONAL `watchman_cfg: WatchmanConfig | None` on
`BacktestConfig` (default `None` -- explicitly means "not modeled", same
placeholder convention as `risk_voice_cfg` above; EXP-002 in
`experiments/experiments_log.md` is the param-tuner finding that first
confirmed this engine never simulated breakeven/trail/structure/time-stop
exits, only fixed SL/TP/end-of-data). `evaluate_watchman` and its
sub-functions (`stop_logic.compute_updated_stop_loss`,
`exit_conditions.check_structure_invalidation`/`check_time_stop`) are reused
verbatim, never reimplemented -- exactly the pure-decision-function reuse
`risk_voice_cfg` above established. `OrderPlan` doesn't carry the swing
index Watchman's structure-invalidation check needs, so at fill time this
engine re-derives it the same way `orchestrator/shadow_loop.py`'s live loop
does (`latest_confirmed_swing_low`/`latest_confirmed_swing_high`, same
`pivot_bars` the signal was built with), then builds an in-memory
`PositionMetadata` (synthetic `ticket=0` -- there is no real broker ticket
in a backtest) that lives only for that trade's lifetime.

**One Watchman sub-condition remains genuinely unmodeled**: news protection
(`watchman/news_protection.py`) -- same missing-historical-news-calendar gap
as Risk Voice's news veto above, and for the same reason not attempted here.

Shield's duplicate-signal cooldown (rule 6, `shield/checkpoint.py`) is ALSO
wired in, via an OPTIONAL `shield_cfg: ShieldConfig | None` on
`BacktestConfig` (default `None`, same placeholder convention as
`risk_voice_cfg`/`watchman_cfg` above). A fresh `Shield` is instantiated once
per `run_backtest` call and consulted right after `config.signal_fn` returns
a plan, using the same swing-index re-derivation `_build_watchman_metadata`
uses at fill time (here done at SIGNAL time instead, matching
`orchestrator/shadow_loop.py`'s live ordering: check before the order is
placed, `record_trade_opened` only once it actually fills). Shield's other 5
rules are exercised too (never reimplemented, the real `Shield.check()` is
called) but are structurally inert in this single-position engine: `min_rr`
is always satisfied because `tp_r_multiple` fixes R:R at exactly 2.0, and
`open_positions` passed to `check()` is always `[]` (this engine only ever
holds one position, so correlation/max-positions/risk-ceiling can never
evaluate against a second one) -- see `experiments/experiments_log.md`'s
2026-07-22 Shield NOTE for the full analysis of why only the cooldown was a
consequential live/backtest divergence today. Defensively (same "should not
normally happen" fallback as `_build_watchman_metadata`), a signal with no
re-derivable confirmed swing at signal time skips the Shield check entirely
rather than crashing.

**Per-bar ordering/timing convention (a real backtest-methodology decision,
documented here rather than left implicit)**: for every bar an already-open
position is still open, the existing fixed SL/TP exit (`check_exit`) is
checked FIRST, against the position's *currently tracked* stop level
(`_OpenPosition.current_sl`, which starts at `plan.stop_loss` and only ever
moves via a Watchman `MODIFY_SL`) -- this preserves the SL-priority-on-
double-touch and weekend-gap-aware fill conventions below completely
unchanged. Only if that check does NOT exit the trade is `evaluate_watchman`
consulted, using `current_price`/`now` = this bar's close/timestamp and
`current_atr` computed the same way `watchman/loop.py`'s live `_current_atr`
does. A `CLOSE` decision (`exit_reason="structure_invalidation"` or
`"time_stop"`) closes the trade at THIS bar's close price. A `MODIFY_SL`
decision only updates `current_sl` -- the new, tighter stop is never
checked against for an exit until the NEXT bar's `check_exit` call (no
look-ahead: the bar that produces a tighter stop can never itself be the bar
that gets stopped out by it).

Guardrails (spec.md §4), enforced structurally, not just documented:
- Decisions only read closed bars: the signal function is called with
  `as_of_index=i`, one full loop iteration *before* bar `i + 1` (the fill
  bar) is even looked at.
- Fills happen at next-bar open, with modeled spread: a signal fired at bar
  `i` becomes a `_PendingOrder` and is only filled at bar `i + 1`'s `open`.
- No MT5 import anywhere in this module -- `SymbolSpec` is a plain input the
  caller resolves once before the backtest starts (spec.md §2.1's `backtest/`
  entry: "Replay the *same* pipeline code over historical ... data"; §2.3's
  dependency-direction invariant).

Cost-model convention (a real backtest-methodology decision, documented
here rather than left implicit):
- Spread + slippage are baked directly into the recorded ENTRY fill price
  (`open + cost` for BUY, `open - cost` for SELL) -- that shift *is* the
  price actually paid, so `gross_pnl` computed from it is already net of
  spread/slippage (NOT net of commission), matching how "gross P&L" is
  conventionally used in retail trading (before commission, after the
  bid/ask you actually traded at). `cost` on the trade record is commission
  only (`cost_model.commission_cost`) -- applying `cost_model.
  round_trip_cost` (which bundles spread+slippage+commission) *again* at
  trade close would double-count the spread/slippage already reflected in
  the fill price. `round_trip_cost` exists for a different caller shape
  (nominal, non-fill-adjusted prices) -- see its docstring. The currency
  amount of that baked-in spread/slippage is not discarded, though: it is
  captured separately on `ClosedTrade.spread_slippage_cost`, so a caller
  wanting the *total* round-trip cost of a trade (spread + slippage +
  commission) sums `cost + spread_slippage_cost` rather than reading `cost`
  alone -- see `ClosedTrade`'s field docstrings.
- Take-profit always fills at its nominal price, even if a bar gaps through
  it favorably -- modeled as a resting limit order (no assumed positive
  slippage), consistent with defaulting to the worse outcome whenever
  intrabar/gap ordering is ambiguous.
- Stop-loss fills at its nominal price UNLESS the bar's own OPEN has already
  gapped past it (weekend gap or any other session gap) -- in that case the
  exit fills at that bar's actual OPEN, the real, worse price, per spec.md
  §6 Phase 4's explicit "including weekend gap bars" proof point. Modeled as
  a stop/market order: genuinely subject to negative slippage through gaps.
- Same-bar-touches-both (a bar's high/low range technically reaches both SL
  and TP, with no gap-through-open): stop-loss takes priority. A real market
  can't tell us which was touched first intrabar, so this assumes the worse
  outcome for us rather than the better one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import pandas as pd

from autotrade.backtest.clock import SimulatedClock
from autotrade.backtest.cost_model import CostModelConfig, commission_cost, spread_slippage_price
from autotrade.backtest.news_stub import NoHistoricalNewsDataProvider
from autotrade.common.clock import Clock
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.decision_matrix import evaluate_council
from autotrade.council.order_construction import OrderPlan
from autotrade.council.risk_voice import RiskVoiceConfig, check_risk_voice
from autotrade.features.indicators import atr, rolling_average
from autotrade.features.swing import latest_confirmed_swing_high, latest_confirmed_swing_low
from autotrade.risk.sizing import compute_lot_size
from autotrade.shield.checkpoint import Shield, ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig, evaluate_watchman
from autotrade.watchman.position_metadata import PositionMetadata

_NO_HISTORICAL_NEWS_PROVIDER = NoHistoricalNewsDataProvider()

SignalFn = Callable[..., "OrderPlan | None"]


def _risk_voice_inputs(df: pd.DataFrame, as_of_index: int) -> dict[str, float]:
    """Same computation as `orchestrator/shadow_loop.py`'s
    `_risk_voice_inputs` -- current spread/ATR plus their 20-day rolling
    averages, from bars up to and including `as_of_index` only (never
    looking ahead)."""
    closes = df["close"].iloc[: as_of_index + 1]
    highs = df["high"].iloc[: as_of_index + 1]
    lows = df["low"].iloc[: as_of_index + 1]
    atr_series = atr(highs, lows, closes)
    return {
        "current_spread_points": float(df["spread"].iloc[as_of_index]),
        "avg_spread_points_20d": rolling_average(df["spread"], as_of_index),
        "current_atr": float(atr_series.iloc[-1]),
        "avg_atr_20d": rolling_average(atr_series, as_of_index),
    }


def _council_signal_fn(
    df: pd.DataFrame,
    as_of_index: int,
    *,
    symbol: str,
    symbol_spec: SymbolSpec,
    sl_buffer_atr: float,
    sl_min_atr: float,
    sl_max_atr: float,
    tp_r_multiple: float,
    pivot_bars: int,
    bull_threshold: int = 70,
    bear_threshold: int = 70,
    conflict_threshold: int = 55,
    risk_voice_cfg: RiskVoiceConfig | None = None,
    clock: Clock | None = None,
) -> OrderPlan | None:
    """Default `SignalFn` -- adapts `council.decision_matrix.evaluate_council`'s
    `(CouncilDecision, BorderlineCase | None)` return shape to the plain
    `OrderPlan | None` contract `run_backtest`'s loop expects. Borderline
    cases are not surfaced here (nowhere to log them to in a backtest run --
    that's `orchestrator/shadow_loop.py`'s `borderline_log.jsonl`, a
    live-loop-only concern) -- only a clean BUY/SELL decision ever becomes a
    trade.

    When `risk_voice_cfg` is given (never `None` for a real promotion-gate
    run -- see module docstring), a Council BUY/SELL decision is additionally
    passed through `check_risk_voice` before becoming a trade, using
    `_risk_voice_inputs` for the market-state numbers and
    `backtest.news_stub.NoHistoricalNewsDataProvider` for the news condition
    (see module docstring for why that's the one condition NOT genuinely
    modeled). `clock` must be given whenever `risk_voice_cfg` is (asserted
    below) -- `run_backtest`'s loop always passes its own ticking
    `SimulatedClock`."""
    decision, _borderline_case = evaluate_council(
        df, as_of_index, symbol, symbol_spec,
        bull_threshold=bull_threshold, bear_threshold=bear_threshold, conflict_threshold=conflict_threshold,
        sl_buffer_atr=sl_buffer_atr, sl_min_atr=sl_min_atr, sl_max_atr=sl_max_atr,
        tp_r_multiple=tp_r_multiple, pivot_bars=pivot_bars,
    )
    if decision.direction is None or decision.order_plan is None:
        return None

    if risk_voice_cfg is not None:
        assert clock is not None, "clock is required whenever risk_voice_cfg is given"
        risk_voice_decision = check_risk_voice(
            symbol=symbol, order_plan=decision.order_plan, news_provider=_NO_HISTORICAL_NEWS_PROVIDER,
            clock=clock, config=risk_voice_cfg, **_risk_voice_inputs(df, as_of_index),
        )
        if risk_voice_decision.vetoed:
            return None

    return decision.order_plan


@dataclass(frozen=True)
class ClosedTrade:
    """See module docstring's "Cost-model convention" section for the full
    rationale. In short: `gross_pnl` is NOT gross of all costs -- it is
    computed from `entry_price`, which already has spread/slippage baked in,
    so `gross_pnl` is only gross of commission. `cost` and
    `spread_slippage_cost` are the two cost components tracked separately;
    total round-trip cost for a trade is `cost + spread_slippage_cost`.
    """

    symbol: str
    direction: Literal["BUY", "SELL"]
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: Literal[
        "stop_loss", "take_profit", "end_of_data", "structure_invalidation", "time_stop"
    ]
    lot_size: float
    gross_pnl: float
    """P&L from `entry_price` (already spread/slippage-adjusted) to
    `exit_price`, before commission -- NOT gross of spread/slippage, only of
    commission."""
    cost: float
    """Commission only (`cost_model.commission_cost`)."""
    spread_slippage_cost: float
    """Spread + slippage cost in currency, already folded into `entry_price`
    (and thus into `gross_pnl`) at fill time -- tracked here separately so a
    reader summing "total trading cost" doesn't silently miss it by reading
    `cost` alone. Total round-trip cost = `cost + spread_slippage_cost`."""
    net_pnl: float
    r_multiple: float


@dataclass(frozen=True)
class BacktestConfig:
    """Strategy/risk/cost parameters for one backtest run. `signal_fn`
    defaults to `_council_signal_fn` (the real Council: Bull/Bear scoring +
    Decision Matrix, per Appendix A §1.1-§1.3, plus Risk Voice when
    `risk_voice_cfg` is given -- see module docstring) but is injected so it
    can be replaced without touching this engine -- it must accept `(df,
    as_of_index, symbol=, symbol_spec=, sl_buffer_atr=, sl_min_atr=,
    sl_max_atr=, tp_r_multiple=, pivot_bars=, risk_voice_cfg=, clock=) ->
    OrderPlan | None` (see `run_backtest`'s call site). `council.trivial_signal.
    build_trade_idea` (Phase 3) does not accept `symbol`/`symbol_spec` and so
    is no longer a drop-in `signal_fn` without a small adapter of its own.

    `current_atr`/`avg_atr_20d` volatility dampening (risk/sizing.py §3.2) is
    left disabled (both `None` at every `compute_lot_size` call), matching
    `orchestrator/shadow_loop.py`'s documented Phase-3 simplification -- a
    correct rolling 20-*trading*-day average ATR needs a bars-per-day
    constant per timeframe, which is more machinery than this phase needs;
    revisit together with that same shadow-loop simplification.
    """

    starting_equity: float
    risk_per_trade_pct: float
    cost_model: CostModelConfig
    sl_buffer_atr: float = 0.2
    sl_min_atr: float = 0.8
    sl_max_atr: float = 2.5
    tp_r_multiple: float = 2.0
    pivot_bars: int = 3
    bull_threshold: int = 70
    bear_threshold: int = 70
    conflict_threshold: int = 55
    signal_fn: SignalFn = _council_signal_fn
    risk_voice_cfg: RiskVoiceConfig | None = None
    """`None` (the default) means Risk Voice is NOT modeled in this run --
    an explicit placeholder, not silently-equivalent-to-passing (see module
    docstring). `scripts/run_backtest.py` always passes a real
    `RiskVoiceConfig` loaded from `config/base.yaml`; leaving this `None` is
    only appropriate for tests/tooling that don't need Risk Voice's veto
    behavior."""
    watchman_cfg: WatchmanConfig | None = None
    """`None` (the default) means Watchman's exit management (breakeven,
    ATR-trailing stop, structure-invalidation, time-stop) is NOT modeled in
    this run -- an explicit placeholder, not silently-equivalent-to-passing
    (see module docstring). Without it, this engine only ever exits a trade
    via its fixed SL/TP/end-of-data (EXP-002, `experiments/experiments_log.md`).
    `scripts/run_backtest.py` always passes a real `WatchmanConfig` loaded
    from `config/base.yaml`; leaving this `None` is only appropriate for
    tests/tooling that don't need Watchman's exit behavior."""
    min_lot_risk_cap_pct: float | None = None
    """`None` (the default) means `risk.sizing.compute_lot_size`'s min-lot
    risk-cap fallback is NOT modeled -- spec-exact §3.1 behavior, zero
    change. See that function's docstring for the exact deliberate-deviation
    mechanics; `config/base.yaml`'s `cfo.min_lot_risk_cap_pct: 1.5` is the
    adopted live value, threaded through by `scripts/run_backtest.py`."""
    shield_cfg: ShieldConfig | None = None
    """`None` (the default) means Shield's duplicate-signal cooldown is NOT
    modeled in this run -- an explicit placeholder, not silently-equivalent-
    to-passing (see module docstring's Shield paragraph). `scripts/
    run_backtest.py` always passes a real `ShieldConfig` loaded from
    `config/base.yaml`'s `shield:` block; leaving this `None` is only
    appropriate for tests/tooling that don't need the cooldown gate."""


@dataclass
class _PendingOrder:
    plan: OrderPlan
    lot_size: float
    signal_index: int
    """The bar index `config.signal_fn` was called with when this order was
    decided -- needed at fill time to re-derive `PositionMetadata.
    entry_swing_index` (see module docstring's Watchman section)."""
    swing_index: int | None
    """The swing index Shield's rule 6 checked this signal against, re-
    derived at SIGNAL time (see module docstring's Shield paragraph) --
    carried forward so `record_trade_opened` can be told the same value once
    this order actually fills. `None` when `config.shield_cfg` is not set,
    or (defensively) when no confirmed swing existed at signal time."""


@dataclass
class _OpenPosition:
    plan: OrderPlan
    lot_size: float
    entry_time: pd.Timestamp
    entry_price: float
    spread_slippage_price_delta: float
    """The spread+slippage price-unit delta baked into `entry_price` at fill
    time (see `_fill_entry_price`) -- kept around so `_close_trade` can
    convert it to currency for `ClosedTrade.spread_slippage_cost`."""
    current_sl: float
    """The position's CURRENTLY tracked stop level -- starts at
    `plan.stop_loss` and only ever moves via a Watchman `MODIFY_SL` decision
    (see module docstring's per-bar ordering section). `_close_trade`'s
    `risk_amount`/`r_multiple` still use `plan.stop_distance` (the ORIGINAL
    fixed risk), never this trailed value."""
    metadata: PositionMetadata | None
    """In-memory Watchman position metadata built at fill time when
    `BacktestConfig.watchman_cfg is not None` (see `_build_watchman_metadata`);
    `None` when Watchman exits are not modeled for this run, or (defensively)
    if no confirmed swing could be re-derived for this trade."""


def _fill_entry_price(
    direction: Literal["BUY", "SELL"],
    bar_open: float,
    bar_spread_points: float,
    symbol: SymbolSpec,
    cost_model: CostModelConfig,
) -> tuple[float, float]:
    """BUY fills at open+cost (pay more to buy), SELL fills at open-cost
    (receive less to sell) -- spec.md §4's fill guardrail, sign convention
    documented at module level. Returns `(entry_price, spread_slippage_price_
    delta)`, the latter in price units (not currency) for the caller to
    convert and record separately."""
    cost = spread_slippage_price(bar_spread_points, symbol, cost_model)
    entry_price = bar_open + cost if direction == "BUY" else bar_open - cost
    return entry_price, cost


def _watchman_current_atr(df: pd.DataFrame, as_of_index: int, period: int = 14) -> float:
    """Same computation as `watchman/loop.py`'s `_current_atr` -- ATR(14)
    from bars up to and including `as_of_index` only (never looking ahead),
    used as Watchman's SL-trail distance input."""
    closes = df["close"].iloc[: as_of_index + 1]
    highs = df["high"].iloc[: as_of_index + 1]
    lows = df["low"].iloc[: as_of_index + 1]
    return float(atr(highs, lows, closes, period=period).iloc[-1])


def _swing_index_at(
    df: pd.DataFrame, as_of_index: int, direction: Literal["BUY", "SELL"], pivot_bars: int,
) -> int | None:
    """The confirmed swing Shield's rule 6 keys its cooldown state on for a
    signal firing at `as_of_index` -- same `latest_confirmed_swing_low`/
    `latest_confirmed_swing_high` call `_build_watchman_metadata` makes at
    fill time, and `orchestrator/shadow_loop.py`'s live loop makes at signal
    time, `None` if no confirmed swing exists yet (see module docstring)."""
    swing = (
        latest_confirmed_swing_low(df, as_of_index, pivot_bars=pivot_bars)
        if direction == "BUY"
        else latest_confirmed_swing_high(df, as_of_index, pivot_bars=pivot_bars)
    )
    return swing[0] if swing is not None else None


def _build_watchman_metadata(
    symbol: str,
    plan: OrderPlan,
    entry_price: float,
    entry_time: pd.Timestamp,
    df: pd.DataFrame,
    signal_index: int,
    pivot_bars: int,
) -> PositionMetadata | None:
    """Re-derives the swing `entry_swing_index` that justified `plan`'s
    stop-loss -- `OrderPlan` doesn't carry it, so this mirrors
    `orchestrator/shadow_loop.py`'s live re-derivation (same
    `latest_confirmed_swing_low`/`latest_confirmed_swing_high` call, same
    `pivot_bars`, same `as_of_index` the signal itself was evaluated at)
    rather than widening the `SignalFn` contract. Returns `None`
    (defensively -- `evaluate_council`/`build_order_plan` already require a
    confirmed swing to build `plan` in the first place, so this should not
    normally happen) if no confirmed swing is found."""
    if plan.direction == "BUY":
        swing = latest_confirmed_swing_low(df, signal_index, pivot_bars=pivot_bars)
    else:
        swing = latest_confirmed_swing_high(df, signal_index, pivot_bars=pivot_bars)
    if swing is None:
        return None
    swing_index, _swing_price = swing
    return PositionMetadata(
        ticket=0,
        symbol=symbol,
        direction=plan.direction,
        entry_price=entry_price,
        initial_stop_distance=plan.stop_distance,
        entry_swing_index=swing_index,
        opened_at=entry_time.to_pydatetime(),
    )


def _classify_watchman_exit_reason(
    reason: str,
) -> Literal["structure_invalidation", "time_stop"]:
    """`evaluate_watchman`'s `WatchmanDecision.reason` is a free-text human
    message, not a machine-readable code -- but its only two `CLOSE` cases
    each start with a fixed, known prefix (see `watchman/evaluate.py`), same
    mapping `watchman/loop.py`'s `_classify_watchman_close_reason` uses for
    the live trade journal. Unlike that live version (which must never crash
    the loop over an MT5-facing surprise), an unrecognized reason here is a
    genuine code bug in this deterministic pure-function pairing, so this
    raises rather than silently mis-labeling a trade's exit_reason."""
    if reason.startswith("structure invalidation"):
        return "structure_invalidation"
    if reason.startswith("time stop"):
        return "time_stop"
    raise ValueError(f"unrecognized Watchman CLOSE reason: {reason!r}")


def check_exit(
    direction: Literal["BUY", "SELL"],
    stop_loss: float,
    take_profit: float,
    bar: "pd.Series",
) -> tuple[float, Literal["stop_loss", "take_profit"]] | None:
    """See module docstring for the documented SL/TP/gap priority convention.
    Public (used by `backtest/forward_walk.py`'s Auditor borderline-order
    replay, Appendix A §5.4, so that replay reuses the exact same SL/TP/gap
    priority this engine applies to real trades rather than a second,
    subtly-different simulation)."""
    if direction == "BUY":
        if bar["open"] <= stop_loss:
            return bar["open"], "stop_loss"
        sl_touched = bar["low"] <= stop_loss
        tp_touched = bar["high"] >= take_profit
    else:
        if bar["open"] >= stop_loss:
            return bar["open"], "stop_loss"
        sl_touched = bar["high"] >= stop_loss
        tp_touched = bar["low"] <= take_profit

    if sl_touched:
        return stop_loss, "stop_loss"
    if tp_touched:
        return take_profit, "take_profit"
    return None


def _close_trade(
    symbol: str,
    position: _OpenPosition,
    exit_time: pd.Timestamp,
    exit_price: float,
    exit_reason: Literal[
        "stop_loss", "take_profit", "end_of_data", "structure_invalidation", "time_stop"
    ],
    point_value: float,
    cost_model: CostModelConfig,
) -> ClosedTrade:
    sign = 1.0 if position.plan.direction == "BUY" else -1.0
    gross_pnl = sign * (exit_price - position.entry_price) * point_value * position.lot_size
    cost = commission_cost(position.lot_size, cost_model)
    spread_slippage_cost = position.spread_slippage_price_delta * point_value * position.lot_size
    net_pnl = gross_pnl - cost
    risk_amount = position.plan.stop_distance * point_value * position.lot_size
    r_multiple = net_pnl / risk_amount if risk_amount else 0.0

    return ClosedTrade(
        symbol=symbol,
        direction=position.plan.direction,
        entry_time=position.entry_time,
        entry_price=position.entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        lot_size=position.lot_size,
        gross_pnl=gross_pnl,
        cost=cost,
        spread_slippage_cost=spread_slippage_cost,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
    )


def run_backtest(
    df: pd.DataFrame,
    symbol: str,
    symbol_spec: SymbolSpec,
    config: BacktestConfig,
) -> list[ClosedTrade]:
    """Walk `df` (columns: `time, open, high, low, close, spread`, contiguous
    0..n-1 index) bar by bar, replaying `config.signal_fn` + `risk.sizing.
    compute_lot_size` + this module's fill/exit simulation. Returns the raw
    closed-trade list, including a final `exit_reason="end_of_data"` entry if
    a position is still open when `df` runs out (never silently dropped).

    Equity compounds: `compute_lot_size` is called with the running equity
    (starting equity + realized net P&L of trades closed so far), not a
    fixed starting value, so position sizing reflects the account's actual
    trajectory through the backtest.
    """
    if len(df) < 2:
        return []

    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    equity = config.starting_equity
    clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
    shield = Shield(
        min_rr=config.shield_cfg.min_rr,
        max_correlation=config.shield_cfg.max_correlation,
        max_positions_per_symbol=config.shield_cfg.max_positions_per_symbol,
        max_positions_total=config.shield_cfg.max_positions_total,
        total_risk_ceiling_pct=config.shield_cfg.total_risk_ceiling_pct,
        duplicate_signal_cooldown_hours=config.shield_cfg.duplicate_signal_cooldown_hours,
    ) if config.shield_cfg is not None else None

    pending: _PendingOrder | None = None
    position: _OpenPosition | None = None
    trades: list[ClosedTrade] = []

    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())

        if pending is not None:
            entry_price, spread_slippage_price_delta = _fill_entry_price(
                pending.plan.direction, bar["open"], bar["spread"], symbol_spec, config.cost_model
            )
            entry_time = pd.Timestamp(bar["time"])
            if shield is not None and pending.swing_index is not None:
                shield.record_trade_opened(
                    symbol=symbol, direction=pending.plan.direction,
                    opened_at=entry_time.to_pydatetime(), swing_index=pending.swing_index,
                )
            metadata = None
            if config.watchman_cfg is not None:
                metadata = _build_watchman_metadata(
                    symbol, pending.plan, entry_price, entry_time, df, pending.signal_index, config.pivot_bars,
                )
            position = _OpenPosition(
                plan=pending.plan,
                lot_size=pending.lot_size,
                entry_time=entry_time,
                entry_price=entry_price,
                spread_slippage_price_delta=spread_slippage_price_delta,
                current_sl=pending.plan.stop_loss,
                metadata=metadata,
            )
            pending = None

        if position is not None:
            exit_result = check_exit(position.plan.direction, position.current_sl, position.plan.take_profit, bar)
            if exit_result is not None:
                exit_price, exit_reason = exit_result
                trades.append(
                    _close_trade(
                        symbol, position, pd.Timestamp(bar["time"]), exit_price, exit_reason,
                        point_value, config.cost_model,
                    )
                )
                equity += trades[-1].net_pnl
                position = None
            elif config.watchman_cfg is not None and position.metadata is not None:
                decision = evaluate_watchman(
                    position_metadata=position.metadata,
                    current_sl=position.current_sl,
                    current_price=float(bar["close"]),
                    current_atr=_watchman_current_atr(df, i),
                    df=df,
                    as_of_index=i,
                    now=pd.Timestamp(bar["time"]).to_pydatetime(),
                    config=config.watchman_cfg,
                )
                if decision.action == "CLOSE":
                    trades.append(
                        _close_trade(
                            symbol, position, pd.Timestamp(bar["time"]), float(bar["close"]),
                            _classify_watchman_exit_reason(decision.reason),
                            point_value, config.cost_model,
                        )
                    )
                    equity += trades[-1].net_pnl
                    position = None
                elif decision.action == "MODIFY_SL":
                    position.current_sl = decision.new_stop_loss

        if position is None and pending is None:
            plan = config.signal_fn(
                df, i, symbol=symbol, symbol_spec=symbol_spec,
                sl_buffer_atr=config.sl_buffer_atr, sl_min_atr=config.sl_min_atr,
                sl_max_atr=config.sl_max_atr, tp_r_multiple=config.tp_r_multiple,
                pivot_bars=config.pivot_bars, bull_threshold=config.bull_threshold,
                bear_threshold=config.bear_threshold, conflict_threshold=config.conflict_threshold,
                risk_voice_cfg=config.risk_voice_cfg, clock=clock,
            )
            swing_index = None
            if plan is not None and shield is not None:
                swing_index = _swing_index_at(df, i, plan.direction, config.pivot_bars)
                if swing_index is not None:
                    shield_decision = shield.check(
                        order_plan=plan, symbol=symbol, open_positions=[],
                        new_trade_risk_pct=config.risk_per_trade_pct, swing_index=swing_index, clock=clock,
                    )
                    if shield_decision.blocked:
                        plan = None
            if plan is not None:
                lot = compute_lot_size(
                    equity=equity, risk_per_trade_pct=config.risk_per_trade_pct,
                    entry=plan.entry, stop_loss=plan.stop_loss, point_value=point_value,
                    volume_min=symbol_spec.volume_min, volume_max=symbol_spec.volume_max,
                    volume_step=symbol_spec.volume_step,
                    min_lot_risk_cap_pct=config.min_lot_risk_cap_pct,
                )
                if lot is not None and i + 1 < len(df):
                    pending = _PendingOrder(plan=plan, lot_size=lot, signal_index=i, swing_index=swing_index)
                # else: below broker minimum, or no next bar to fill on --
                # never becomes a trade, consistent with "not filled" rather
                # than a silently-wrong same-bar fill.

    if position is not None:
        last_bar = df.iloc[-1]
        trades.append(
            _close_trade(
                symbol, position, pd.Timestamp(last_bar["time"]), last_bar["close"], "end_of_data",
                point_value, config.cost_model,
            )
        )

    return trades
