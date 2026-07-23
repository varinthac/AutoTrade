#!/usr/bin/env python3
"""EXP-015 harness: Council scoring WEIGHT-REALLOCATION (confluence's dead +15
redistributed to discriminating components), TRAIN-ONLY per-year evaluation.

Mechanism: the Council scores Bull/Bear 0-100 (council/scoring.py). The
`confluence` component is a structural constant +15 on every bar (prior NOTE
2026-07-23). This harness reweights the 5 components while KEEPING the score's
max at 100 (so bull/bear_threshold=70 and conflict_threshold=55 retain their
exact meaning), then re-runs the STOCK engine + STOCK decision-matrix +
STOCK order construction. Only the two score functions are swapped (monkeypatch
in the decision_matrix namespace) for a FAST vectorised reweighted version.
Every candidate produces a genuinely NEW trade set (weights change which bars
cross 70), not a relabeling of the baseline set.

Speed: all four scored components are causal (adjust=False EWM; swing = fractal
with right-side confirmation only ever using bars <= as_of_index), so per-bar
values equal the production scorer's per-slice .iloc[-1]. Precompute once per
window, then O(1) lookup per bar. Fidelity is PROVEN two ways below:
(1) fast BASE scores == real score_bull/bear_voice on 300 random bars;
(2) fast BASE backtest == stock backtest on a small window, trade-for-trade.

No production code modified: config/base.yaml, council/*, engine.py UNCHANGED
on disk. Config = adopted live-equivalent: commission 0, all-24h
(risk_voice=None), tp 2.0, sl 0.2/0.8/2.5, pivot 3, thresholds 70/70/55,
Watchman/Shield OFF, cost model on (slippage = bar's own spread).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import autotrade.council.decision_matrix as dm
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import generate_report
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.scoring import (
    BullBearScore,
    score_bear_voice as STOCK_BEAR,
    score_bull_voice as STOCK_BULL,
)
from autotrade.features.indicators import atr, ema, macd_histogram, rsi
from autotrade.features.swing import detect_swings
from autotrade.feed.historical import HISTORICAL_DIR

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)

PIVOT = 3


def _struct_array(idx_list, close, n, pivot, kind):
    """Vectorise is_higher_low (kind='low') / is_lower_high (kind='high') using
    a precomputed sorted list of swing indices + a confirmation pointer."""
    out = np.zeros(n, dtype=bool)
    p = 0  # count of swing indices with idx <= i - pivot
    j = 0
    for i in range(n):
        limit = i - pivot
        while j < len(idx_list) and idx_list[j] <= limit:
            j += 1
            p = j
        if p >= 2:
            latest, prev = idx_list[p - 1], idx_list[p - 2]
            if kind == "low":
                out[i] = close[latest] > close[prev] and close[i] > close[latest]
            else:
                out[i] = close[latest] < close[prev] and close[i] < close[latest]
    return out


def _struct_array_hl(idx_list, series_vals, close, n, pivot, kind):
    """Same as _struct_array but the swing anchor value comes from a separate
    series (low for higher-low, high for lower-high) -- matches swing.py which
    compares df.loc[idx,'low']/'high', while 'currently holding' uses close."""
    out = np.zeros(n, dtype=bool)
    j = 0
    for i in range(n):
        limit = i - pivot
        while j < len(idx_list) and idx_list[j] <= limit:
            j += 1
        p = j
        if p >= 2:
            latest, prev = idx_list[p - 1], idx_list[p - 2]
            if kind == "low":
                out[i] = series_vals[latest] > series_vals[prev] and close[i] > series_vals[latest]
            else:
                out[i] = series_vals[latest] < series_vals[prev] and close[i] < series_vals[latest]
    return out


def _confluence_array(wdf, atr_vals):
    """confluence_fires[i] = is_near_key_level(close[i], atr[i], [round, pivot?]).
    Exact reimplementation of _confluence_score's boolean, causal, vectorised."""
    close = wdf["close"].to_numpy()
    n = len(close)
    # round level (granularity 0.5 for gold, digits<=3)
    round_level = np.round(close / 0.5) * 0.5
    dist_round = np.abs(close - round_level)
    thr = 0.5 * atr_vals
    fires = dist_round <= thr
    # prior-day pivot level
    days = wdf["time"].dt.normalize()
    uniq = list(dict.fromkeys(days.tolist()))  # ordered unique days
    day_high = wdf.groupby(days)["high"].max()
    day_low = wdf.groupby(days)["low"].min()
    day_close = wdf.groupby(days)["close"].last()
    pivot_of = {}
    for k in range(1, len(uniq)):
        pd_ = uniq[k - 1]
        pivot_of[uniq[k]] = (day_high[pd_] + day_low[pd_] + day_close[pd_]) / 3.0
    day_arr = days.to_numpy()
    for i in range(n):
        if fires[i]:
            continue
        piv = pivot_of.get(day_arr[i])
        if piv is not None and abs(close[i] - piv) <= thr[i]:
            fires[i] = True
    return fires


def build_components(wdf):
    """Precompute all per-bar component fire/tier arrays for one window."""
    closes = wdf["close"]
    n = len(wdf)
    ema20 = ema(closes, 20).to_numpy()
    ema50 = ema(closes, 50).to_numpy()
    ema200 = ema(closes, 200).to_numpy()
    rsi_v = rsi(closes, 14).to_numpy()
    hist = macd_histogram(closes).to_numpy()
    hist_prev = np.concatenate([[np.nan], hist[:-1]])
    atr_vals = atr(wdf["high"], wdf["low"], closes).to_numpy()

    # trend tier: 2=full, 1=partial, 0=none
    tcat_bull = np.where(
        (ema20 > ema50) & (ema50 > ema200), 2, np.where(ema20 > ema50, 1, 0)
    )
    tcat_bear = np.where(
        (ema20 < ema50) & (ema50 < ema200), 2, np.where(ema20 < ema50, 1, 0)
    )
    rsi_bull = (rsi_v >= 50) & (rsi_v <= 70)
    rsi_bear = (rsi_v >= 30) & (rsi_v <= 50)
    macd_bull = (hist > 0) & (hist > hist_prev)
    macd_bear = (hist < 0) & (hist < hist_prev)

    sw = detect_swings(wdf, pivot_bars=PIVOT)
    low_idx = list(np.where(sw["swing_low"].to_numpy())[0])
    high_idx = list(np.where(sw["swing_high"].to_numpy())[0])
    close = closes.to_numpy()
    low_vals = wdf["low"].to_numpy()
    high_vals = wdf["high"].to_numpy()
    struct_bull = _struct_array_hl(low_idx, low_vals, close, n, PIVOT, "low")
    struct_bear = _struct_array_hl(high_idx, high_vals, close, n, PIVOT, "high")

    conf = _confluence_array(wdf, atr_vals)

    return {
        "tcat_bull": tcat_bull, "tcat_bear": tcat_bear,
        "rsi_bull": rsi_bull, "rsi_bear": rsi_bear,
        "macd_bull": macd_bull, "macd_bear": macd_bear,
        "struct_bull": struct_bull, "struct_bear": struct_bear,
        "conf": conf,
    }


def make_fast_scores(comp, w):
    """Return (bull_fn, bear_fn) matching score_bull/bear_voice's signature but
    O(1) array lookups, with component points from weight dict `w`."""
    def _fn(direction):
        tcat = comp["tcat_" + direction]
        rsi_f = comp["rsi_" + direction]
        macd_f = comp["macd_" + direction]
        struct_f = comp["struct_" + direction]
        conf_f = comp["conf"]

        def score(df, as_of_index, symbol_spec, pivot_bars=3):
            i = as_of_index
            trend = w["trend_full"] if tcat[i] == 2 else (w["trend_partial"] if tcat[i] == 1 else 0)
            m_rsi = w["rsi"] if rsi_f[i] else 0
            m_macd = w["macd"] if macd_f[i] else 0
            ms = w["struct"] if struct_f[i] else 0
            cf = w["confluence"] if conf_f[i] else 0
            s = trend + m_rsi + m_macd + ms + cf
            return BullBearScore(
                score=int(s), trend_alignment=int(trend), momentum_rsi=int(m_rsi),
                momentum_macd=int(m_macd), market_structure=int(ms), confluence=int(cf),
            )
        return score
    return _fn("bull"), _fn("bear")


def patch(comp, w):
    b, be = make_fast_scores(comp, w)
    dm.score_bull_voice = b
    dm.score_bear_voice = be


def unpatch():
    dm.score_bull_voice = STOCK_BULL
    dm.score_bear_voice = STOCK_BEAR


def run(wdf):
    cfg = BacktestConfig(
        starting_equity=10000.0, risk_per_trade_pct=0.5,
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=None),
        sl_buffer_atr=0.2, sl_min_atr=0.8, sl_max_atr=2.5,
        tp_r_multiple=2.0, pivot_bars=PIVOT,
        bull_threshold=70, bear_threshold=70, conflict_threshold=55,
    )
    trades = run_backtest(wdf, "XAUUSD", _SPEC, cfg)
    rep = generate_report(trades, 10000.0)
    return {
        "trades": rep.trade_count, "win": round(rep.win_rate, 3),
        "PF": round(rep.profit_factor, 4), "net": round(rep.total_net_pnl, 1),
        "avgR": round(rep.avg_r_multiple, 4), "DD": round(rep.max_drawdown_pct, 3),
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4),
    }


BASE = dict(trend_full=30, trend_partial=15, rsi=20, macd=15, struct=20, confluence=15)
WEIGHTS = {
    "BASE": BASE,
    "C1_macd30": dict(trend_full=30, trend_partial=15, rsi=20, macd=30, struct=20, confluence=0),
    "C2_trend45": dict(trend_full=45, trend_partial=15, rsi=20, macd=15, struct=20, confluence=0),
    "C3_split": dict(trend_full=38, trend_partial=15, rsi=20, macd=22, struct=20, confluence=0),
}
YEARS = [
    ("Y1", "2021-07-22", "2022-07-21"),
    ("Y2", "2022-07-21", "2023-07-21"),
    ("Y3", "2023-07-21", "2024-07-21"),
    ("TRAIN", "2021-07-22", "2024-07-21"),
]


def slice_win(df, s, e):
    return df[(df["time"] >= pd.Timestamp(s)) & (df["time"] < pd.Timestamp(e))].reset_index(drop=True)


def main():
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    # --- Fidelity 1: fast BASE scores == real scorer on random bars (TRAIN slice) ---
    tr = slice_win(df, "2021-07-22", "2024-07-21")
    comp = build_components(tr)
    fb, fbe = make_fast_scores(comp, BASE)
    rng = np.random.default_rng(42)
    sample = rng.choice(np.arange(210, len(tr)), size=300, replace=False)
    mism = 0
    for i in sample:
        i = int(i)
        if fb(tr, i, _SPEC).score != STOCK_BULL(tr, i, _SPEC).score:
            mism += 1
        if fbe(tr, i, _SPEC).score != STOCK_BEAR(tr, i, _SPEC).score:
            mism += 1
    print(f"FIDELITY-1 random-bar score match: {600 - mism}/600 (mismatch={mism})")

    # --- Fidelity 2: fast BASE backtest == stock backtest on a small window ---
    sw = slice_win(df, "2022-01-01", "2022-05-01")
    unpatch()
    stock = run(sw)
    csw = build_components(sw)
    patch(csw, BASE)
    fast = run(sw)
    unpatch()
    print("FIDELITY-2 stock =", json.dumps(stock))
    print("FIDELITY-2 fast  =", json.dumps(fast))
    print("FIDELITY-2 match =", stock == fast)
    print()

    # --- Run all weight sets over per-year + full Train ---
    for yname, s, e in YEARS:
        wdf = slice_win(df, s, e)
        c = build_components(wdf)
        for wname, w in WEIGHTS.items():
            patch(c, w)
            r = run(wdf)
            print(f"RESULT {wname:12s} {yname:6s} " + json.dumps(r))
        unpatch()


if __name__ == "__main__":
    main()
