#!/usr/bin/env python3
"""EXP-016 harness: Council scoring — trend_alignment PARTIAL-tier point value.

Single parameter under test: the `trend_partial` weight in
`council/scoring.py`'s trend_alignment component. Baseline awards 30 pts for
FULL EMA alignment (EMA20>EMA50>EMA200 bull / reversed bear), 15 pts for
PARTIAL (EMA20>EMA50 only). The 2026-07-23 scoring NOTE flagged the partial
tier as correlating with net-LOSING trades (avgR -0.022 in the fired set),
but with a selection-confound caveat that must be re-measured as a real
trade-set change (exactly what EXP-015 proved matters).

Candidates: trend_partial in {15 (BASE/live), 0 (drop the tier — a trade
needs FULL alignment for ANY trend credit), 7 (midpoint)}. Every OTHER
component and all 3 thresholds are held at the live config, isolating this
one variable. NOT combined with the already-rejected EXP-015 reweighting
(confluence stays the live inert +15 constant, all other weights untouched).

Mechanism of the trade-set change: lowering trend_partial removes points from
bars that have partial-but-not-full alignment. Such a bar scores 15 (or 8)
lower -> some drop below the 70/55 thresholds -> a genuinely NEW, smaller
fired set (not a relabeling of the baseline set). Bars with FULL alignment
(30) or no alignment (0) are unchanged.

This directly reuses the EXP-015 vectorised-scorer + fidelity machinery
(imported, not re-implemented) — only the WEIGHTS dict differs.

No production code modified: config/base.yaml, council/*, engine.py UNCHANGED
on disk. Config = adopted live-equivalent: commission 0, all-24h
(risk_voice=None), tp 2.0, sl 0.2/0.8/2.5, pivot 3, thresholds 70/70/55,
Watchman/Shield OFF, cost model on (slippage = bar's own spread).
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

# Reuse EXP-015 machinery verbatim (same folder).
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from exp015_reweight_harness import (  # noqa: E402
    BASE,
    STOCK_BEAR,
    STOCK_BULL,
    _SPEC,
    build_components,
    make_fast_scores,
    patch,
    run,
    slice_win,
    unpatch,
)
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402

# --- Candidates: only trend_partial changes; everything else == BASE (live) ---
def _variant(partial):
    w = dict(BASE)
    w["trend_partial"] = partial
    return w

WEIGHTS = {
    "BASE_p15": _variant(15),  # live
    "P0_drop": _variant(0),   # require full alignment for any trend credit
    "P7_mid": _variant(7),    # midpoint
}

TRAIN_YEARS = [
    ("Y1", "2021-07-22", "2022-07-21"),
    ("Y2", "2022-07-21", "2023-07-21"),
    ("Y3", "2023-07-21", "2024-07-21"),
    ("TRAIN", "2021-07-22", "2024-07-21"),
]
VALIDATION = ("VAL", "2024-07-21", "2025-07-21")


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "train"
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    # Sanity: confirm the candidates actually differ from BASE only in trend_partial.
    for name, w in WEIGHTS.items():
        diff = {k: w[k] for k in w if w[k] != BASE[k]}
        assert set(diff) <= {"trend_partial"}, f"{name} changed more than trend_partial: {diff}"
    print("ISOLATION check: all candidates differ from BASE only in trend_partial", flush=True)

    # --- Fidelity 1: fast BASE_p15 scorer == real scorer on 300 random Train bars ---
    tr = slice_win(df, "2021-07-22", "2024-07-21")
    comp = build_components(tr)
    fb, fbe = make_fast_scores(comp, WEIGHTS["BASE_p15"])
    rng = np.random.default_rng(43)
    sample = rng.choice(np.arange(210, len(tr)), size=300, replace=False)
    mism = 0
    for i in sample:
        i = int(i)
        if fb(tr, i, _SPEC).score != STOCK_BULL(tr, i, _SPEC).score:
            mism += 1
        if fbe(tr, i, _SPEC).score != STOCK_BEAR(tr, i, _SPEC).score:
            mism += 1
    print(f"FIDELITY-1 random-bar score match: {600 - mism}/600 (mismatch={mism})", flush=True)

    # --- Fidelity 2: fast BASE backtest == stock backtest on a small window ---
    sw = slice_win(df, "2022-01-01", "2022-05-01")
    unpatch()
    stock = run(sw)
    csw = build_components(sw)
    patch(csw, WEIGHTS["BASE_p15"])
    fast = run(sw)
    unpatch()
    print("FIDELITY-2 stock =", json.dumps(stock), flush=True)
    print("FIDELITY-2 fast  =", json.dumps(fast), flush=True)
    print("FIDELITY-2 match =", stock == fast, flush=True)
    print(flush=True)

    windows = list(TRAIN_YEARS)
    if stage == "val":
        windows.append(VALIDATION)

    for yname, s, e in windows:
        wdf = slice_win(df, s, e)
        c = build_components(wdf)
        for wname, w in WEIGHTS.items():
            patch(c, w)
            r = run(wdf)
            print(f"RESULT {wname:10s} {yname:6s} " + json.dumps(r), flush=True)
        unpatch()


if __name__ == "__main__":
    main()
