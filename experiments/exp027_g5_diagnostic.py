"""G5 quantification: decompose engine-vs-EXP024-harness 'affected@real' into
(i) eligibility-boundary inclusivity and (ii) the profit-gate float round-trip.

READ THE `affected_EXP024_rule` COLUMN WITH CARE: this loop breaks as soon as
the ENGINE fires, so that column stops counting there and UNDER-reports (51/41/
44/45). A separate no-break run of the same rule reproduced EXP-024 §1's
published affected@real counts exactly (y1 64, y3 67) -- that is the number the
log quotes. The three columns this script exists for
(`affected_engine_eligibility_rule`, `affected_engine_ACTUALLY_fired`,
`lost_to_profit_gate_roundtrip`) are unaffected by the break, because the break
only ever happens after a fire.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "experiments")
import exp022_minlot_harness as e22
import exp024_real_calendar_harness as e24
import exp026_minlot_skip_harness as e26
import exp027_entry_blackout_harness as e27
import autotrade.backtest.engine as engine_mod
from autotrade.common.config import load_yaml_config

cfg = load_yaml_config("base")
cal24 = e24.load_calendar(Path(e24.DEFAULT_CALENDAR))

for WIN in ["y1_2021-22", "y2_2022-23", "y3_2023-24", "y4_VAL_2024-25"]:
    df = e27.load_window(WIN)
    times = pd.to_datetime(df["time"]).to_numpy(dtype="datetime64[s]")
    span = np.timedelta64(90, "m")
    e24r = np.zeros(len(times), dtype=bool)
    engr = np.zeros(len(times), dtype=bool)
    for ev in cal24.events:
        ev = np.datetime64(ev, "s")
        lo = np.searchsorted(times, ev - span, side="right")
        hi = np.searchsorted(times, ev, side="left")
        if hi > lo:
            e24r[lo:hi] = True
        lo = np.searchsorted(times, ev - span, side="left")
        hi = np.searchsorted(times, ev, side="right")
        if hi > lo:
            engr[lo:hi] = True

    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        bt0 = e27.build_cfg(cfg, "C0")
        rec = e26._Recorder()
        full = e27.run_full(df, bt0, recorder=rec)
    finally:
        e22.uninstall_fast_path()
    bt_p = e27.build_cfg(cfg, "P")
    idx = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
    exit_i = {str(t.entry_time): idx[pd.Timestamp(t.exit_time)] for t in full}

    a24 = aeng = afired = 0
    bar_gate_rejects = 0          # elig bars with a candidate rejected by the profit gate
    bar_gate_rejects_boundary = 0  # ... of which the reason says exactly 0.50R
    dropped = []
    for kw in rec.entries:
        i0 = idx[pd.Timestamp(kw["entry_time"])]
        iE = exit_i[str(kw["entry_time"])]
        md = kw["metadata"]
        pos = e26._REAL_OPEN_POSITION(**dict(kw))
        h24 = heng = hfire = False
        for i in range(i0, iE + 1):
            bar = df.iloc[i]
            if engine_mod.check_exit(pos.plan.direction, pos.current_sl, pos.plan.take_profit, bar) is not None:
                break
            cp = engine_mod._news_trigger_candidate_price(
                kw["plan"].direction, md.entry_price, md.initial_stop_distance, 0.5, bar)
            if cp is None:
                continue
            if e24r[i]:
                h24 = True
            if not engr[i]:
                continue
            heng = True
            dec, _ = engine_mod._news_protection_decision_this_bar(
                pos, bar, pd.Timestamp(bar["time"]).to_pydatetime(), 60.0,
                bt_p.news_calendar, bt_p.news_protection_cfg)
            if dec.action == "NO_ACTION":
                bar_gate_rejects += 1
                if "below protection threshold" in dec.reason:
                    bar_gate_rejects_boundary += 1
            else:
                hfire = True
                break
        a24 += h24
        aeng += heng
        afired += hfire
        if heng and not hfire:
            dropped.append(str(kw["entry_time"]))
    print(json.dumps({
        "window": WIN, "positions": len(rec.entries),
        "affected_EXP024_rule": a24,
        "affected_engine_eligibility_rule": aeng,
        "affected_engine_ACTUALLY_fired": afired,
        "lost_to_profit_gate_roundtrip": len(dropped),
        "bar_level_gate_rejects_on_elig_candidate_bars": bar_gate_rejects,
        "of_which_reason_is_0.50R_boundary": bar_gate_rejects_boundary,
    }), flush=True)
