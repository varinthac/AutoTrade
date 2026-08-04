#!/usr/bin/env python3
"""TASK-1 driver -- the HONEST TEST BASELINE (2026-08-04).

A thin driver over the REAL production path (`scripts/run_backtest.py`'s own
`run_and_persist` -> `backtest.engine.run_backtest` -> `backtest/report.py` ->
the JSON envelope `auditor/promotion.py` reads). It is NOT a re-implementation
and NOT the exp023/024/025 harness family: per the 2026-08-04 log NOTE, only the
engine models the genuine `CLOSE_HALF_AND_BREAKEVEN` partial-close branch that
live executes whenever half-lot >= volume_min, so the engine path -- not a
harness -- is the live-faithful one for a promotion-gate number.

MEASUREMENT ONLY. No parameter is varied, no candidate exists, nothing under
`src/` or `config/` is modified, and no promotion threshold is touched or
proposed for change (rule 8).

Two Test-year arms:
  * mode C   -- news protection NOT modeled: the anchor, reproducing what every
                historical Gate-1 figure in this project has actually measured.
  * A@real   -- news protection modeled from the built historical calendar
                (`scripts/build_backtest_calendar.py`'s output): the honest
                baseline for the configuration that actually runs live.

The only deviation from `scripts/run_backtest.py`'s CLI: `SymbolSpec` is the
same hardcoded IC Markets XAUUSD spec `experiments/exp022_minlot_harness.py`
already uses (identical to what the CLI resolves from a live MT5 session), so
this runs without a terminal. EXP-022's validated fast-path memoisation shim is
installed for speed and re-proved identical in `--mode fidelity` first.

MODES:
  --mode fidelity   fast-path shim == real engine, trade-for-trade, news OFF and ON
  --mode anchor     y4/VAL external anchors (mode C 254/1.0961/+352.60/9.99%;
                    engine A@real 350/1.0667/+259.67/11.42%) -- NOT the Test year
  --mode run        one arm on one window; --window y5_TEST_2025-26 --arm C|A
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from autotrade.auditor.backtest_results import load_backtest_report_envelope  # noqa: E402
from autotrade.auditor.promotion import PromotionThresholds, evaluate_backtest_to_paper_gate  # noqa: E402
from autotrade.backtest.cost_model import CostModelConfig, SwapModelConfig  # noqa: E402
from autotrade.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from autotrade.backtest.historical_news_calendar import HistoricalNewsCalendarProvider  # noqa: E402
from autotrade.backtest.report import generate_report  # noqa: E402
from autotrade.common.config import REPO_ROOT, load_yaml_config  # noqa: E402
from autotrade.feed.historical import HISTORICAL_DIR  # noqa: E402
from autotrade.watchman.news_protection import NewsProtectionConfig  # noqa: E402

import exp022_minlot_harness as e22  # noqa: E402
import run_backtest as rb  # noqa: E402  (scripts/run_backtest.py -- the production CLI module)

SYMBOL = "XAUUSD"
CALENDAR = HISTORICAL_DIR / "news_calendar_backtest.csv"
YEARS = dict((name, (a, b)) for name, a, b in e22.YEARS)
EQUITY = 3000.0
COMMISSION = 0.0  # IC Markets Standard, the real account (EXP-022+ context)


def load_window(window: str) -> pd.DataFrame:
    start, end = YEARS[window]
    df = pd.read_csv(HISTORICAL_DIR / f"{SYMBOL}_H1.csv", parse_dates=["time"])
    df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end) + pd.Timedelta(hours=23))]
    return df.reset_index(drop=True)


def build_cfg(cfg, *, news: bool) -> BacktestConfig:
    rv_cfg, wm_cfg, sh_cfg, order = e22.build_cfgs(cfg)
    np_cfg = calendar = None
    if news:
        np_cfg = NewsProtectionConfig(
            news_window_minutes=cfg["watchman"]["news_window_minutes"],
            profit_threshold_r=cfg["watchman"]["news_profit_threshold_r"],
            close_mode=cfg["watchman"]["news_close_mode"],
        )
        calendar = HistoricalNewsCalendarProvider(CALENDAR)
    return BacktestConfig(
        starting_equity=EQUITY,
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        cost_model=CostModelConfig(
            commission_per_lot=COMMISSION, slippage_points=None,
            swap_model=SwapModelConfig(long_per_lot_per_night=e22.SWAP_LONG,
                                       short_per_lot_per_night=e22.SWAP_SHORT),
        ),
        risk_voice_cfg=rv_cfg, watchman_cfg=wm_cfg, shield_cfg=sh_cfg,
        news_protection_cfg=np_cfg, news_calendar=calendar,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=cfg["cfo"]["min_lot_risk_cap_pct"],
        **order,
    )


def metrics(trades) -> dict:
    rep = generate_report(trades, EQUITY)
    news_exits = [t for t in trades if t.exit_reason == "news_protection"]
    return {
        "trades": rep.trade_count,
        "PF": None if rep.profit_factor is None else round(rep.profit_factor, 4),
        "net$": round(rep.total_net_pnl, 2),
        "maxDD%": None if rep.max_drawdown_pct is None else round(rep.max_drawdown_pct, 4),
        "avgR": None if rep.avg_r_multiple is None else round(rep.avg_r_multiple, 4),
        "pf_ex5": None if rep.profit_factor_excluding_top_5 is None else round(rep.profit_factor_excluding_top_5, 4),
        "win_rate": None if rep.win_rate is None else round(rep.win_rate, 4),
        "news_protection_exits": len(news_exits),
    }


def mode_fidelity(args) -> int:
    cfg = load_yaml_config("base")
    df = load_window("y4_VAL_2024-25").iloc[:4000].reset_index(drop=True)
    out = {}
    for label, news in (("newsOFF", False), ("newsON", True)):
        bt = build_cfg(cfg, news=news)
        slow = run_backtest(df, SYMBOL, e22._SPEC, bt)
        e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
        try:
            fast = run_backtest(df, SYMBOL, e22._SPEC, bt)
        finally:
            e22.uninstall_fast_path()
        out[label] = {
            "n_slow": len(slow), "n_fast": len(fast),
            "identical": [asdict(t) for t in slow] == [asdict(t) for t in fast],
        }
    print("FIDELITY " + json.dumps(out, default=str))
    return 0 if all(v["identical"] for v in out.values()) else 1


def run_arm(window: str, arm: str, *, persist: bool, out_of_sample: bool) -> dict:
    cfg = load_yaml_config("base")
    df = load_window(window)
    bt = build_cfg(cfg, news=(arm == "A"))
    e22.install_fast_path(df, cfg["global"]["swing_pivot_bars"])
    try:
        trades = run_backtest(df, SYMBOL, e22._SPEC, bt)
    finally:
        e22.uninstall_fast_path()
    row = {
        "window": window, "arm": arm, "bars": len(df),
        "bar_range": [str(df["time"].iloc[0]), str(df["time"].iloc[-1])],
        **metrics(trades),
    }
    if persist:
        rep = generate_report(trades, EQUITY)
        env = rb.build_envelope(
            SYMBOL, df, rep, bt.cost_model, EQUITY, out_of_sample,
            risk_voice_modeled=True, watchman_exits_modeled=True, shield_modeled=True,
            news_protection_modeled=(arm == "A"),
            # 2026-08-04 (post-3ec55ee compat): the entry blackout stays
            # unmodeled in BOTH Task-1 arms -- the measurement pre-dates that
            # flag and its arms are defined as protection-only.
            risk_voice_news_modeled=False,
            min_lot_risk_cap_pct=bt.min_lot_risk_cap_pct,
        )
        out_dir = REPO_ROOT / "data" / "db" / "backtest_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{SYMBOL}_TASK1_{window}_{arm}.json"
        path.write_text(json.dumps(env, indent=2, default=str), encoding="utf-8")
        row["envelope"] = str(path)
        loaded = load_backtest_report_envelope(path)
        a = cfg["auditor"]["promotion"]
        gate = evaluate_backtest_to_paper_gate(loaded, PromotionThresholds(
            backtest_min_profit_factor=a["backtest_min_profit_factor"],
            backtest_max_drawdown_pct=a["backtest_max_drawdown_pct"],
            backtest_min_trade_count=a["backtest_min_trade_count"],
            backtest_min_profit_factor_excluding_top_5=a["backtest_min_profit_factor_excluding_top_5"],
        ))
        row["gate1_passed"] = gate.passed
        row["gate1_criteria"] = [
            {"name": c.name, "passed": c.passed, "actual": c.actual, "threshold": c.threshold} for c in gate.criteria
        ]
    return row


def mode_anchor(args) -> int:
    for arm in ("C", "A"):
        row = run_arm("y4_VAL_2024-25", arm, persist=False, out_of_sample=False)
        print("ANCHOR " + json.dumps(row, default=str))
    return 0


def mode_run(args) -> int:
    row = run_arm(args.window, args.arm, persist=True, out_of_sample=args.window.startswith("y5"))
    print("RUN " + json.dumps(row, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["fidelity", "anchor", "run"])
    p.add_argument("--window", default="y5_TEST_2025-26", choices=sorted(YEARS))
    p.add_argument("--arm", default="C", choices=["C", "A"])
    args = p.parse_args()
    return {"fidelity": mode_fidelity, "anchor": mode_anchor, "run": mode_run}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
