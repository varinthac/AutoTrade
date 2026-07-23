#!/usr/bin/env python3
"""Run the backtest engine over data/historical/{symbol}_H1.csv and persist
a BacktestReport JSON envelope to data/db/backtest_reports/ -- what Appendix
A §5.2's Backtest -> Paper promotion gate reads (via
autotrade.auditor.backtest_results.load_backtest_report_envelope), not a raw
ClosedTrade list.

    python scripts/run_backtest.py XAUUSD --starting-equity 10000
        --commission-per-lot 0.5 [--slippage-points 5.0]
        [--risk-per-trade-pct 0.5] [--min-lot-risk-cap-pct 1.5] [--out-of-sample]

`--commission-per-lot` is REQUIRED (not optional) -- it must be a conscious,
account-specific choice every run, since `0.0` is a legitimate real value for
a commission-free account (e.g. IC Markets "Standard", which recovers cost
entirely through a wider spread) and NOT a safe silent default: see
`backtest/cost_model.py`'s `CostModelConfig` docstring.

Requires an active MT5 session only to resolve the symbol's `SymbolSpec`
(digits/point/tick_value/... -- same as scripts/download_historical.py); the
replay itself runs entirely offline against the historical CSV.

Risk Voice (`council/risk_voice.py`) is always modeled, using
`config/base.yaml`'s `risk_voice:` thresholds -- see `backtest/engine.py`'s
module docstring for exactly which of its 6 conditions are (and are not,
i.e. news) faithfully replayed here.

Watchman's exit management (breakeven/trail/structure-invalidation/time-stop)
is likewise always modeled, using `config/base.yaml`'s `watchman:` block --
see `backtest/engine.py`'s module docstring for the exact per-bar ordering
convention and the one sub-condition (news protection) still unmodeled.

Shield's duplicate-signal cooldown is likewise always modeled, using
`config/base.yaml`'s `shield:` block -- see `backtest/engine.py`'s module
docstring for exactly which of Shield's 6 rules have real effect in this
single-position engine (only the cooldown does; the other 5 are structurally
inert here).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import BacktestReport, format_report, generate_report
from autotrade.common.config import REPO_ROOT, load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.common.symbols import get_symbol_spec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.shield.checkpoint import ShieldConfig
from autotrade.watchman.evaluate import WatchmanConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "db" / "backtest_reports"


def build_envelope(
    symbol: str,
    df: pd.DataFrame,
    report: BacktestReport,
    cost_model: CostModelConfig,
    starting_equity: float,
    is_out_of_sample: bool,
    risk_voice_modeled: bool,
    watchman_exits_modeled: bool,
    shield_modeled: bool,
    min_lot_risk_cap_pct: float | None,
) -> dict:
    """The JSON-serializable envelope written to disk -- see
    `auditor/backtest_results.py`'s `BacktestReportEnvelope` for the
    corresponding load-side shape/validation. `cost_model_complete` per
    Appendix A §5.2's "backtest ที่ไม่มี cost model = ไม่นับ": true iff
    slippage uses the minimum-1-spread convention (`slippage_points is
    None`, see `CostModelConfig`'s docstring) rather than a possibly-too-
    small override. Commission is NOT checked for `> 0` here -- `0.0` is a
    legitimate real value for a commission-free account (e.g. IC Markets
    "Standard"), not just an unconfigured placeholder, so it can't be used
    to infer completeness. The "did the caller actually decide this"
    safeguard instead lives in `main()`'s CLI: `--commission-per-lot` is a
    required argument, so this function is never reached with an
    un-considered commission value in the normal CLI path.
    `risk_voice_modeled`/`watchman_exits_modeled`/`shield_modeled` mirror
    that same "don't silently count an incomplete simulation" philosophy for
    Risk Voice, Watchman's exit management, and Shield's cooldown
    respectively -- see `backtest/engine.py`'s module docstring.
    `min_lot_risk_cap_pct` records this run's
    `BacktestConfig.min_lot_risk_cap_pct` for auditability (`None` when the
    min-lot risk-cap fallback was disabled) -- see `risk/sizing.py`'s
    `compute_lot_size` docstring.
    """
    cost_model_complete = cost_model.slippage_points is None
    return {
        "symbol": symbol,
        "bar_range": {"start": str(df["time"].iloc[0]), "end": str(df["time"].iloc[-1])},
        "starting_equity": starting_equity,
        "cost_model": asdict(cost_model),
        "cost_model_complete": cost_model_complete,
        "is_out_of_sample": is_out_of_sample,
        "risk_voice_modeled": risk_voice_modeled,
        "watchman_exits_modeled": watchman_exits_modeled,
        "shield_modeled": shield_modeled,
        "min_lot_risk_cap_pct": min_lot_risk_cap_pct,
        "report": asdict(report),
    }


def run_and_persist(
    symbol: str,
    df: pd.DataFrame,
    symbol_spec: SymbolSpec,
    starting_equity: float,
    risk_per_trade_pct: float,
    cost_model: CostModelConfig,
    is_out_of_sample: bool,
    output_dir: Path,
    risk_voice_cfg: RiskVoiceConfig | None = None,
    watchman_cfg: WatchmanConfig | None = None,
    shield_cfg: ShieldConfig | None = None,
    pivot_bars: int = 3,
    min_lot_risk_cap_pct: float | None = None,
) -> Path:
    """`risk_voice_cfg=None`/`watchman_cfg=None`/`shield_cfg=None` (the
    defaults) mean Risk Voice / Watchman's exit management / Shield's
    cooldown are NOT modeled in this run -- an explicit, honest placeholder
    (see `backtest/engine.py`'s module docstring), not a silent equivalent to
    passing one. `scripts/run_backtest.py`'s CLI (`main()`, below) always
    constructs real ones from `config/base.yaml`'s `risk_voice:`/`watchman:`/
    `shield:` blocks; leaving any `None` is only appropriate for
    tests/tooling that don't need that veto/exit/cooldown behavior.
    `pivot_bars` defaults to `BacktestConfig`'s own default (3); `main()`
    always passes `config/base.yaml`'s `global.swing_pivot_bars` instead of
    relying on that default. `min_lot_risk_cap_pct=None` (the default)
    disables `risk/sizing.py`'s min-lot risk-cap fallback -- spec-exact
    behavior; `main()` passes `config/base.yaml`'s `cfo.min_lot_risk_cap_pct`
    (the adopted 1.5 value) unless overridden by `--min-lot-risk-cap-pct`."""
    config = BacktestConfig(
        starting_equity=starting_equity, risk_per_trade_pct=risk_per_trade_pct, cost_model=cost_model,
        risk_voice_cfg=risk_voice_cfg, watchman_cfg=watchman_cfg, shield_cfg=shield_cfg, pivot_bars=pivot_bars,
        min_lot_risk_cap_pct=min_lot_risk_cap_pct,
    )
    trades = run_backtest(df, symbol, symbol_spec, config)
    report = generate_report(trades, starting_equity)
    envelope = build_envelope(
        symbol, df, report, cost_model, starting_equity, is_out_of_sample,
        risk_voice_modeled=risk_voice_cfg is not None,
        watchman_exits_modeled=watchman_cfg is not None,
        shield_modeled=shield_cfg is not None,
        min_lot_risk_cap_pct=min_lot_risk_cap_pct,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"{symbol}_{stamp}.json"
    out_path.write_text(json.dumps(envelope, indent=2, default=str), encoding="utf-8")

    logger.info("Backtest report written to %s", out_path)
    logger.info("%s", format_report(report))
    return out_path


def filter_by_date_range(df: pd.DataFrame, start_date: str | None, end_date: str | None) -> pd.DataFrame:
    """Restricts `df` to bars at/after `start_date` and before `end_date`
    (both `YYYY-MM-DD`, `end_date` exclusive) -- the only way to get a
    genuine held-out out-of-sample slice via this CLI, since
    `HISTORICAL_DIR / f"{symbol}_H1.csv"` otherwise always contains the
    entire downloaded history. Either bound may be omitted. Returns a
    reindexed copy; never mutates `df` in place."""
    if start_date:
        df = df[df["time"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["time"] < pd.Timestamp(end_date)]
    return df.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbol", help="Canonical symbol, e.g. XAUUSD")
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument(
        "--risk-per-trade-pct", type=float, default=None,
        help="Overrides config/base.yaml's cfo.risk_per_trade_pct",
    )
    parser.add_argument(
        "--min-lot-risk-cap-pct", type=float, default=None,
        help="Overrides config/base.yaml's cfo.min_lot_risk_cap_pct",
    )
    parser.add_argument(
        "--commission-per-lot", type=float, required=True,
        help="Currency per 1.0 lot round-trip. REQUIRED -- 0.0 is a legitimate value for a "
             "commission-free account (e.g. IC Markets Standard) but must be consciously chosen, "
             "never a silent default; see CostModelConfig's docstring.",
    )
    parser.add_argument(
        "--slippage-points", type=float, default=None,
        help="Omit to use the bar's own spread (minimum-1-spread convention)",
    )
    parser.add_argument(
        "--out-of-sample", action="store_true",
        help="Mark this run as out-of-sample in the envelope (human-set, not auto-detected)",
    )
    parser.add_argument(
        "--start-date", type=str, default=None,
        help="Restrict the replay to bars at/after this date (YYYY-MM-DD) -- for a genuine held-out "
             "out-of-sample slice, since this CLI otherwise always reads the entire historical CSV",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="Restrict the replay to bars before this date (YYYY-MM-DD, exclusive)",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    csv_path = HISTORICAL_DIR / f"{args.symbol}_H1.csv"
    if not csv_path.exists():
        logger.error("No historical data at %s -- run scripts/download_historical.py first", csv_path)
        return 1
    df = pd.read_csv(csv_path, parse_dates=["time"])
    df = filter_by_date_range(df, args.start_date, args.end_date)
    if df.empty:
        logger.error("No bars remain in %s after applying --start-date/--end-date filters", csv_path)
        return 1

    cfg = load_yaml_config("base")
    risk_per_trade_pct = (
        args.risk_per_trade_pct if args.risk_per_trade_pct is not None else cfg["cfo"]["risk_per_trade_pct"]
    )
    min_lot_risk_cap_pct = (
        args.min_lot_risk_cap_pct
        if args.min_lot_risk_cap_pct is not None
        else cfg["cfo"]["min_lot_risk_cap_pct"]
    )
    # Always model Risk Voice from config/base.yaml's real thresholds -- same
    # "no opt-out" convention scripts/run_shadow_loop.py already uses (unlike
    # commission, which genuinely has no non-placeholder default, every
    # Risk Voice threshold IS already fully specified in config, so there is
    # no honest reason for this CLI's normal path to skip modeling it).
    risk_voice_cfg = RiskVoiceConfig(
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
    # Always model Watchman's exit management from config/base.yaml's real
    # thresholds -- same "no opt-out" convention as risk_voice_cfg above.
    watchman_cfg = WatchmanConfig(
        breakeven_at_r=cfg["watchman"]["breakeven_at_r"],
        trail_start_r=cfg["watchman"]["trail_start_r"],
        trail_distance_atr=cfg["watchman"]["trail_distance_atr"],
        time_stop_hours=cfg["watchman"]["time_stop_hours"],
        dead_trade_r_band=cfg["watchman"]["dead_trade_r_band"],
        breakeven_enabled=cfg["watchman"]["breakeven_enabled"],
        trail_enabled=cfg["watchman"]["trail_enabled"],
    )
    # Always model Shield's cooldown from config/base.yaml's real thresholds
    # -- same "no opt-out" convention as risk_voice_cfg/watchman_cfg above.
    shield_cfg = ShieldConfig(
        min_rr=cfg["shield"]["min_rr"],
        max_correlation=cfg["shield"]["max_correlation"],
        max_positions_per_symbol=cfg["shield"]["max_positions_per_symbol"],
        max_positions_total=cfg["shield"]["max_positions_total"],
        total_risk_ceiling_pct=cfg["shield"]["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=cfg["shield"]["duplicate_signal_cooldown_hours"],
    )

    creds = load_mt5_credentials()
    with mt5_session(creds):
        symbol_spec = get_symbol_spec(args.symbol)

    cost_model = CostModelConfig(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points,
    )

    run_and_persist(
        args.symbol, df, symbol_spec, args.starting_equity, risk_per_trade_pct,
        cost_model, args.out_of_sample, args.output_dir,
        risk_voice_cfg=risk_voice_cfg, watchman_cfg=watchman_cfg, shield_cfg=shield_cfg,
        pivot_bars=cfg["global"]["swing_pivot_bars"],
        min_lot_risk_cap_pct=min_lot_risk_cap_pct,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
