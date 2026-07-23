#!/usr/bin/env python3
"""DIAGNOSTIC (NOT an EXP) -- winter/summer seasonality + DST/server-time probe.

Two independent questions:
  (1) Is there a real month-of-year / season pattern in the trades the CURRENTLY
      ADOPTED config produces on the 5yr XAUUSD H1 history, and is it consistent
      per-year (not a one-year fluke)?
  (2) Does this broker's MT5 server clock observe DST? Empirical signature:
      does the hour-of-day of the London/NY volatility ramp SHIFT by ~1h around
      the DST transitions across the 5yr data? Plus data-quality checks in the
      transition weeks.

Reuses production run_backtest offline (manual SymbolSpec, like the martingale
harness). No src/ or config/ modified. Config = adopted base.yaml.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.shield.checkpoint import ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig

_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def pf(rs):
    rs = np.asarray(rs, dtype=float)
    g = rs[rs > 0].sum()
    l = -rs[rs < 0].sum()
    return (g / l) if l > 0 else float("inf")


def main():
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])
    cfg = load_yaml_config("base")
    print(f"Data: {df['time'].iloc[0]} -> {df['time'].iloc[-1]}  bars={len(df)}")

    # ---- config-faithful backtest (adopted values) ----
    rv = RiskVoiceConfig(
        max_spread_multiple=cfg["risk_voice"]["max_spread_multiple"],
        max_spread_points_xauusd=cfg["risk_voice"]["max_spread_points_xauusd"],
        news_blackout_before_min=cfg["risk_voice"]["news_blackout_before_min"],
        news_blackout_after_min=cfg["risk_voice"]["news_blackout_after_min"],
        max_stop_atr_multiple=cfg["risk_voice"]["max_stop_atr_multiple"],
        session_start_hour=cfg["risk_voice"]["session_start_hour"],
        session_end_hour=cfg["risk_voice"]["session_end_hour"],
        friday_close_hour=cfg["risk_voice"]["friday_close_hour"],
        max_atr_panic_multiple=cfg["risk_voice"]["max_atr_panic_multiple"],
    )
    wm = WatchmanConfig(
        breakeven_at_r=cfg["watchman"]["breakeven_at_r"],
        trail_start_r=cfg["watchman"]["trail_start_r"],
        trail_distance_atr=cfg["watchman"]["trail_distance_atr"],
        time_stop_hours=cfg["watchman"]["time_stop_hours"],
        dead_trade_r_band=cfg["watchman"]["dead_trade_r_band"],
        breakeven_enabled=cfg["watchman"]["breakeven_enabled"],
        trail_enabled=cfg["watchman"]["trail_enabled"],
    )
    sh = ShieldConfig(
        min_rr=cfg["shield"]["min_rr"], max_correlation=cfg["shield"]["max_correlation"],
        max_positions_per_symbol=cfg["shield"]["max_positions_per_symbol"],
        max_positions_total=cfg["shield"]["max_positions_total"],
        total_risk_ceiling_pct=cfg["shield"]["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=cfg["shield"]["duplicate_signal_cooldown_hours"],
    )
    bt = BacktestConfig(
        starting_equity=3000.0, risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(commission_per_lot=0.0, slippage_points=None),
        risk_voice_cfg=rv, watchman_cfg=wm, shield_cfg=sh,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
    )
    trades = run_backtest(df, "XAUUSD", _SPEC, bt)
    t = pd.DataFrame([{
        "entry": tr.entry_time, "r": tr.r_multiple, "net": tr.net_pnl,
        "win": 1 if tr.r_multiple > 0 else 0,
    } for tr in trades])
    t["entry"] = pd.to_datetime(t["entry"])
    t["month"] = t["entry"].dt.month
    # trade-year keyed to the strategy's Y1..Y5 (Jul 22 -> Jul 21 windows)
    t["yr"] = t["entry"].apply(lambda d: d.year if d.month >= 7 else d.year - 1)
    print(f"\n=== Q1 SEASONALITY ===  total trades={len(t)}  overall PF={pf(t['r']):.3f}  net=${t['net'].sum():.0f}")

    print("\n-- by MONTH-OF-YEAR (all 5yr pooled) --")
    print("mo  trades  win%   avgR    PF     net$")
    for m in range(1, 13):
        s = t[t["month"] == m]
        if len(s) == 0:
            continue
        print(f"{m:2d}  {len(s):5d}  {100*s['win'].mean():4.0f}  {s['r'].mean():+.3f}  {pf(s['r']):5.2f}  {s['net'].sum():+7.0f}")

    print("\n-- by SEASON (pooled) --  winter=Nov-Feb, spring=Mar-Apr, summer=May-Aug, autumn=Sep-Oct")
    seasons = {"winter(NDJF)": [11, 12, 1, 2], "summer(MJJA)": [5, 6, 7, 8],
               "shoulder(MA+SO)": [3, 4, 9, 10]}
    for name, mos in seasons.items():
        s = t[t["month"].isin(mos)]
        print(f"{name:16s} trades={len(s):4d} win%={100*s['win'].mean():4.0f} avgR={s['r'].mean():+.3f} PF={pf(s['r']):.3f} net=${s['net'].sum():+.0f}")

    print("\n-- SEASON x YEAR (per-year robustness of the season split) --  PF (net$, n)")
    print(f"{'season':16s} " + " ".join(f"{y:>16d}" for y in sorted(t['yr'].unique())))
    for name, mos in seasons.items():
        row = f"{name:16s} "
        for y in sorted(t["yr"].unique()):
            s = t[(t["month"].isin(mos)) & (t["yr"] == y)]
            if len(s) == 0:
                row += f"{'--':>16s} "
            else:
                row += f"{pf(s['r']):.2f}({s['net'].sum():+.0f},{len(s)}) ".rjust(17)
        print(row)

    # ---- Q2: DST / server-time signature ----
    print("\n\n=== Q2 DST / SERVER-TIME ===")
    d = df.copy()
    d["hour"] = d["time"].dt.hour
    d["month"] = d["time"].dt.month
    d["rng"] = d["high"] - d["low"]
    # summer months (DST active in US/EU: Apr-Oct) vs winter (Nov-Mar)
    d["dst_regime"] = np.where(d["month"].isin([4, 5, 6, 7, 8, 9, 10]), "summer", "winter")
    print("\n-- mean H1 bar range (high-low) by hour-of-day (server time), summer vs winter --")
    print("hr   summer   winter")
    piv = d.groupby(["hour", "dst_regime"])["rng"].mean().unstack()
    for h in range(24):
        su = piv.loc[h, "summer"] if h in piv.index else float("nan")
        wi = piv.loc[h, "winter"] if h in piv.index else float("nan")
        print(f"{h:2d}   {su:6.2f}   {wi:6.2f}")
    # locate the NY ramp (biggest range hours) in each regime
    for reg in ["summer", "winter"]:
        col = piv[reg]
        top = col.sort_values(ascending=False).head(4)
        print(f"{reg}: peak-range hours (server) = {list(top.index)}")

    # month-by-month peak-range hour to see the shift transition timing
    print("\n-- peak-range hour (server) per calendar month, pooled over 5yr --")
    print("mo peak_hr  top3_hours")
    for m in range(1, 13):
        s = d[d["month"] == m].groupby("hour")["rng"].mean()
        top3 = list(s.sort_values(ascending=False).head(3).index)
        print(f"{m:2d}   {s.idxmax():2d}     {top3}")

    # ---- data quality in DST transition weeks ----
    print("\n-- data-quality in DST transition weeks (missing bars / spread=0 / ATR spike) --")
    d["date"] = d["time"].dt.date
    # approximate EU/US transition dates per year
    trans = {
        2021: ["2021-03-14", "2021-03-28", "2021-10-31", "2021-11-07"],
        2022: ["2022-03-13", "2022-03-27", "2022-10-30", "2022-11-06"],
        2023: ["2023-03-12", "2023-03-26", "2023-10-29", "2023-11-05"],
        2024: ["2024-03-10", "2024-03-31", "2024-10-27", "2024-11-03"],
        2025: ["2025-03-09", "2025-03-30", "2025-10-26", "2025-11-02"],
        2026: ["2026-03-08", "2026-03-29"],
    }
    d = d.sort_values("time").reset_index(drop=True)
    gap = d["time"].diff()
    for yr, dates in trans.items():
        for ds in dates:
            c = pd.Timestamp(ds)
            wk = d[(d["time"] >= c - pd.Timedelta(days=2)) & (d["time"] < c + pd.Timedelta(days=3))]
            if wk.empty:
                continue
            zero_spread = int((wk["spread"] == 0).sum())
            wkgap = gap[wk.index]
            big_gaps = int((wkgap > pd.Timedelta(hours=1, minutes=30)).sum())
            maxgap = wkgap.max()
            print(f"{ds}: bars={len(wk)} zero_spread={zero_spread} gaps>1.5h={big_gaps} maxgap={maxgap} "
                  f"minspread={wk['spread'].min()} maxspread={wk['spread'].max()}")


if __name__ == "__main__":
    main()
