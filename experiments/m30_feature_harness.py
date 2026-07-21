#!/usr/bin/env python3
"""EXP-007 harness: does M30-resolution structure BEFORE an H1 entry signal
discriminate H1 trade outcomes? Direct M30 analogue of EXP-005's
m15_feature_harness.py.

ANALYSIS-ONLY. Does not modify any production code, config, or the engine.
It (1) runs the STOCK H1 backtest (same engine + stock signal fn + cost model
as m15_feature_harness.py, all-24h / Risk-Voice-off, tp=2.0 defaults -> the
adopted live-equivalent config after EXP-003), (2) for each realized H1 trade
recovers the H1 SIGNAL bar (the H1 bar immediately before the fill bar), (3)
attaches features computed ONLY from M30 bars that had already CLOSED by the
H1 signal bar's close (no lookahead: the last M30 bar used closes at exactly
the fill-bar open), and (4) writes a per-trade JSONL for bucketing.

RESOLUTION NOTE vs EXP-005: an H1 bar is composed of exactly 2 M30 bars (vs 4
M15 bars). So the M30 features carry LESS intra-hour information than M15 did,
not more. All bar-count constants are re-derived in M30 units to preserve the
same *wall-clock* windows EXP-005 used (1h = 2 M30 bars, 2h = 4 M30 bars,
3h context = 6 M30 bars), so the two studies are like-for-like on time span.

No-lookahead contract: H1 signal bar has time = sig_open, closes at
sig_open+1h = fill-bar open = trade.entry_time. M30 bars used are those with
time < entry_time (already closed). The 2 most recent are exactly the M30
bars composing the signal H1 bar; more are pulled for slope/EMA/RSI context.
"""
from __future__ import annotations
import json, sys
import numpy as np
import pandas as pd
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.feed.historical import HISTORICAL_DIR

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

LOOKBACK_M30 = 6  # M30 bars of context before/at signal close (3 hours)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)


def m30_features(seg: pd.DataFrame, direction: str) -> dict | None:
    """seg = M30 bars with time < entry_time, chronological. Uses the last
    LOOKBACK_M30. Returns direction-signed features (positive = aligned with
    the H1 trade direction) or None if insufficient bars."""
    if len(seg) < LOOKBACK_M30:
        return None
    w = seg.iloc[-LOOKBACK_M30:].reset_index(drop=True)
    sgn = 1.0 if direction == "BUY" else -1.0
    c = w["close"].to_numpy(); o = w["open"].to_numpy()
    h = w["high"].to_numpy(); l = w["low"].to_numpy()
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr_m30 = tr[-4:].mean()  # last 2h
    if atr_m30 <= 0:
        atr_m30 = max(h.max() - l.min(), 1e-6)

    # slopes over last 1h (2 M30 bars) and 2h (4 M30 bars), signed & ATR-normalized
    slope_1h = sgn * (c[-1] - c[-2]) / atr_m30
    slope_2h = sgn * (c[-1] - c[-4]) / atr_m30
    # EMA alignment on M30 (9 vs 21) at signal close
    e9 = pd.Series(seg["close"].to_numpy()).ewm(span=9, adjust=False).mean().iloc[-1]
    e21 = pd.Series(seg["close"].to_numpy()).ewm(span=21, adjust=False).mean().iloc[-1]
    ema_align = 1 if (sgn * (e9 - e21)) > 0 else 0
    # consecutive last M30 bars closing in signal direction
    consec = 0
    for k in range(len(w) - 1, -1, -1):
        if sgn * (c[k] - o[k]) > 0:
            consec += 1
        else:
            break
    # last M30 bar range vs prior-4 mean => contraction(<1)/expansion(>1)
    last_range = h[-1] - l[-1]
    prior_mean = (h[-5:-1] - l[-5:-1]).mean()
    range_exp = last_range / prior_mean if prior_mean > 0 else 1.0
    # signal-direction rejection wick fraction on last bar
    rng = max(h[-1] - l[-1], 1e-9)
    if direction == "BUY":
        wick = (min(o[-1], c[-1]) - l[-1]) / rng   # lower wick => buyers rejected lows
    else:
        wick = (h[-1] - max(o[-1], c[-1])) / rng   # upper wick => sellers rejected highs
    # RSI(14) on M30 at signal close, and signed distance from 50
    rsi_val = float(rsi(pd.Series(seg["close"].to_numpy())).iloc[-1])
    rsi_aligned = sgn * (rsi_val - 50.0)
    # position of last close within last-2h M30 range (0=low,1=high) signed:
    # for BUY, high position = extended/late; for SELL invert so high=extended
    lo2, hi2 = l[-4:].min(), h[-4:].max()
    pos = (c[-1] - lo2) / max(hi2 - lo2, 1e-9)
    pos_signed = pos if direction == "BUY" else (1 - pos)  # 1 = extended in trade dir

    return {
        "m30_slope_1h": round(float(slope_1h), 4),
        "m30_slope_2h": round(float(slope_2h), 4),
        "m30_ema_align": int(ema_align),
        "m30_consec": int(consec),
        "m30_range_exp": round(float(range_exp), 4),
        "m30_wick": round(float(wick), 4),
        "m30_rsi_aligned": round(float(rsi_aligned), 4),
        "m30_pos_ext": round(float(pos_signed), 4),
    }


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "2021-07-22"  # M30 covers full Train
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-07-21"    # exclude Test
    out_path = sys.argv[3] if len(sys.argv) > 3 else "experiments/m30_trades.jsonl"

    h1 = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    m30 = pd.read_csv(HISTORICAL_DIR / "XAUUSD_M30.csv", parse_dates=["time"])
    m30 = m30.sort_values("time").reset_index(drop=True)

    dfh = h1[(h1["time"] >= pd.Timestamp(start)) & (h1["time"] < pd.Timestamp(end))].reset_index(drop=True)

    cfg = BacktestConfig(
        starting_equity=10000.0, risk_per_trade_pct=0.5,
        cost_model=CostModelConfig(commission_per_lot=7.0, slippage_points=None),
    )
    trades = run_backtest(dfh, "XAUUSD", _SPEC, cfg)

    h1_time_to_idx = {t: i for i, t in enumerate(dfh["time"])}

    rows = []
    skipped = 0
    for t in trades:
        et = pd.Timestamp(t.entry_time)
        fill_idx = h1_time_to_idx.get(et)
        if fill_idx is None or fill_idx == 0:
            skipped += 1
            continue
        sig_time = dfh["time"].iloc[fill_idx - 1]
        seg = m30[m30["time"] < et]
        feats = m30_features(seg, t.direction)
        if feats is None:
            skipped += 1
            continue
        bars_held = (pd.Timestamp(t.exit_time) - et) / pd.Timedelta(hours=1)
        row = {
            "entry_time": str(et), "sig_time": str(sig_time),
            "direction": t.direction, "exit_reason": t.exit_reason,
            "r_multiple": round(float(t.r_multiple), 4), "net_pnl": round(float(t.net_pnl), 2),
            "win": int(t.net_pnl > 0), "bars_held": round(float(bars_held), 2),
            "year": int(et.year),
            **feats,
        }
        rows.append(row)

    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"trades total={len(trades)} usable={len(rows)} skipped={skipped} -> {out_path}")
    print(f"window {dfh['time'].iloc[0]} -> {dfh['time'].iloc[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
