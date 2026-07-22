#!/usr/bin/env python3
"""EXP-011 harness: run the STOCK Council/engine NATIVELY on M30 (or M15) bars
as the PRIMARY decision timeframe, with an INDEPENDENTLY tunable parameter set.

ANALYSIS-ONLY. Does not modify any production code, config, or the engine. It
feeds M30/M15 OHLC(+spread) bars straight into `backtest.engine.run_backtest`,
so Bull/Bear scoring, Decision Matrix, order construction, sizing, (optionally)
Risk Voice and Watchman all run NATIVELY on the chosen timeframe's bars -- NOT
H1. Every tunable knob exposed on `BacktestConfig` can be swept independently of
the H1-tuned `config/base.yaml` values, which is the whole point of EXP-011
(the earlier TF-probe reused H1 thresholds on faster bars -- a known mismatched
comparison; this harness lets the faster TF be tuned on its own terms).

WHAT IS AND IS NOT RE-TUNABLE HERE (honesty note, mirrors the pre-registration):
  Config-exposable (swept via BacktestConfig, no source edit):
    sl_buffer_atr, sl_min_atr, sl_max_atr, tp_r_multiple, pivot_bars,
    bull/bear/conflict thresholds, risk_voice session/spread, watchman
    time_stop_hours / dead_trade_r_band / be+trail.
  NOT config-exposable (module constants in features/indicators.py &
  council/scoring.py -- would need a monkeypatch/code change, OUT of the
  primary sweep scope, flagged): EMA 20/50/200 trend backbone, RSI 14,
  MACD 12/26/9, rolling_average 480-bar (=20d H1) window. On M30 these span
  HALF the wall-clock of H1 (EMA200 = 100h not 200h). Leaving them native is a
  legitimate "M30 reacts faster to the same indicator definitions" reading;
  rescaling them is a separate, deeper follow-up (noted in the log verdict).

Cost model ON throughout: commission per lot passed explicitly (IC Markets
Standard = $0), slippage = bar's own spread (min-1-spread), spread baked into
fill. Data must be the spread-floored CSVs (feed/historical SPREAD_ZERO_FLOOR).
"""
from __future__ import annotations
import json, sys
import numpy as np
import pandas as pd
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.watchman.evaluate import WatchmanConfig

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)
_POINT_VALUE = _SPEC.tick_value / _SPEC.tick_size  # 100 $/price-unit/lot

# Per-year boundaries aligned to the H1 log's splits (chronological, no shuffle).
YEARS = {
    "Y1": ("2021-07-22", "2022-07-21"),  # Train
    "Y2": ("2022-07-21", "2023-07-21"),  # Train
    "Y3": ("2023-07-21", "2024-07-21"),  # Train
    "Y4": ("2024-07-21", "2025-07-21"),  # Validation
    "Y5": ("2025-07-21", "2026-07-21"),  # Test  (touch once, only for a chosen candidate)
}


def _load(tf: str) -> pd.DataFrame:
    df = pd.read_csv(HISTORICAL_DIR / f"XAUUSD_{tf}.csv", parse_dates=["time"])
    return df.sort_values("time").reset_index(drop=True)


def _make_watchman(mode: str) -> WatchmanConfig | None:
    """mode: 'off' | 'structtime' (live-equivalent: struct-invalidation always on,
    time-stop on, be/trail OFF) with time_stop in HOURS (wall-clock, TF-agnostic)."""
    if mode == "off":
        return None
    return WatchmanConfig(
        breakeven_at_r=1.0, trail_start_r=1.5, trail_distance_atr=1.0,
        time_stop_hours=48, dead_trade_r_band=0.3,
        news_window_minutes=30, news_profit_threshold_r=0.5, news_close_mode="half",
        connectivity_timeout_minutes=5,
        breakeven_enabled=False, trail_enabled=False,
    )


def metrics(trades: list, equity0: float) -> dict:
    if not trades:
        return dict(trades=0, win=0.0, pf=0.0, net=0.0, avgr=0.0, dd=0.0, pf_ex5=0.0, hold=0.0)
    nets = np.array([t.net_pnl for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    holds = np.array([(pd.Timestamp(t.exit_time) - pd.Timestamp(t.entry_time)) / pd.Timedelta(hours=1) for t in trades])
    wins = nets[nets > 0].sum()
    losses = -nets[nets < 0].sum()
    pf = wins / losses if losses > 0 else float("inf")
    # PF excluding top-5 winners
    order = np.argsort(nets)[::-1]
    ex5 = nets.copy()
    ex5_idx = order[:5]
    keep = np.ones(len(nets), dtype=bool); keep[ex5_idx] = False
    w2 = nets[keep][nets[keep] > 0].sum(); l2 = -nets[keep][nets[keep] < 0].sum()
    pf_ex5 = w2 / l2 if l2 > 0 else float("inf")
    # equity curve max drawdown (%) on compounding equity
    eq = equity0 + np.cumsum(nets)
    peak = np.maximum.accumulate(np.concatenate([[equity0], eq]))
    dd = ((peak - np.concatenate([[equity0], eq])) / peak).max() * 100
    return dict(
        trades=len(trades), win=round(float((nets > 0).mean() * 100), 1),
        pf=round(float(pf), 4), net=round(float(nets.sum()), 1),
        avgr=round(float(rs.mean()), 4), dd=round(float(dd), 2),
        pf_ex5=round(float(pf_ex5), 4), hold=round(float(np.median(holds)), 1),
    )


def run_year(df: pd.DataFrame, y0: str, y1: str, cfg: BacktestConfig) -> dict:
    d = df[(df["time"] >= pd.Timestamp(y0)) & (df["time"] < pd.Timestamp(y1))].reset_index(drop=True)
    trades = run_backtest(d, "XAUUSD", _SPEC, cfg)
    return metrics(trades, cfg.starting_equity)


def build_cfg(commission: float, watchman_mode: str, risk_voice: bool,
              sl_buffer: float, sl_min: float, sl_max: float, tp: float, pivot: int,
              bull: int, bear: int, conflict: int, equity: float = 10000.0) -> BacktestConfig:
    rv = None
    if risk_voice:
        rv = RiskVoiceConfig(
            max_spread_multiple=1.5, max_spread_points_xauusd=35,
            news_blackout_before_min=45, news_blackout_after_min=30,
            max_stop_atr_multiple=2.5, session_start_hour=0, session_end_hour=24,
            friday_close_hour=20, max_atr_panic_multiple=3.0,
        )
    return BacktestConfig(
        starting_equity=equity, risk_per_trade_pct=1.0,
        cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
        sl_buffer_atr=sl_buffer, sl_min_atr=sl_min, sl_max_atr=sl_max,
        tp_r_multiple=tp, pivot_bars=pivot,
        bull_threshold=bull, bear_threshold=bear, conflict_threshold=conflict,
        risk_voice_cfg=rv, watchman_cfg=_make_watchman(watchman_mode),
    )


def main() -> int:
    # CLI: tf  years(comma)  watchman(off|structtime)  rv(0|1)  sl_buffer sl_min sl_max tp pivot bull bear conflict  [commission]
    a = sys.argv
    tf = a[1] if len(a) > 1 else "M30"
    years = (a[2].split(",") if len(a) > 2 else ["Y1", "Y2", "Y3", "Y4"])
    wm = a[3] if len(a) > 3 else "off"
    rv = bool(int(a[4])) if len(a) > 4 else False
    sl_buffer = float(a[5]) if len(a) > 5 else 0.2
    sl_min = float(a[6]) if len(a) > 6 else 0.8
    sl_max = float(a[7]) if len(a) > 7 else 2.5
    tp = float(a[8]) if len(a) > 8 else 2.0
    pivot = int(a[9]) if len(a) > 9 else 3
    bull = int(a[10]) if len(a) > 10 else 70
    bear = int(a[11]) if len(a) > 11 else 70
    conflict = int(a[12]) if len(a) > 12 else 55
    commission = float(a[13]) if len(a) > 13 else 0.0

    df = _load(tf)
    cfg = build_cfg(commission, wm, rv, sl_buffer, sl_min, sl_max, tp, pivot, bull, bear, conflict)
    tag = f"tf={tf} wm={wm} rv={int(rv)} slb={sl_buffer} slmin={sl_min} slmax={sl_max} tp={tp} piv={pivot} thr={bull}/{bear}/{conflict} comm={commission}"
    out = {"tag": tag}
    for y in years:
        y0, y1 = YEARS[y]
        out[y] = run_year(df, y0, y1, cfg)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
