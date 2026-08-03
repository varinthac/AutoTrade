#!/usr/bin/env python3
"""EXP-023 harness -- news-protection min-lot fallback: CLOSE-ALL (mode A,
current live behavior) vs LOCK-STOP (mode B, `lock_frac`) at ~$3,000 equity.

WHY (2026-08-03): on the live $2,940 demo, 4 of the 5 winning trades in the
2026-07-22..2026-08-03 paper window closed with `exit_reason='news_protection'`
at +0.51R..+0.67R while TP (2R) was never reached once. Cause, in code:
`watchman/loop.py::_half_volume_rounded` returns None for a 0.01-lot position
(half rounds to 0.00 < `volume_min`), so `_act_on_news_decision`'s
CLOSE_HALF_AND_BREAKEVEN branch recurses into CLOSE_ALL. At min-lot sizing --
the norm on a $3k account (EXP-022) -- Appendix A 4.5's "close half, move the
rest to breakeven" therefore degenerates into "full exit at ~0.5R".

TWO CODE-READING FINDINGS THAT SHAPE THIS HARNESS (see the log's EXP-023 D1/D2):
  D1. `backtest/engine.py` does NOT model news protection at all (its module
      docstring says so explicitly, and `check_news_protection` is never
      imported there). Every prior baseline in experiments_log.md is therefore
      a "mode C" run -- the live rule has never been backtested.
  D2. There is no historical high-impact-news calendar anywhere in this
      project (`backtest/news_stub.NoHistoricalNewsDataProvider` returns `[]`
      always; `MQL5CalendarProvider` only exports the forward-looking live
      calendar). The trigger TIME must therefore be proxied, and the two
      proxies are pre-registered, not chosen after seeing results:
        P1 "always"  -- every bar is trigger-eligible => protection fires at
                        the first touch of +0.5R. Exactly the behavior when
                        the calendar is unavailable (news_protection.py's
                        documented fail-safe TRIGGERS protection), and the
                        maximum-exposure bound / largest legitimate sample.
        P2 "newshours" -- Mon-Fri server hours {14,15,16,20,21} only; a coarse
                        stand-in for when high-impact USD/FOMC releases land.
                        ROBUSTNESS ONLY, never used for selection.

WHAT THIS FILE DOES *NOT* DO: it does not modify anything under `src/` or
`config/`. `run_backtest_news()` below is a VERBATIM copy of
`backtest.engine.run_backtest`'s bar loop with the news mechanism inserted
(the same "copy the loop, prove it byte-identical when the new mechanism is
disabled" pattern EXP-021 used for the weekend-close hypothesis).
`--mode fidelity` proves both that copy AND EXP-022's fast-path shim.

PER-BAR ORDERING CONVENTION (pre-registered methodology decision): within a
bar the priority is
    SL/TP `check_exit` (unchanged engine convention, SL wins a double-touch)
      > news-protection intrabar trigger
      > Watchman CLOSE at the bar's close.
The live loop polls every ~5s, so a price level touched intrabar is acted on
BEFORE that bar closes, which is the earliest moment Watchman's closed-bar
structure/time verdict can change. Trigger price = the bar's OPEN if the
position is already >= 0.5R at the open, else the exact +0.5R level. A
lock_sl trigger only moves `current_sl`; per the engine's no-look-ahead rule
the new stop is not checked for an exit until the NEXT bar.

MODES:
  --mode fidelity     news OFF == real engine, trade-for-trade, field-for-field
  --mode conditional  DECIDING metric: per-trade treatment effect on the
                      affected subset with the trade SEQUENCE HELD FIXED
                      (same entries/lots/bars, only the management rule
                      differs) -- no single-position reshuffling confound
  --mode portfolio    full-sequence re-simulation per config (PF/DD/net$);
                      veto-only evidence, never selection evidence
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import (  # noqa: E402
    BacktestConfig,
    ClosedTrade,
    _build_watchman_metadata,
    _classify_watchman_exit_reason,
    _close_trade,
    _fill_entry_price,
    _swing_index_at,
    _watchman_current_atr,
    check_exit,
    run_backtest,
)
from autotrade.backtest.report import generate_report  # noqa: E402
from autotrade.backtest.clock import SimulatedClock  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402
from autotrade.risk.sizing import compute_lot_size  # noqa: E402
from autotrade.shield.checkpoint import Shield  # noqa: E402
from autotrade.watchman.evaluate import evaluate_watchman  # noqa: E402

# EXP-022's validated pieces, reused verbatim rather than re-derived: the
# XAUUSD SymbolSpec, the EXP-018 swap rates, the year windows, the config
# builders and the fast-path memoisation shim (proven exact by its own
# `--mode fidelity`, re-proven here by ours).
from exp022_minlot_harness import (  # noqa: E402
    SWAP_LONG,
    SWAP_SHORT,
    YEARS,
    _SPEC,
    build_cfgs,
    install_fast_path,
    slice_year,
    uninstall_fast_path,
)

# Windows legitimately available to this experiment. y5 (TEST) is deliberately
# absent -- EXP-023 is pre-registered as Train+Val only.
TRAIN_VAL = [y for y in YEARS if not y[0].startswith("y5")]

# P2 proxy (robustness only): Mon-Fri, server hours when high-impact USD
# macro releases and FOMC decisions actually land. NOT a calendar.
NEWSHOURS = frozenset({14, 15, 16, 20, 21})

# Set to a list by `_run_with_origins` to capture each filled position's exact
# entry context (plan/lot/fill price/metadata) so `replay_one` can re-live that
# same position under a different management rule. `None` = not collecting.
_ORIGIN_SINK: list | None = None


@dataclass(frozen=True)
class NewsSim:
    """`mode="off"` reproduces `backtest.engine.run_backtest` exactly."""

    mode: str = "off"  # "off" | "close_all" | "lock_sl"
    lock_frac: float = 0.3
    profit_threshold_r: float = 0.5  # config/base.yaml watchman.news_profit_threshold_r
    hours: frozenset[int] | None = None  # None = P1 (every bar eligible)
    lock_samebar: bool = False
    """Whether the freshly-locked stop is live for the REMAINDER of the bar
    that triggered it (True) or only from the next bar (False).

    This is a genuine, unavoidable intrabar-path convention, so BOTH are run
    and B must win under both to be recommended:
      * False = the engine's documented `MODIFY_SL` rule ("the bar that
        produces a tighter stop can never itself be the bar that gets stopped
        out by it"). That rule exists because Watchman's MODIFY_SL is decided
        at the bar's CLOSE, when the bar is already over. Our news trigger is
        INTRABAR, so the rationale does not transfer, and the effect is a
        PENALTY on B that live would not suffer: the next bar can open far
        below the lock and fill there.
      * True = live-faithful: the live loop places the broker-side stop within
        ~5s of the trigger, so a reversal later in the same hour fills AT the
        locked level. Fills exactly at the level (the price was at >= +0.5R
        when the stop was placed, so the level is inside the remaining range,
        not gapped). Still pessimistic on ordering: if the same bar reaches
        both the lock and the take-profit, the STOP wins, per the engine's
        same-bar-touches-both convention."""

    @property
    def label(self) -> str:
        if self.mode == "off":
            return "C_none"
        if self.mode == "close_all":
            return "A_close_all"
        return f"B_lock_{self.lock_frac:g}" + ("@samebar" if self.lock_samebar else "")


@dataclass
class _Pos:
    """`engine._OpenPosition` plus the three fields the news mechanism needs."""

    plan: object
    lot_size: float
    entry_time: pd.Timestamp
    entry_price: float
    spread_slippage_price_delta: float
    current_sl: float
    metadata: object
    entry_index: int
    news_locked: bool = False
    news_trigger_index: int | None = None
    news_trigger_price: float | None = None
    locked_level: float | None = None


@dataclass
class TradeInfo:
    trade: ClosedTrade
    entry_index: int
    news_triggered: bool
    news_trigger_index: int | None
    news_trigger_price: float | None
    locked_level: float | None
    gapped_through_lock: bool


def _news_trigger_price(pos: _Pos, bar: pd.Series, news: NewsSim) -> float | None:
    """The price at which live's ~5s-cadence Watchman would first see this
    position at >= `profit_threshold_r`, within this bar -- or None if it
    never gets there on this bar (or this bar is not trigger-eligible under
    the active proxy). Mirrors `news_protection.check_news_protection`'s
    profit_r computation exactly: (price - entry)/initial_stop_distance for a
    BUY, entry-relative and using the ORIGINAL stop distance, never a moved
    stop."""
    if news.hours is not None:
        ts = bar["time"]
        if ts.hour not in news.hours or ts.weekday() >= 5:
            return None
    thr = pos.plan.stop_distance * news.profit_threshold_r
    if pos.plan.direction == "BUY":
        level = pos.entry_price + thr
        if bar["open"] >= level:
            return float(bar["open"])
        if bar["high"] >= level:
            return float(level)
        return None
    level = pos.entry_price - thr
    if bar["open"] <= level:
        return float(bar["open"])
    if bar["low"] <= level:
        return float(level)
    return None


def _locked_stop(pos: _Pos, news: NewsSim) -> float:
    """`entry +- lock_frac x R`, in the profit direction."""
    off = pos.plan.stop_distance * news.lock_frac
    return pos.entry_price + off if pos.plan.direction == "BUY" else pos.entry_price - off


def _step_position(
    df: pd.DataFrame,
    i: int,
    pos: _Pos,
    news: NewsSim,
    config: BacktestConfig,
) -> tuple[float, str] | None:
    """One bar of position management. Returns `(exit_price, exit_reason)` if
    the position closes on this bar, else None (still open, `pos` possibly
    mutated by an SL move). This is the SINGLE implementation used by both
    `run_backtest_news` and `replay_one`, so the conditional (sequence-held-
    fixed) replay cannot silently diverge from the full-sequence simulation.

    With `news.mode == "off"` the body is line-for-line
    `backtest.engine.run_backtest`'s open-position block."""
    bar = df.iloc[i]

    exit_result = check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar)
    if exit_result is not None:
        exit_price, exit_reason = exit_result
        if (
            exit_reason == "stop_loss"
            and pos.news_locked
            and pos.locked_level is not None
            and abs(pos.current_sl - pos.locked_level) < 1e-9
        ):
            # A stop-out at the LOCKED level is a protective exit, not a
            # loss -- labelled so the affected-subset accounting can see it.
            exit_reason = "news_lock_stop"
        return float(exit_price), exit_reason

    if news.mode != "off" and not pos.news_locked:
        trig = _news_trigger_price(pos, bar, news)
        if trig is not None:
            pos.news_trigger_index = i
            pos.news_trigger_price = trig
            if news.mode == "close_all":
                return trig, "news_protection"
            level = _locked_stop(pos, news)
            if pos.plan.direction == "BUY":
                pos.current_sl = max(pos.current_sl, level)
            else:
                pos.current_sl = min(pos.current_sl, level)
            pos.locked_level = pos.current_sl
            pos.news_locked = True
            if news.lock_samebar:
                # The stop is live for the rest of THIS bar (see NewsSim.
                # lock_samebar). Fill at the level, never worse: the price was
                # at the trigger (>= +0.5R) when the stop was placed, so the
                # level lies inside the bar's remaining range rather than
                # being gapped through.
                hit = (
                    bar["low"] <= pos.current_sl if pos.plan.direction == "BUY"
                    else bar["high"] >= pos.current_sl
                )
                if hit:
                    return float(pos.current_sl), "news_lock_stop"

    if config.watchman_cfg is not None and pos.metadata is not None:
        decision = evaluate_watchman(
            position_metadata=pos.metadata,
            current_sl=pos.current_sl,
            current_price=float(bar["close"]),
            current_atr=_watchman_current_atr(df, i),
            df=df,
            as_of_index=i,
            now=pd.Timestamp(bar["time"]).to_pydatetime(),
            config=config.watchman_cfg,
        )
        if decision.action == "CLOSE":
            return float(bar["close"]), _classify_watchman_exit_reason(decision.reason)
        if decision.action == "MODIFY_SL":
            # Never loosen a stop the news lock already tightened (the live
            # loop's stop_logic is monotonic in the profit direction too).
            if pos.plan.direction == "BUY":
                pos.current_sl = max(pos.current_sl, decision.new_stop_loss)
            else:
                pos.current_sl = min(pos.current_sl, decision.new_stop_loss)
    return None


def _finish(
    symbol: str, pos: _Pos, df: pd.DataFrame, i: int, exit_price: float, exit_reason: str,
    point_value: float, config: BacktestConfig,
) -> TradeInfo:
    # `_close_trade` only type-ANNOTATES exit_reason (a `Literal` is not
    # enforced at runtime) and `report.generate_report` never reads it, so the
    # two honest new labels ("news_protection", "news_lock_stop") are carried
    # straight through instead of being squeezed into the engine's vocabulary.
    trade = _close_trade(
        symbol, pos, pd.Timestamp(df.iloc[i]["time"]), exit_price, exit_reason,
        point_value, config.cost_model,
    )
    gapped = False
    if exit_reason == "news_lock_stop" and pos.locked_level is not None:
        if pos.plan.direction == "BUY":
            gapped = exit_price < pos.locked_level - 1e-9
        else:
            gapped = exit_price > pos.locked_level + 1e-9
    return TradeInfo(
        trade=trade,
        entry_index=pos.entry_index,
        news_triggered=pos.news_trigger_index is not None,
        news_trigger_index=pos.news_trigger_index,
        news_trigger_price=pos.news_trigger_price,
        locked_level=pos.locked_level,
        gapped_through_lock=gapped,
    )


def run_backtest_news(
    df: pd.DataFrame, symbol: str, symbol_spec, config: BacktestConfig, news: NewsSim,
) -> list[TradeInfo]:
    """VERBATIM copy of `backtest.engine.run_backtest` with the open-position
    block delegated to `_step_position` (which is the engine's own block when
    `news.mode == "off"`) and each trade's news metadata retained. Proven
    byte-identical to the real engine at `news.mode == "off"` by
    `--mode fidelity`."""
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

    pending = None
    position: _Pos | None = None
    out: list[TradeInfo] = []

    for i in range(len(df)):
        bar = df.iloc[i]
        clock.set(pd.Timestamp(bar["time"]).to_pydatetime())

        if pending is not None:
            entry_price, delta = _fill_entry_price(
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
            position = _Pos(
                plan=pending.plan, lot_size=pending.lot_size, entry_time=entry_time,
                entry_price=entry_price, spread_slippage_price_delta=delta,
                current_sl=pending.plan.stop_loss, metadata=metadata, entry_index=i,
            )
            if _ORIGIN_SINK is not None:
                _ORIGIN_SINK.append({
                    "entry_index": i, "plan": pending.plan, "lot_size": pending.lot_size,
                    "entry_time": entry_time, "entry_price": entry_price, "delta": delta,
                    "metadata": metadata,
                })
            pending = None

        if position is not None:
            step = _step_position(df, i, position, news, config)
            if step is not None:
                exit_price, exit_reason = step
                info = _finish(symbol, position, df, i, exit_price, exit_reason, point_value, config)
                out.append(info)
                equity += info.trade.net_pnl
                position = None

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
                    pending = engine_mod._PendingOrder(
                        plan=plan, lot_size=lot, signal_index=i, swing_index=swing_index
                    )

    if position is not None:
        last = len(df) - 1
        out.append(
            _finish(symbol, position, df, last, float(df.iloc[last]["close"]), "end_of_data",
                    point_value, config)
        )
    return out


def replay_one(
    df: pd.DataFrame, symbol: str, symbol_spec, config: BacktestConfig, news: NewsSim, template: TradeInfo,
    origin: dict,
) -> TradeInfo:
    """Re-live ONE already-opened position under a different management rule,
    with everything else held fixed (same entry bar, same fill price, same
    lot, same metadata, same subsequent bars). This is the conditional /
    treatment-effect replay: it deliberately does NOT let an earlier exit
    change which signal is taken next, because in a
    `max_positions_per_symbol: 1` engine that reshuffling dominates the
    portfolio delta and is not caused by the rule under test (EXP-017/020/021)."""
    point_value = symbol_spec.tick_value / symbol_spec.tick_size
    pos = _Pos(
        plan=origin["plan"], lot_size=origin["lot_size"], entry_time=origin["entry_time"],
        entry_price=origin["entry_price"], spread_slippage_price_delta=origin["delta"],
        current_sl=origin["plan"].stop_loss, metadata=origin["metadata"],
        entry_index=template.entry_index,
    )
    for i in range(template.entry_index, len(df)):
        step = _step_position(df, i, pos, news, config)
        if step is not None:
            return _finish(symbol, pos, df, i, step[0], step[1], point_value, config)
    last = len(df) - 1
    return _finish(symbol, pos, df, last, float(df.iloc[last]["close"]), "end_of_data", point_value, config)


def build_bt_config(cfg, *, equity: float, commission: float, swap: bool) -> BacktestConfig:
    rv_cfg, wm_cfg, sh_cfg, order = build_cfgs(cfg)
    return BacktestConfig(
        starting_equity=equity,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=commission, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=SWAP_LONG,
                                       short_per_lot_per_night=SWAP_SHORT) if swap else None,
        ),
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    )


# ------------------------------------------------------------------ metrics
def _pf(rs: list[float]) -> float | None:
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    if not rs:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return round(gp / gl, 4)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def subset_stats(trades: list[ClosedTrade]) -> dict:
    rs = [t.r_multiple for t in trades]
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "sumR": _r(sum(rs)),
        "avgR": _r(sum(rs) / len(rs)),
        "medR": _r(statistics.median(rs)),
        "PF": _pf(rs),
        "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4),
        "worstR": _r(min(rs)),
        "bestR": _r(max(rs)),
        "net$": round(sum(t.net_pnl for t in trades), 2),
    }


def paired_delta(a: list[float], b: list[float]) -> dict:
    """Paired treatment effect B - A over the SAME trades."""
    d = [x - y for x, y in zip(b, a)]
    n = len(d)
    if n < 2:
        return {"n": n}
    mean = sum(d) / n
    sd = statistics.stdev(d)
    se = sd / math.sqrt(n)
    return {
        "n": n,
        "mean_dR": _r(mean),
        "se_dR": _r(se),
        "t": _r(mean / se) if se else None,
        "required_1.6se": _r(1.6 * se),
        "passes_1.6se": bool(se and mean > 1.6 * se),
        "n_better": sum(1 for x in d if x > 1e-9),
        "n_worse": sum(1 for x in d if x < -1e-9),
        "n_same": sum(1 for x in d if abs(x) <= 1e-9),
    }


def cell(trades: list[ClosedTrade], equity: float) -> dict:
    rep = generate_report(trades, equity)
    return {
        "trades": rep.trade_count,
        "PF": _r(rep.profit_factor) if rep.profit_factor not in (None, float("inf")) else rep.profit_factor,
        "PF_ex5": _r(rep.profit_factor_excluding_top_5)
        if rep.profit_factor_excluding_top_5 not in (None, float("inf"))
        else rep.profit_factor_excluding_top_5,
        "net$": round(rep.total_net_pnl, 2),
        "avgR": _r(rep.avg_r_multiple),
        "win_rate": _r(rep.win_rate),
        "maxDD%": _r(rep.max_drawdown_pct),
        "worst$": round(min((t.net_pnl for t in trades), default=0.0), 2),
    }


def exit_mix(infos: list[TradeInfo]) -> dict:
    mix: dict[str, int] = {}
    for i in infos:
        mix[i.trade.exit_reason] = mix.get(i.trade.exit_reason, 0) + 1
    return dict(sorted(mix.items()))


# ------------------------------------------------------------------ modes
def mode_fidelity(df, cfg, args) -> int:
    sub = df.iloc[:4000].reset_index(drop=True)
    bt = build_bt_config(cfg, equity=args.equity, commission=args.commission, swap=not args.no_swap)

    real = run_backtest(sub, "XAUUSD", _SPEC, bt)
    copy_slow = [t.trade for t in run_backtest_news(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"))]
    same_copy = real == copy_slow

    install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
    try:
        copy_fast = [t.trade for t in run_backtest_news(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"))]
    finally:
        uninstall_fast_path()
    same_fast = real == copy_fast

    print(f"FIDELITY bars={len(sub)} real_trades={len(real)} "
          f"copy_off_identical={same_copy} copy_off_fastpath_identical={same_fast}", flush=True)
    if real:
        print("SAMPLE " + json.dumps({
            "first": [str(real[0].entry_time), real[0].exit_reason, round(real[0].net_pnl, 4)],
            "last": [str(real[-1].entry_time), real[-1].exit_reason, round(real[-1].net_pnl, 4)],
        }), flush=True)
    return 0 if (same_copy and same_fast) else 1


def mode_conditional(df, cfg, args) -> int:
    equity = args.equity
    bt = build_bt_config(cfg, equity=equity, commission=args.commission, swap=not args.no_swap)
    fracs = [float(x) for x in args.locks.split(",")]
    proxies = [("P1_always", None)]
    if not args.no_p2:
        proxies.append(("P2_newshours", NEWSHOURS))

    for name, s, e in _windows(args):
        sub = slice_year(df, s, e)
        # The fast path stays installed for the replays too -- it is exact
        # (EXP-022's own fidelity mode, re-proven by ours), and the replays
        # re-evaluate Watchman bar by bar, which is where the memoised ATR
        # matters most.
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            base_infos, base_origins = _run_with_origins(sub, bt, NewsSim(mode="off"))
            _conditional_window(sub, bt, base_infos, base_origins, proxies, fracs, name)
        finally:
            uninstall_fast_path()
    return 0


def _conditional_window(sub, bt, base_infos, base_origins, proxies, fracs, name) -> None:
    # C self-check: replaying mode C trade-by-trade must reproduce the
    # full-sequence mode C result exactly, or the replay is not a faithful
    # counterfactual and nothing below can be trusted. Proxy-independent, so
    # it is done once per window rather than once per proxy.
    selfcheck = [
        replay_one(sub, "XAUUSD", _SPEC, bt, NewsSim(mode="off"), t, o).trade
        for t, o in zip(base_infos, base_origins)
    ]
    assert selfcheck == [t.trade for t in base_infos], "conditional replay diverged from mode C"

    for pname, hours in proxies:
        a_news = NewsSim(mode="close_all", hours=hours)
        a = [replay_one(sub, "XAUUSD", _SPEC, bt, a_news, t, o)
             for t, o in zip(base_infos, base_origins)]
        affected = [k for k, x in enumerate(a) if x.news_triggered]

        row = {
            "window": name, "proxy": pname,
            "trades_total": len(base_infos),
            "affected_n": len(affected),
            "affected_pct": round(100.0 * len(affected) / len(base_infos), 2) if base_infos else 0.0,
            "C_on_affected": subset_stats([base_infos[k].trade for k in affected]),
            "A_on_affected": subset_stats([a[k].trade for k in affected]),
            "A_exit_mix": exit_mix([a[k] for k in affected]),
            "C_exit_mix_on_affected": exit_mix([base_infos[k] for k in affected]),
            "B": {},
        }
        ar = [a[k].trade.r_multiple for k in affected]
        cr = [base_infos[k].trade.r_multiple for k in affected]
        row["dR_A_minus_C"] = paired_delta(cr, ar)

        for f, samebar in [(f, sb) for f in fracs for sb in (False, True)]:
            bnews = NewsSim(mode="lock_sl", lock_frac=f, hours=hours, lock_samebar=samebar)
            b = [replay_one(sub, "XAUUSD", _SPEC, bt, bnews, base_infos[k], base_origins[k])
                 for k in affected]
            # every affected trade must trigger under B too (same trigger rule)
            assert all(x.news_triggered for x in b), "B did not trigger where A did"
            br = [x.trade.r_multiple for x in b]
            row["B"][f"{f:g}" + ("@samebar" if samebar else "")] = {
                **subset_stats([x.trade for x in b]),
                "dR_vs_A": paired_delta(ar, br),
                "dR_vs_C": paired_delta(cr, br),
                "gapped_through_lock": sum(1 for x in b if x.gapped_through_lock),
                "exit_mix": exit_mix(b),
                "worstR": _r(min((x.trade.r_multiple for x in b), default=0.0)),
            }
        print("COND " + json.dumps(row), flush=True)


def _run_with_origins(sub, bt, news):
    """`run_backtest_news` + the per-trade fill context `replay_one` needs
    (the `OrderPlan`/`PositionMetadata`/fill price are not recoverable from a
    `ClosedTrade` alone). Collected by the run itself via `_ORIGIN_SINK`, then
    asserted 1:1 against the trade list on entry_index/entry_price/lot."""
    global _ORIGIN_SINK
    _ORIGIN_SINK = []
    infos = run_backtest_news(sub, "XAUUSD", _SPEC, bt, news)
    origins = list(_ORIGIN_SINK)
    _ORIGIN_SINK = None
    assert len(origins) == len(infos), f"origin/trade mismatch {len(origins)} vs {len(infos)}"
    for o, t in zip(origins, infos):
        assert o["entry_index"] == t.entry_index
        assert abs(o["entry_price"] - t.trade.entry_price) < 1e-12
        assert abs(o["lot_size"] - t.trade.lot_size) < 1e-12
    return infos, origins


def mode_portfolio(df, cfg, args) -> int:
    equity = args.equity
    bt = build_bt_config(cfg, equity=equity, commission=args.commission, swap=not args.no_swap)
    fracs = [float(x) for x in args.locks.split(",")]
    proxies = [("P1_always", None)]
    if not args.no_p2:
        proxies.append(("P2_newshours", NEWSHOURS))

    for name, s, e in _windows(args):
        sub = slice_year(df, s, e)
        install_fast_path(sub, cfg["global"]["swing_pivot_bars"])
        try:
            for pname, hours in proxies:
                sims = [NewsSim(mode="close_all", hours=hours)]
                sims += [NewsSim(mode="lock_sl", lock_frac=f, hours=hours, lock_samebar=sb)
                         for f in fracs for sb in (False, True)]
                if pname == "P1_always":
                    sims.append(NewsSim(mode="off"))  # reference bound, proxy-independent
                for sim in sims:
                    infos = run_backtest_news(sub, "XAUUSD", _SPEC, bt, sim)
                    trades = [i.trade for i in infos]
                    print("PORT " + json.dumps({
                        "window": name, "proxy": pname, "config": sim.label,
                        **cell(trades, equity),
                        "news_fired": sum(1 for i in infos if i.news_triggered),
                        "exit_mix": exit_mix(infos),
                    }), flush=True)
        finally:
            uninstall_fast_path()
    return 0


def _windows(args):
    if args.window in (None, "all"):
        return TRAIN_VAL
    return [y for y in TRAIN_VAL if y[0] == args.window]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True, choices=["fidelity", "conditional", "portfolio"])
    p.add_argument("--equity", type=float, default=3000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--window", default=None, help="y1_2021-22 | y2_2022-23 | y3_2023-24 | y4_VAL_2024-25 | all")
    p.add_argument("--locks", default="0.0,0.2,0.3,0.5")
    p.add_argument("--no-p2", action="store_true", help="skip the P2 news-hours robustness proxy")
    p.add_argument("--no-swap", action="store_true")
    args = p.parse_args()

    if args.window is not None and args.window.startswith("y5"):
        print("REFUSED: EXP-023 is pre-registered Train+Val only; the Test year stays untouched.", flush=True)
        return 2

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    if args.mode == "fidelity":
        return mode_fidelity(df, cfg, args)
    if args.mode == "conditional":
        return mode_conditional(df, cfg, args)
    return mode_portfolio(df, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
