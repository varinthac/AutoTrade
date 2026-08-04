#!/usr/bin/env python3
"""EXP-028 harness -- ABLATION: `trend_alignment` PARTIAL tier (15 pts) as a
direct ENTRY VETO, measured through the production engine.

Family: "council-entry-tier" (NEW). Menu item #4 of
`experiments/entry_diagnostic_2026-08-04.md` §7.

WHAT IS ABLATED, AND AT WHICH SEAM (declared here, in code, BEFORE any result
exists). The candidate has NO config knob: `trend_alignment`'s 30/15/0 tiering
is hard-coded in `council/scoring.py`. The veto is therefore installed as a
SIGNAL-LEVEL seam -- a wrapper around `backtest.engine._council_signal_fn`
passed via `BacktestConfig.signal_fn` (a public, injectable field; nothing
under `src/` or `config/` is modified):

    plan = _council_signal_fn(df, i, ...)          # real Council + real Risk Voice
    if plan is not None and veto:
        tier = score_<leading>_voice(df, i, ...).trend_alignment
        if tier == 15:                              # PARTIAL alignment
            return None                             # <-- the ablation

`<leading>` is the voice whose direction the Decision Matrix actually admitted
(`score_bull_voice` for a BUY plan, `score_bear_voice` for a SELL plan), i.e.
exactly the population the diagnostic §6 measured ("winning voice's own
component only, clean signals").

Consequences of choosing THIS seam, stated up front because they define what
the experiment can and cannot show:
 * The veto happens BEFORE Shield's cooldown check, BEFORE CFO sizing and
   BEFORE `_PendingOrder` creation -- so a vetoed signal never occupies the
   single position slot, and the engine's own bar loop is free to take a LATER
   signal that the baseline never had room for. The sequence is therefore
   replayed HONESTLY (with slot refill), not by deleting rows from a finished
   trade list. Slot-refill accounting is measurement (c) below.
 * The veto is applied on the SIGNAL bar `i` (the bar the plan is built on),
   which is the same instant the Council/Risk-Voice decision is taken. No clock
   convention is involved (unlike EXP-027's blackout): `trend_alignment` is a
   pure function of closed bars up to `i`.
 * A partial-tier bar is removed REGARDLESS of its total score. This is the
   whole point of the design, and the reason EXP-016 does not close this
   question: EXP-016 changed the partial tier's WEIGHT (15 -> 0 / 7), which only
   removes partial bars that fall under 70 as a result; a partial bar scoring 85
   still fired there. This veto removes all of them.

CELLS (a cell is a set of flags; there is exactly ONE candidate, `V`):
    C0   veto OFF, blackout OFF, protection OFF  = this log's universal baseline
    V    veto ON,  blackout OFF, protection OFF  = THE CANDIDATE
    P    veto OFF, blackout OFF, protection ON   = post-defect-fix anchor (gate only)
    EP   veto OFF, blackout ON,  protection ON   = full news parity (informational)
    EPV  veto ON,  blackout ON,  protection ON   = full news parity + veto (informational)
C0 and V are the primary pair, run on Train (y1/y2/y3) + Val (y4). P/EP/EPV are
run on Val ONLY and are INFORMATIONAL (one overlay row, to sanity-check that the
conclusion survives full news parity) -- they are not candidates and nothing is
selected among them.

SCOPE: Train + Val ONLY. **The Test year 2025-07-22 -> 2026-07-21 is NOT touched
under any outcome** -- `load_window` (inherited from the EXP-027 harness) refuses
`y5*` unconditionally and this file has no `--allow-test` escape hatch,
deliberately. This family has no authorised Test budget, and a RECOMMEND verdict
would end at a recommendation (adoption needs a `src/` change + a spec
conversation, per the diagnostic's own item #4).

MODES (one window per invocation -- every call stays well under the 600s cap):
  --mode fidelity --scope fastpath   fast-path shim identity, cells C0 and V
  --mode fidelity --scope seam       wrapper-with-veto-OFF == stock engine, trade-for-trade
  --mode fidelity --scope anchor     C0 anchor for --window (P anchor too, with --cells C0,P)
  --mode grid --window W             portfolio rows for --cells (default C0,V)
  --mode removed --window W          the removed population on the C0 sequence (per-year table)
  --mode pool --infile               pool `removed` rows into Train / Val statistics
  --mode refill --gridfile           slot-refill accounting (C0 vs V entry sets)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade.backtest.engine as engine_mod  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from autotrade.backtest.historical_news_calendar import HistoricalNewsCalendarProvider  # noqa: E402
from autotrade.council.scoring import score_bear_voice, score_bull_voice  # noqa: E402
from autotrade.common.config import load_yaml_config  # noqa: E402
from autotrade.watchman.news_protection import NewsProtectionConfig  # noqa: E402

import exp022_minlot_harness as e22  # noqa: E402
import exp026_minlot_skip_harness as e26  # noqa: E402
import exp027_entry_blackout_harness as e27  # noqa: E402

SYMBOL = e27.SYMBOL
CALENDAR = e27.CALENDAR
EQUITY = e27.EQUITY
COMMISSION = e27.COMMISSION
TRAIN = e27.TRAIN
VAL = e27.VAL
PARTIAL_TIER = 15  # `council/scoring.py`: EMA20>EMA50 but EMA200 not yet crossed

# (blackout, protection, veto)
CELLS = {
    "C0": (False, False, False),
    "V": (False, False, True),
    "P": (False, True, False),
    "EP": (True, True, False),
    "EPV": (True, True, True),
}

# Anchors CITED BEFORE ANY RUN (an anchor is only an anchor if it is named
# first). C0 = this log's universal baseline (EXP-022 cap-1.5, re-confirmed by
# EXP-023/024/025/026 §4 and EXP-027 G2; unchanged by the 2026-08-04 defect
# fixes, which the fix NOTE re-verified bit-for-bit). P = the POST-defect-fix
# protection-only row from the 2026-08-04 fix NOTE (it SUPERSEDES EXP-027's
# pre-fix P column) -- only y4 is used here, and only for the informational
# overlay's gate.
ANCHORS_C0 = {  # trades, PF, maxDD%
    "y1_2021-22": (266, 1.0159, 14.4879),
    "y2_2022-23": (254, 0.9949, 26.12),
    "y3_2023-24": (233, 1.2020, 12.2659),
    "y4_VAL_2024-25": (254, 1.0961, 9.9895),
}
ANCHOR_C0_Y4_NET = 352.60
ANCHORS_P = {"y4_VAL_2024-25": (369, 1.0830, 329.94)}  # records, PF, net$ (post-fix)

load_window = e27.load_window
group_stats = e27.group_stats
portfolio_metrics = e27.portfolio_metrics
_pf = e27._pf
_mix = e27._mix


# ------------------------------------------------------- the ablation seam
def leading_trend_tier(df, as_of_index, direction, symbol_spec, pivot_bars) -> int:
    """`trend_alignment` of the voice that produced the admitted direction --
    30 (full EMA alignment), 15 (PARTIAL) or 0. Calls the REAL scorer, so the
    fast-path memoisation shim applies exactly as it does inside the engine."""
    score = (score_bull_voice if direction == "BUY" else score_bear_voice)(
        df, as_of_index, symbol_spec, pivot_bars=pivot_bars
    )
    return score.trend_alignment


class VetoSignalFn:
    """`BacktestConfig.signal_fn` wrapper implementing the ablation. With
    `veto=False` it is a strict pass-through (proven trade-for-trade by
    `--mode fidelity --scope seam`, and again by every C0 anchor, which is
    produced through this wrapper)."""

    def __init__(self, veto: bool):
        self.veto = veto
        self.gross_vetoed = 0
        self.vetoed_bars: list[int] = []
        self.tier_hist: dict[int, int] = {}

    def __call__(self, df, as_of_index, **kw):
        plan = engine_mod._council_signal_fn(df, as_of_index, **kw)
        if plan is None:
            return None
        tier = leading_trend_tier(df, as_of_index, plan.direction, kw["symbol_spec"], kw["pivot_bars"])
        self.tier_hist[tier] = self.tier_hist.get(tier, 0) + 1
        if self.veto and tier == PARTIAL_TIER:
            self.gross_vetoed += 1
            self.vetoed_bars.append(as_of_index)
            return None
        return plan


def build_cfg(cfg, cell: str) -> tuple[BacktestConfig, VetoSignalFn]:
    blackout, protection, veto = CELLS[cell]
    rv_cfg, wm_cfg, sh_cfg, order = e22.build_cfgs(cfg)
    np_cfg = calendar = None
    if protection:
        np_cfg = NewsProtectionConfig(
            news_window_minutes=cfg["watchman"]["news_window_minutes"],
            profit_threshold_r=cfg["watchman"]["news_profit_threshold_r"],
            close_mode=cfg["watchman"]["news_close_mode"],
        )
    if protection or blackout:
        calendar = HistoricalNewsCalendarProvider(CALENDAR)
    fn = VetoSignalFn(veto)
    return BacktestConfig(
        starting_equity=EQUITY,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=COMMISSION, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=e22.SWAP_LONG,
                                       short_per_lot_per_night=e22.SWAP_SHORT),
        ),
        signal_fn=fn,
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        model_risk_voice_news=blackout,
        news_protection_cfg=np_cfg, news_calendar=calendar,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    ), fn


# ------------------------------------------------------------------ modes
def mode_fidelity(args) -> int:
    cfg = load_yaml_config("base")
    out = {"scope": args.scope}
    ok = True

    if args.scope == "fastpath":
        df = load_window("y4_VAL_2024-25").iloc[:4000].reset_index(drop=True)
        for cell in ("C0", "V"):
            bt, _ = build_cfg(cfg, cell)
            slow = run_backtest(df, SYMBOL, e22._SPEC, bt)
            e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
            try:
                bt2, _ = build_cfg(cfg, cell)
                fast = run_backtest(df, SYMBOL, e22._SPEC, bt2)
            finally:
                e22.uninstall_fast_path()
            same = [asdict(t) for t in slow] == [asdict(t) for t in fast]
            ok = ok and same
            out[f"fastpath_{cell}"] = {"n": len(slow), "identical": same}
        print("FIDELITY " + json.dumps(out, default=str))
        return 0 if ok else 1

    if args.scope == "seam":
        # The wrapper with veto=False must be a strict no-op vs the engine's own
        # default signal_fn -- trade-for-trade, field-for-field.
        df = load_window("y4_VAL_2024-25").iloc[:4000].reset_index(drop=True)
        e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
        try:
            bt_wrapped, fn = build_cfg(cfg, "C0")
            wrapped = run_backtest(df, SYMBOL, e22._SPEC, bt_wrapped)
            import dataclasses
            bt_stock = dataclasses.replace(bt_wrapped, signal_fn=engine_mod._council_signal_fn)
            stock = run_backtest(df, SYMBOL, e22._SPEC, bt_stock)
        finally:
            e22.uninstall_fast_path()
        same = [asdict(t) for t in wrapped] == [asdict(t) for t in stock]
        ok = same
        out["seam_noop"] = {"n_wrapped": len(wrapped), "n_stock": len(stock), "identical": same,
                            "tier_hist_at_admitted_signals": fn.tier_hist}
        print("FIDELITY " + json.dumps(out, default=str))
        return 0 if ok else 1

    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        for cell in args.cells.split(","):
            bt, _ = build_cfg(cfg, cell)
            m = portfolio_metrics(run_backtest(df, SYMBOL, e22._SPEC, bt))
            if cell == "C0":
                exp_t, exp_pf, exp_dd = ANCHORS_C0[args.window]
                match = (m["records"] == exp_t and abs(m["PF"] - exp_pf) <= 0.0006
                         and abs(m["maxDD%"] - exp_dd) <= 0.006)
                if args.window == "y4_VAL_2024-25":
                    match = match and abs(m["net$"] - ANCHOR_C0_Y4_NET) <= 0.01
                rec = {"trades": exp_t, "PF": exp_pf, "maxDD%": exp_dd}
            elif cell == "P":
                exp_t, exp_pf, exp_net = ANCHORS_P[args.window]
                match = (m["records"] == exp_t and abs(m["PF"] - exp_pf) <= 0.0006
                         and abs(m["net$"] - exp_net) <= 0.01)
                rec = {"records": exp_t, "PF": exp_pf, "net$": exp_net}
            else:
                raise SystemExit(f"no recorded anchor for cell {cell}")
            ok = ok and match
            out[f"anchor_{cell}_{args.window}"] = {"measured": m, "recorded": rec, "match": match}
    finally:
        e22.uninstall_fast_path()
    print("FIDELITY " + json.dumps(out, default=str))
    return 0 if ok else 1


def mode_grid(args) -> int:
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        for cell in args.cells.split(","):
            bt, fn = build_cfg(cfg, cell)
            trades = run_backtest(df, SYMBOL, e22._SPEC, bt)
            row = {"window": args.window, "cell": cell, **portfolio_metrics(trades),
                   "gross_vetoed_signals": fn.gross_vetoed,
                   "tier_hist_at_admitted_signals": fn.tier_hist,
                   "entries": sorted({str(t.entry_time) for t in trades}),
                   "exit_mix": _mix([t.exit_reason for t in trades])}
            print("PORT " + json.dumps(row, default=str))
    finally:
        e22.uninstall_fast_path()
    return 0


def mode_removed(args) -> int:
    """The REMOVED POPULATION, sequence held FIXED on the C0 trade list: which
    of C0's own entries came from a partial-tier signal bar, and what those
    trades actually did. This is the per-year negativity check -- the same
    'measure the treated subset on the baseline sequence' method EXP-023 D3 /
    EXP-024 §6(2) / EXP-027 (a) used."""
    cfg = load_yaml_config("base")
    df = load_window(args.window)
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        bt, _ = build_cfg(cfg, "C0")
        trades = run_backtest(df, SYMBOL, e22._SPEC, bt)
        idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
        rows = []
        for t in trades:
            i_fill = idx[pd.Timestamp(t.entry_time)]
            i_sig = i_fill - 1  # engine: pending set on bar i, filled on bar i+1
            tier = leading_trend_tier(df, i_sig, t.direction, e22._SPEC, cfg["global"]["swing_pivot_bars"])
            rows.append({
                "window": args.window, "signal_time": str(df["time"].iloc[i_sig]),
                "entry": str(t.entry_time), "dir": t.direction, "tier": tier,
                "rC0": round(t.r_multiple, 6), "net": round(t.net_pnl, 2), "exit": t.exit_reason,
            })
    finally:
        e22.uninstall_fast_path()

    rem = [r for r in rows if r["tier"] == PARTIAL_TIER]
    kept = [r for r in rows if r["tier"] != PARTIAL_TIER]
    out = {
        "window": args.window, "C0_trades": len(rows),
        "tier_counts": _mix([str(r["tier"]) for r in rows]),
        "removed_n": len(rem),
        "removed_pct_of_C0": round(100.0 * len(rem) / len(rows), 2) if rows else None,
        "removed": group_stats([r["rC0"] for r in rem]),
        "kept": group_stats([r["rC0"] for r in kept]),
        "removed_net$": round(sum(r["net"] for r in rem), 2),
        "kept_net$": round(sum(r["net"] for r in kept), 2),
        "exit_mix_removed": _mix([r["exit"] for r in rem]),
        "exit_mix_kept": _mix([r["exit"] for r in kept]),
        "tier30": group_stats([r["rC0"] for r in rows if r["tier"] == 30]),
        "tier0": group_stats([r["rC0"] for r in rows if r["tier"] == 0]),
    }
    print("REMOVED " + json.dumps(out, default=str))
    for r in rows:
        print("POS " + json.dumps(r, default=str))
    return 0


def mode_pool(args) -> int:
    recs = [json.loads(l[8:]) for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
            if l.startswith("REMOVED ")]

    def _pool(rs):
        if not rs:
            return {}
        n_all = sum(r["C0_trades"] for r in rs)
        n_rem = sum(r["removed_n"] for r in rs)
        v = e27._pool_groups(rs, "removed")
        k = e27._pool_groups(rs, "kept")
        diff = None
        if v.get("n", 0) > 1 and k.get("n", 0) > 1:
            se = math.sqrt(v["se"] ** 2 + k["se"] ** 2)
            d = v["avgR"] - k["avgR"]
            diff = {"removed_minus_kept": round(d, 4), "se": round(se, 4),
                    "t": round(d / se, 3) if se else None,
                    "ci95": [round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)]}
        return {"C0_trades": n_all, "removed_n": n_rem,
                "removed_pct": round(100.0 * n_rem / n_all, 2) if n_all else None,
                "removed_net$": round(sum(r["removed_net$"] for r in rs), 2),
                "removed": v, "kept": k, "removed_minus_kept": diff,
                "tier30": e27._pool_groups(rs, "tier30"), "tier0": e27._pool_groups(rs, "tier0")}

    print("POOL " + json.dumps({
        "TRAIN": _pool([r for r in recs if r["window"] in TRAIN]),
        "VAL": _pool([r for r in recs if r["window"] in VAL]),
        "per_window": {r["window"]: {"C0_trades": r["C0_trades"], "removed_n": r["removed_n"],
                                     "removed_pct": r["removed_pct_of_C0"],
                                     "removed": r["removed"], "kept": r["kept"],
                                     "removed_net$": r["removed_net$"],
                                     "exit_mix_removed": r["exit_mix_removed"]} for r in recs},
    }, default=str))
    return 0


def mode_refill(args) -> int:
    """Slot-refill accounting, EXP-027 (d)'s method: a vetoed signal frees the
    single slot, so the V arm takes later signals C0 never had room for. The
    arithmetic identity |C0| - |C0\\V| + |V\\C0| == |V| is asserted per window."""
    port = [json.loads(l[5:]) for l in Path(args.gridfile).read_text(encoding="utf-8").splitlines()
            if l.startswith("PORT ")]
    cells = {(r["window"], r["cell"]): r for r in port}
    out = {}
    for (w, c) in list(cells):
        if c != "C0":
            continue
        c0, v = cells[(w, "C0")], cells.get((w, "V"))
        if v is None:
            continue
        s0, sv = set(c0["entries"]), set(v["entries"])
        out[w] = {
            "C0_positions": c0["positions"], "V_positions": v["positions"],
            "net_change": v["positions"] - c0["positions"],
            "net_change_pct": round(100.0 * (v["positions"] - c0["positions"]) / c0["positions"], 2),
            "gross_vetoed_signals_in_V_run": v["gross_vetoed_signals"],
            "C0_entries_absent_from_V": len(s0 - sv),
            "V_entries_absent_from_C0": len(sv - s0),
            "shared_entries": len(s0 & sv),
            "replacement_rate_pct": round(100.0 * len(sv - s0) / len(s0 - sv), 2) if (s0 - sv) else None,
            "identity_check": (len(s0) - len(s0 - sv) + len(sv - s0)) == len(sv),
            "gate1_200_trade_floor": {"C0": c0["records"] >= 200, "V": v["records"] >= 200},
        }
    print("REFILL " + json.dumps(out, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["fidelity", "grid", "removed", "pool", "refill"])
    p.add_argument("--scope", default="anchor", choices=["fastpath", "seam", "anchor"])
    p.add_argument("--window", default="y1_2021-22", choices=sorted(e27.YEARS))
    p.add_argument("--cells", default="C0,V")
    p.add_argument("--infile", default="experiments/exp028_removed_out.txt")
    p.add_argument("--gridfile", default="experiments/exp028_port_out.txt")
    args = p.parse_args()
    return {"fidelity": mode_fidelity, "grid": mode_grid, "removed": mode_removed,
            "pool": mode_pool, "refill": mode_refill}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
