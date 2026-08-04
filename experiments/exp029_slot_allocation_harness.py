#!/usr/bin/env python3
"""EXP-029 harness -- SLOT ALLOCATION: (A) how much Council signal supply the
single position slot throws away and WHAT KIND OF HOLD throws it away, and
(B) the one existing knob, `shield.duplicate_signal_cooldown_hours`.

Family: "shield-slot-allocation" (NEW). Menu item #3 of
`experiments/entry_diagnostic_2026-08-04.md` §7.

PART A is a MEASUREMENT (no candidate, nothing selected). PART B is a small
pre-registered sweep over ONE `[adjustable]` config key. PART C (architectural
options) is scoping prose in the log and is NOT simulated here -- deliberately:
`max_positions_per_symbol` is gated by the spec's own "raise to 2 only after 3
months live", so this file must not sweep it, and does not.

SCOPE: Train (y1/y2/y3) + Val (y4) ONLY. **The Test year 2025-07-22 ->
2026-07-21 is NOT touched under any outcome** -- `load_window` (inherited from
the EXP-027 harness) refuses `y5*` unconditionally and this file has no
`--allow-test` escape hatch, deliberately.

------------------------------------------------------------------ PART A
Definitions, fixed here BEFORE any number exists:
 * **signal bar**  -- a bar at which the REAL `_council_signal_fn` (real
   Council + real Risk Voice, news mechanisms OFF = the C0 convention) returns
   an `OrderPlan`, evaluated UNCONDITIONALLY at every bar of the window with a
   `SimulatedClock` ticked to that bar's OPEN time, exactly as the engine ticks
   it. This is the SUPPLY.
 * **signal episode** -- a maximal run of CONSECUTIVE signal bars with the same
   direction. A gap bar or a direction change starts a new episode. (The entry
   diagnostic §3.4's "distinct signal episodes" is the same construction; its
   counts, 560/530/538/543, are the number this mode must land near.)
 * **admitted episode** -- some bar of the episode is the SIGNAL bar of an
   actual C0 trade (i.e. entry index == that bar + 1). Otherwise **missed**.
 * **counterfactual quality** -- `backtest.forward_walk.simulate_order_forward`
   on the episode's FIRST bar's plan, started at the next bar, entry = the
   plan's own entry (the signal bar's close), spread = the signal bar's spread,
   `time_stop_bars=48`, Appendix A §5.4 cost convention (spread + commission,
   NO slippage, no Watchman, no Shield, no news) -- the SAME machinery and
   convention the entry diagnostic used, so the two are comparable. One
   observation per episode (not per bar), which deliberately avoids the ~5x
   overlap inflation the diagnostic's §1.1 caveat 1 warns about.
 * **blocking attribution** -- for a missed episode, the reason at its FIRST
   bar: `slot_busy` (the engine never called `signal_fn`, i.e. a position was
   open), `shield_cooldown` (called, plan returned, Shield rule 6 blocked),
   `sizing_or_no_next_bar` (called, plan returned, Shield passed, still no
   trade -- the min-lot floor or the last bar). For `slot_busy`, the HOLDER is
   identified: its age in bars at that instant, its total holding length, and
   its eventual exit reason. That is the "which kinds of holds block the most
   supply" question.
Self-check asserted in code: the set of bars the engine did NOT call
`signal_fn` on must equal the union of [entry_index, exit_index) over the C0
trades. If that fails, the occupancy map is wrong and the mode aborts.

------------------------------------------------------------------ PART B
`shield.duplicate_signal_cooldown_hours` (currently **4.0**, `[adjustable]`,
never swept). What it actually gates, read from `shield/checkpoint.py` rule 6
BEFORE the sweep: a new signal is blocked iff (same symbol AND same direction
as the last trade Shield approved AND that was actually filled) AND (the
`swing_index` re-derived at signal time EQUALS the one recorded at that trade)
AND (elapsed < the cooldown). A genuinely new confirmed swing bypasses it
entirely, regardless of elapsed time. In this single-position engine it is the
ONLY one of Shield's six rules with any effect (`open_positions` is always
`[]`, and `min_rr` always passes at `tp_r_multiple` 2.0) -- so `cooldown = 0`
must be behaviourally identical to `shield_cfg=None`, which is fidelity gate
G3 below.
Pre-registered grid: **{0, 2, 4 (baseline), 8}**. Nothing else is swept.

MODES (one window per invocation -- every call stays well under the 600s cap):
  --mode fidelity --scope anchor --window W   C0 anchors (cooldown = config 4.0)
  --mode fidelity --scope shield0 --window W  cooldown=0 == shield_cfg=None, trade-for-trade
  --mode census --window W                    PART A
  --mode sweep --window W                     PART B ({0,2,4,8})
  --mode pool --infile                        pool census rows into Train / Val
  --mode sweeppool --infile                   pool sweep rows into Train / Val
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.clock import SimulatedClock  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from autotrade.backtest.forward_walk import simulate_order_forward  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.shield.checkpoint import Shield, ShieldConfig  # noqa: E402

import exp022_minlot_harness as e22  # noqa: E402
import exp027_entry_blackout_harness as e27  # noqa: E402

SYMBOL = e27.SYMBOL
EQUITY = e27.EQUITY
COMMISSION = e27.COMMISSION
TRAIN = e27.TRAIN
VAL = e27.VAL
TIME_STOP_BARS = 48  # the entry diagnostic's own forward-walk cutoff
COOLDOWNS = (0.0, 2.0, 4.0, 8.0)  # pre-registered grid; 4.0 = live baseline

# Anchors CITED BEFORE ANY RUN (this log's universal C0 baseline; unchanged by
# the 2026-08-04 engine defect fixes, which re-verified it bit-for-bit).
ANCHORS_C0 = {
    "y1_2021-22": (266, 1.0159, 14.4879),
    "y2_2022-23": (254, 0.9949, 26.12),
    "y3_2023-24": (233, 1.2020, 12.2659),
    "y4_VAL_2024-25": (254, 1.0961, 9.9895),
}
ANCHOR_C0_Y4_NET = 352.60
# The entry diagnostic §3.4's episode counts, cited before the census runs so
# the census is checked against a published number rather than self-validating.
DIAG_EPISODES = {"y1_2021-22": 560, "y2_2022-23": 530, "y3_2023-24": 538, "y4_VAL_2024-25": 543}

load_window = e27.load_window
group_stats = e27.group_stats
portfolio_metrics = e27.portfolio_metrics
_pf = e27._pf
_mix = e27._mix


# ------------------------------------------------------------------ setup
def build_cfg(cfg, *, cooldown: float | None, signal_fn=None) -> BacktestConfig:
    """C0 context (news OFF both sides). `cooldown=None` means shield_cfg=None
    -- the pre-cooldown-wiring behaviour, used by fidelity gate G3 only."""
    rv_cfg, wm_cfg, sh_cfg, order = e22.build_cfgs(cfg)
    if cooldown is None:
        sh_cfg = None
    else:
        sh_cfg = dataclasses.replace(sh_cfg, duplicate_signal_cooldown_hours=cooldown)
    kw = {}
    if signal_fn is not None:
        kw["signal_fn"] = signal_fn
    return BacktestConfig(
        starting_equity=EQUITY,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=COMMISSION, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=e22.SWAP_LONG,
                                       short_per_lot_per_night=e22.SWAP_SHORT),
        ),
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **kw,
        **order,
    )


class CallProbe:
    """Pass-through wrapper over the engine's own `signal_fn` that records WHICH
    bars the engine actually evaluated (the free-bar set) and which of those
    produced a plan. Behaviour-neutral by construction (it returns exactly what
    `_council_signal_fn` returns); proven by the C0 anchor gate, which is
    produced through it."""

    def __init__(self):
        self.called: set[int] = set()
        self.planned: dict[int, str] = {}

    def __call__(self, df, as_of_index, **kw):
        plan = engine_mod._council_signal_fn(df, as_of_index, **kw)
        self.called.add(as_of_index)
        if plan is not None:
            self.planned[as_of_index] = plan.direction
        return plan


def make_recording_shield(log: list):
    class _RecordingShield(Shield):
        def check(self, **kw):
            d = super().check(**kw)
            log.append((kw["clock"].now(), d.cooldown_blocked, d.blocked))
            return d
    return _RecordingShield


# ------------------------------------------------------------------ modes
def mode_fidelity(args) -> int:
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    out = {"scope": args.scope, "window": args.window}
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        if args.scope == "anchor":
            probe = CallProbe()
            m = portfolio_metrics(run_backtest(df, SYMBOL, e22._SPEC,
                                               build_cfg(cfg, cooldown=4.0, signal_fn=probe)))
            exp_t, exp_pf, exp_dd = ANCHORS_C0[args.window]
            ok = (m["records"] == exp_t and abs(m["PF"] - exp_pf) <= 0.0006
                  and abs(m["maxDD%"] - exp_dd) <= 0.006)
            if args.window == "y4_VAL_2024-25":
                ok = ok and abs(m["net$"] - ANCHOR_C0_Y4_NET) <= 0.01
            out["anchor"] = {"measured": m, "recorded": {"trades": exp_t, "PF": exp_pf, "maxDD%": exp_dd},
                             "match": ok, "bars_signal_fn_called": len(probe.called),
                             "bars_with_plan_at_free_bars": len(probe.planned)}
        else:  # shield0
            a = run_backtest(df, SYMBOL, e22._SPEC, build_cfg(cfg, cooldown=0.0))
            b = run_backtest(df, SYMBOL, e22._SPEC, build_cfg(cfg, cooldown=None))
            ok = [asdict(t) for t in a] == [asdict(t) for t in b]
            out["shield0_equals_shield_none"] = {"n_cooldown0": len(a), "n_shield_none": len(b),
                                                 "identical": ok}
    finally:
        e22.uninstall_fast_path()
    print("FIDELITY " + json.dumps(out, default=str))
    return 0 if ok else 1


def mode_census(args) -> int:
    """PART A -- the missed-signal population and the occupancy attribution."""
    cfg = load_yaml_config("base")
    pivot = cfg["global"]["swing_pivot_bars"]
    df = load_window(args.window)
    n = len(df)
    e22.install_fast_path(df, pivot)
    try:
        # ---- 1. the ACTUAL C0 run, instrumented
        probe = CallProbe()
        shield_log: list = []
        real_shield = engine_mod.Shield
        engine_mod.Shield = make_recording_shield(shield_log)
        try:
            bt = build_cfg(cfg, cooldown=4.0, signal_fn=probe)
            trades = run_backtest(df, SYMBOL, e22._SPEC, bt)
        finally:
            engine_mod.Shield = real_shield

        idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
        holder_of: dict[int, int] = {}
        holders = []
        for k, t in enumerate(trades):
            e = idx[pd.Timestamp(t.entry_time)]
            x = idx[pd.Timestamp(t.exit_time)]
            # A trade that exits via check_exit / Watchman / news protection
            # frees the slot BEFORE that bar's signal step, so bar x is free.
            # `end_of_data` is closed AFTER the loop, so bar x stays busy.
            last_busy = x if t.exit_reason == "end_of_data" else x - 1
            holders.append({"entry_i": e, "exit_i": x, "len": x - e, "dir": t.direction,
                            "exit": t.exit_reason, "r": round(t.r_multiple, 4)})
            for b in range(e, last_busy + 1):
                holder_of[b] = k
        busy = set(holder_of)
        free = set(range(n)) - busy
        assert free == probe.called, (
            f"occupancy map mismatch: |free|={len(free)} |called|={len(probe.called)} "
            f"|diff|={len(free ^ probe.called)}"
        )
        cooldown_bars = {idx[pd.Timestamp(ts)] for ts, cd, _ in shield_log if cd}
        trade_signal_bars = {idx[pd.Timestamp(t.entry_time)] - 1 for t in trades}

        # ---- 2. the UNCONDITIONAL supply, every bar
        clock = SimulatedClock(pd.Timestamp(df["time"].iloc[0]).to_pydatetime())
        plans = {}
        for i in range(n):
            clock.set(pd.Timestamp(df["time"].iloc[i]).to_pydatetime())
            plan = engine_mod._council_signal_fn(
                df, i, symbol=SYMBOL, symbol_spec=e22._SPEC,
                sl_buffer_atr=bt.sl_buffer_atr, sl_min_atr=bt.sl_min_atr,
                sl_max_atr=bt.sl_max_atr, tp_r_multiple=bt.tp_r_multiple,
                pivot_bars=pivot, bull_threshold=bt.bull_threshold,
                bear_threshold=bt.bear_threshold, conflict_threshold=bt.conflict_threshold,
                risk_voice_cfg=bt.risk_voice_cfg, model_risk_voice_news=False,
                news_calendar=None, clock=clock,
            )
            if plan is not None:
                plans[i] = plan
        # sanity: at a bar the engine DID evaluate, the two must agree
        disagree = sum(1 for i in probe.called if (i in plans) != (i in probe.planned))
        assert disagree == 0, f"unconditional census disagrees with the engine on {disagree} free bars"

        # ---- 3. episodes
        episodes = []
        cur = None
        for i in sorted(plans):
            d = plans[i].direction
            if cur is not None and i == cur["end"] + 1 and d == cur["dir"]:
                cur["end"] = i
                continue
            if cur is not None:
                episodes.append(cur)
            cur = {"start": i, "end": i, "dir": d}
        if cur is not None:
            episodes.append(cur)

        rows = []
        for ep in episodes:
            s, e = ep["start"], ep["end"]
            bars = list(range(s, e + 1))
            admitted = any(b in trade_signal_bars for b in bars)
            first = s
            if first in trade_signal_bars:
                cause = "traded_at_first_bar"
            elif first not in probe.called:
                cause = "slot_busy"
            elif first in cooldown_bars:
                cause = "shield_cooldown"
            else:
                cause = "sizing_or_no_next_bar"
            plan = plans[s]
            fw = simulate_order_forward(
                df, s + 1, plan, entry_price=plan.entry,
                spread_points_at_entry=float(df["spread"].iloc[s]),
                symbol_spec=e22._SPEC, cost_model=bt.cost_model, time_stop_bars=TIME_STOP_BARS,
            )
            h = holders[holder_of[first]] if first in holder_of else None
            rows.append({
                "window": args.window, "start_i": s, "len": len(bars), "dir": ep["dir"],
                "time": str(df["time"].iloc[s]),
                "admitted": admitted, "cause_at_first_bar": cause,
                "fw_outcome": fw.outcome, "fw_net_r": None if fw.net_r is None else round(fw.net_r, 6),
                "blocked_bars": sum(1 for b in bars if b not in probe.called),
                "cooldown_bars": sum(1 for b in bars if b in cooldown_bars),
                "holder_age_at_block": None if h is None else first - h["entry_i"],
                "holder_len": None if h is None else h["len"],
                "holder_exit": None if h is None else h["exit"],
                "holder_r": None if h is None else h["r"],
                # DESCRIPTIVE field added after the pre-registration (flagged as
                # such in RESULTS): is the blocked episode the SAME direction as
                # the position holding the slot? -- i.e. would admitting it have
                # been a pyramid into a running move, or a genuine reversal?
                "holder_same_dir": None if h is None else (h["dir"] == ep["dir"]),
            })
    finally:
        e22.uninstall_fast_path()

    adm = [r for r in rows if r["admitted"]]
    mis = [r for r in rows if not r["admitted"]]
    res = [r for r in rows if r["fw_net_r"] is not None]
    # supply blocked, attributed to the holding trade's eventual exit reason
    by_exit_bars: dict[str, int] = {}
    by_exit_eps: dict[str, int] = {}
    by_exit_hold: dict[str, list] = {}
    for r in mis:
        if r["holder_exit"] is None:
            continue
        by_exit_bars[r["holder_exit"]] = by_exit_bars.get(r["holder_exit"], 0) + r["blocked_bars"]
        by_exit_eps[r["holder_exit"]] = by_exit_eps.get(r["holder_exit"], 0) + 1
        by_exit_hold.setdefault(r["holder_exit"], []).append(r["holder_len"])
    ages = sorted(r["holder_age_at_block"] for r in mis if r["holder_age_at_block"] is not None)
    lens = sorted(r["holder_len"] for r in mis if r["holder_len"] is not None)

    def _q(v, q):
        return v[min(len(v) - 1, int(q * len(v)))] if v else None

    out = {
        "window": args.window, "bars": n,
        "signal_bars": len(plans), "episodes": len(rows),
        "episodes_recorded_by_diagnostic": DIAG_EPISODES[args.window],
        "C0_trades": len(trades),
        "admitted_episodes": len(adm), "missed_episodes": len(mis),
        "admit_rate_pct": round(100.0 * len(adm) / len(rows), 2) if rows else None,
        "cause_mix_missed": _mix([r["cause_at_first_bar"] for r in mis]),
        "quality_forward_walk": {
            "admitted": group_stats([r["fw_net_r"] for r in adm if r["fw_net_r"] is not None]),
            "missed": group_stats([r["fw_net_r"] for r in mis if r["fw_net_r"] is not None]),
            "unresolved_no_exit": len(rows) - len(res),
        },
        "quality_by_cause": {
            c: group_stats([r["fw_net_r"] for r in mis
                            if r["cause_at_first_bar"] == c and r["fw_net_r"] is not None])
            for c in sorted({r["cause_at_first_bar"] for r in mis})
        },
        "actual_engine_R_of_C0_trades": group_stats([round(t.r_multiple, 6) for t in trades]),
        "occupancy_attribution": {
            "blocked_bars_by_holder_exit": dict(sorted(by_exit_bars.items(), key=lambda kv: -kv[1])),
            "missed_episodes_by_holder_exit": dict(sorted(by_exit_eps.items(), key=lambda kv: -kv[1])),
            "median_holder_len_by_exit": {k: sorted(v)[len(v) // 2] for k, v in by_exit_hold.items()},
            "holder_age_at_block_bars": {"n": len(ages), "min": _q(ages, 0.0), "p25": _q(ages, 0.25),
                                         "median": _q(ages, 0.5), "p75": _q(ages, 0.75),
                                         "p90": _q(ages, 0.90), "max": ages[-1] if ages else None,
                                         "mean": round(sum(ages) / len(ages), 2) if ages else None},
            "holder_total_len_bars": {"median": _q(lens, 0.5), "p90": _q(lens, 0.90),
                                      "mean": round(sum(lens) / len(lens), 2) if lens else None},
            "trade_len_distribution_all_C0": {
                "median": sorted(h["len"] for h in holders)[len(holders) // 2],
                "mean": round(sum(h["len"] for h in holders) / len(holders), 2),
                "by_exit_median": {k: sorted(v)[len(v) // 2] for k, v in
                                   {e: [h["len"] for h in holders if h["exit"] == e]
                                    for e in {h["exit"] for h in holders}}.items()},
                "by_exit_total_bars": {e: sum(h["len"] for h in holders if h["exit"] == e)
                                       for e in {h["exit"] for h in holders}},
            },
        },
        "busy_bars": len(busy), "free_bars": len(free),
        "busy_pct": round(100.0 * len(busy) / n, 2),
        "cooldown_blocked_bars": len(cooldown_bars),
    }
    print("CENSUS " + json.dumps(out, default=str))
    for r in rows:
        print("EP " + json.dumps(r, default=str))
    return 0


def mode_sweep(args) -> int:
    """PART B -- the pre-registered {0,2,4,8} cooldown grid, C0 context."""
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        for cd in COOLDOWNS:
            trades = run_backtest(df, SYMBOL, e22._SPEC, build_cfg(cfg, cooldown=cd))
            rs = [round(t.r_multiple, 6) for t in trades]
            row = {"window": args.window, "cooldown_h": cd, **portfolio_metrics(trades),
                   "R": group_stats(rs),
                   "entries": sorted({str(t.entry_time) for t in trades}),
                   "exit_mix": _mix([t.exit_reason for t in trades])}
            print("SWEEP " + json.dumps(row, default=str))
    finally:
        e22.uninstall_fast_path()
    return 0


def mode_pool(args) -> int:
    recs = [json.loads(l[7:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
            if l.startswith("CENSUS ")]
    eps = [json.loads(l[3:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
           if l.startswith("EP ")]

    def _pool(rs, es):
        if not rs:
            return {}
        adm = [e for e in es if e["admitted"] and e["fw_net_r"] is not None]
        mis = [e for e in es if not e["admitted"] and e["fw_net_r"] is not None]
        a = group_stats([e["fw_net_r"] for e in adm])
        m = group_stats([e["fw_net_r"] for e in mis])
        diff = None
        if a.get("n", 0) > 1 and m.get("n", 0) > 1:
            se = math.sqrt(a["se"] ** 2 + m["se"] ** 2)
            d = m["avgR"] - a["avgR"]
            diff = {"missed_minus_admitted": round(d, 4), "se": round(se, 4),
                    "t": round(d / se, 3) if se else None,
                    "ci95": [round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)]}
        bars: dict[str, int] = {}
        epc: dict[str, int] = {}
        for r in rs:
            for k, v in r["occupancy_attribution"]["blocked_bars_by_holder_exit"].items():
                bars[k] = bars.get(k, 0) + v
            for k, v in r["occupancy_attribution"]["missed_episodes_by_holder_exit"].items():
                epc[k] = epc.get(k, 0) + v
        tot = sum(bars.values()) or 1
        return {
            "episodes": sum(r["episodes"] for r in rs),
            "admitted": sum(r["admitted_episodes"] for r in rs),
            "missed": sum(r["missed_episodes"] for r in rs),
            "admit_rate_pct": round(100.0 * sum(r["admitted_episodes"] for r in rs)
                                    / sum(r["episodes"] for r in rs), 2),
            "cause_mix_missed": {k: sum(r["cause_mix_missed"].get(k, 0) for r in rs)
                                 for k in {k for r in rs for k in r["cause_mix_missed"]}},
            "fw_admitted": a, "fw_missed": m, "missed_minus_admitted": diff,
            "blocked_bars_by_holder_exit": dict(sorted(bars.items(), key=lambda kv: -kv[1])),
            "blocked_bars_share_pct": {k: round(100.0 * v / tot, 1)
                                       for k, v in sorted(bars.items(), key=lambda kv: -kv[1])},
            "missed_episodes_by_holder_exit": dict(sorted(epc.items(), key=lambda kv: -kv[1])),
        }

    print("POOL " + json.dumps({
        "TRAIN": _pool([r for r in recs if r["window"] in TRAIN],
                       [e for e in eps if e["window"] in TRAIN]),
        "VAL": _pool([r for r in recs if r["window"] in VAL],
                     [e for e in eps if e["window"] in VAL]),
        "per_window": {r["window"]: {k: r[k] for k in
                                     ("bars", "signal_bars", "episodes",
                                      "episodes_recorded_by_diagnostic", "C0_trades",
                                      "admitted_episodes", "missed_episodes", "admit_rate_pct",
                                      "busy_pct", "cooldown_blocked_bars", "cause_mix_missed",
                                      "quality_forward_walk", "occupancy_attribution")}
                       for r in recs},
    }, default=str))
    return 0


def mode_sweeppool(args) -> int:
    rows = [json.loads(l[6:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
            if l.startswith("SWEEP ")]
    base = {r["window"]: r for r in rows if r["cooldown_h"] == 4.0}
    out = {}
    for split, wins in (("TRAIN", TRAIN), ("VAL", VAL)):
        cells = {}
        for cd in COOLDOWNS:
            rs = [r for r in rows if r["cooldown_h"] == cd and r["window"] in wins]
            if not rs:
                continue
            n = sum(r["R"]["n"] for r in rs)
            s = sum(r["R"]["sum"] for r in rs)
            s2 = sum(r["R"]["sum2"] for r in rs)
            mean = s / n
            se = math.sqrt(max((s2 - n * mean * mean) / (n - 1), 0.0) / n)
            b = [base[r["window"]] for r in rs]
            nb = sum(x["R"]["n"] for x in b)
            sb = sum(x["R"]["sum"] for x in b)
            s2b = sum(x["R"]["sum2"] for x in b)
            mb = sb / nb
            seb = math.sqrt(max((s2b - nb * mb * mb) / (nb - 1), 0.0) / nb)
            d = mean - mb
            dse = math.sqrt(se ** 2 + seb ** 2)
            shared = sum(len(set(r["entries"]) & set(bb["entries"])) for r, bb in zip(rs, b))
            cells[str(cd)] = {
                "trades": sum(r["records"] for r in rs),
                "net$": round(sum(r["net$"] for r in rs), 2),
                "avgR": round(mean, 4), "se": round(se, 4),
                "per_window_PF": {r["window"]: r["PF"] for r in rs},
                "per_window_net$": {r["window"]: r["net$"] for r in rs},
                "per_window_trades": {r["window"]: r["records"] for r in rs},
                "per_window_pf_ex5": {r["window"]: r["pf_ex5"] for r in rs},
                "vs_baseline_avgR": {"delta": round(d, 4), "se_unpaired": round(dse, 4),
                                     "t": round(d / dse, 3) if dse else None},
                "entries_shared_with_baseline": shared,
                "entries_shared_pct": round(100.0 * shared / sum(r["records"] for r in rs), 1),
            }
        out[split] = cells
    print("SWEEPPOOL " + json.dumps(out, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True,
                   choices=["fidelity", "census", "sweep", "pool", "sweeppool"])
    p.add_argument("--scope", default="anchor", choices=["anchor", "shield0"])
    p.add_argument("--window", default="y1_2021-22", choices=sorted(e27.YEARS))
    p.add_argument("--infile", default="experiments/exp029_census_out.txt")
    args = p.parse_args()
    return {"fidelity": mode_fidelity, "census": mode_census, "sweep": mode_sweep,
            "pool": mode_pool, "sweeppool": mode_sweeppool}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
