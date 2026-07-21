#!/usr/bin/env python3
"""The Auditor's CLI (Phase 8b, trading_system_summary_v2.md Appendix A §5):
daily trade-autopsy reports (§5.1), promotion-gate evaluation (§5.2),
demotion-rule evaluation (§5.3), and borderline-case expectancy tracking
(§5.4).

    python scripts/run_auditor.py daily [--date YYYY-MM-DD] [--mode paper|live] [--db-path PATH] [--notify]
    python scripts/run_auditor.py promotion --gate backtest --envelope PATH [--notify]
    python scripts/run_auditor.py promotion --gate paper --envelope PATH --weeks-elapsed 10 [--db-path PATH] [--notify]
    python scripts/run_auditor.py promotion --gate live --months-elapsed 3 [--db-path PATH] [--notify]
    python scripts/run_auditor.py demotion --envelope PATH [--as-of-date YYYY-MM-DD] [--mode live] [--db-path PATH] [--notify]
    python scripts/run_auditor.py borderline [--log-path PATH] --commission-per-lot 0.0

`--notify` (daily/promotion/demotion only) sends a Telegram message
(notify/telegram.py) -- `daily --notify` de-dupes on the report's own
`server_date` via a small state file (`data/db/notify_last_daily.json`) so a
double-invocation can't double-send; `promotion --notify`/`demotion --notify`
only send when the evaluated result differs from the last-known one
(notify/gate_state.py). This CLI does not build or run a scheduler itself --
`daily --notify` is meant to be invoked once per day, comfortably after the
broker server-day rollover, by an OS-level scheduled task, e.g. on Windows:

    schtasks /Create /TN "AutoTrade Daily Report" /SC DAILY /ST 00:15 ^
        /TR "C:/path/to/.venv/Scripts/python.exe C:/path/to/scripts/run_auditor.py daily --mode live --notify"

`--date`/`--as-of-date` default to the current MT5 broker SERVER date
(`common/mt5_time.server_now()`, same convention this codebase uses
everywhere else -- never local/UTC wall-clock, per Appendix A §0) --
resolving that default briefly opens an MT5 session using the first symbol
in `config/base.yaml`'s `symbols:` map as the reference. Pass `--date`/
`--as-of-date` explicitly to skip that MT5 round-trip entirely.

**Known operational caveat (not fixed by this CLI):** `promotion`/
`demotion`'s paper/live gates scan the ENTIRE paper/live DB file with no way
to scope to a specific live_ramp "attempt" -- after a demote-then-retry, old
resolved incidents (e.g. a since-cleared drawdown_halt) stay in the history
forever unless the DB file is manually archived/rotated. Out of scope here;
a human operator must currently do that archival by hand.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from autotrade.auditor.backtest_results import BacktestReportEnvelopeError, load_backtest_report_envelope
from autotrade.auditor.borderline import build_borderline_expectancy_report, load_borderline_cases
from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.auditor.demotion import DemotionThresholds, evaluate_demotion
from autotrade.auditor.metrics import compute_trade_metrics
from autotrade.auditor.promotion import (
    PromotionThresholds,
    evaluate_backtest_to_paper_gate,
    evaluate_live_ramp_to_full_gate,
    evaluate_paper_to_live_gate,
)
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.common.config import REPO_ROOT, load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.mt5_time import server_now
from autotrade.common.symbols import get_symbol_spec, to_broker_name
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.notify import gate_state
from autotrade.notify.telegram import notify
from autotrade.orchestrator.shadow_loop import DEFAULT_BORDERLINE_LOG_PATH
from autotrade.risk.circuit_breaker import HEAVY_CB_MARKER
from autotrade.store import journal
from autotrade.store.models import DEFAULT_DB_PATH, DEFAULT_LIVE_DB_PATH, DEFAULT_PAPER_DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Wide-but-finite bounds for "all history" range queries -- safer across
# SQLite/SQLAlchemy datetime handling than datetime.min/datetime.max.
_EPOCH = datetime(2000, 1, 1)
_FAR_FUTURE = datetime(2100, 1, 1)

_MODE_DB_PATHS = {"paper": DEFAULT_PAPER_DB_PATH, "live": DEFAULT_LIVE_DB_PATH}

DEFAULT_NOTIFY_DAILY_STATE_PATH = REPO_ROOT / "data" / "db" / "notify_last_daily.json"


def _resolve_db_path(mode: str | None, db_path: Path | None) -> Path | None:
    if db_path is not None:
        return db_path
    if mode is not None:
        return _MODE_DB_PATHS[mode]
    return DEFAULT_DB_PATH


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _server_today(cfg: dict) -> date:
    """Current MT5 broker server date -- the default for `--date`/
    `--as-of-date` when not given explicitly. This codebase never mixes
    server time with local/UTC wall-clock (Appendix A §0), so even a
    reporting-only CLI must not fall back to `date.today()`'s local
    calendar day; only an explicit `--date`/`--as-of-date` skips this MT5
    round-trip."""
    reference_symbol = next(iter(cfg["symbols"]))
    broker_name = to_broker_name(reference_symbol, cfg["symbols"])
    creds = load_mt5_credentials()
    with mt5_session(creds):
        return server_now(broker_name).date()


def _load_last_notified_daily_date(path: Path) -> str | None:
    """The server date (ISO string) `daily --notify` last actually sent, or
    `None` if never sent / the state file is missing/corrupt -- treated as
    "never sent" rather than an error, same fail-open convention as
    `notify/gate_state.py`'s state file."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("server_date")
    except (json.JSONDecodeError, OSError):
        return None


def _save_last_notified_daily_date(server_date: date, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server_date": server_date.isoformat()}), encoding="utf-8")


def _print_gate_result(title: str, result) -> None:
    print(f"=== {title} ===")
    print(f"passed: {result.passed}")
    for c in result.criteria:
        status = "PASS" if c.passed else ("N/A " if c.passed is None else "FAIL")
        note = f" -- {c.note}" if c.note else ""
        print(f"  [{status}] {c.name}: actual={c.actual} threshold={c.threshold}{note}")
    if result.recommendation:
        print(f"recommendation: {result.recommendation}")


def cmd_daily(args: argparse.Namespace) -> int:
    cfg = load_yaml_config(args.config)
    server_date = _parse_date(args.date) if args.date else _server_today(cfg)
    db_path = _resolve_db_path(args.mode, args.db_path)
    report = build_daily_report(server_date, db_path=db_path)
    print(format_daily_report(report))

    if getattr(args, "notify", False):
        last_sent = _load_last_notified_daily_date(DEFAULT_NOTIFY_DAILY_STATE_PATH)
        if last_sent == server_date.isoformat():
            logger.info("daily --notify: already sent for server_date=%s -- skipping", server_date)
        elif notify(format_daily_report(report)):
            _save_last_notified_daily_date(server_date, DEFAULT_NOTIFY_DAILY_STATE_PATH)
        else:
            logger.warning(
                "daily --notify: Telegram send failed for server_date=%s -- NOT marking it sent, so "
                "this is re-attempted on the next run instead of being lost.", server_date,
            )

    return 0


def _load_envelope_or_error(path: Path):
    try:
        return load_backtest_report_envelope(path)
    except BacktestReportEnvelopeError as exc:
        logger.error("Could not load backtest report envelope: %s", exc)
        return None


def _notify_promotion_gate_if_changed(args: argparse.Namespace, gate: str, passed: bool) -> None:
    if not getattr(args, "notify", False):
        return
    changed, description = gate_state.check_promotion_gate_changed(gate, passed)
    if not changed:
        logger.info("promotion --notify: gate=%s unchanged (passed=%s) -- skipping", gate, passed)
        return
    if notify(f"[AutoTrade] {description}"):
        gate_state.record_promotion_gate(gate, passed)
    else:
        logger.warning(
            "promotion --notify: gate=%s changed but the Telegram send failed -- NOT persisting the "
            "new state, so this change is re-attempted on the next run instead of being lost.", gate,
        )


def cmd_promotion(args: argparse.Namespace) -> int:
    cfg = load_yaml_config(args.config)
    thresholds = PromotionThresholds(**cfg["auditor"]["promotion"])

    if args.gate == "backtest":
        if args.envelope is None:
            logger.error("--envelope is required for --gate backtest")
            return 1
        envelope = _load_envelope_or_error(args.envelope)
        if envelope is None:
            return 1
        result = evaluate_backtest_to_paper_gate(envelope, thresholds)
        _print_gate_result("Backtest -> Paper", result)
        _notify_promotion_gate_if_changed(args, "backtest", result.passed)
        return 0

    if args.gate == "paper":
        if args.envelope is None or args.weeks_elapsed is None:
            logger.error("--envelope and --weeks-elapsed are required for --gate paper")
            return 1
        envelope = _load_envelope_or_error(args.envelope)
        if envelope is None:
            return 1
        db_path = args.db_path or DEFAULT_PAPER_DB_PATH
        paper_trades = journal.get_trades_in_range(_EPOCH, _FAR_FUTURE, db_path=db_path)
        paper_metrics = compute_trade_metrics(paper_trades, starting_equity=envelope.starting_equity)
        result = evaluate_paper_to_live_gate(
            paper_metrics, envelope.report, weeks_elapsed=args.weeks_elapsed,
            trade_count=paper_metrics.trade_count, thresholds=thresholds,
        )
        _print_gate_result("Paper -> Live ramp", result)
        _notify_promotion_gate_if_changed(args, "paper", result.passed)
        return 0

    # args.gate == "live"
    if args.months_elapsed is None:
        logger.error("--months-elapsed is required for --gate live")
        return 1
    db_path = args.db_path or DEFAULT_LIVE_DB_PATH
    live_trades = journal.get_trades_in_range(_EPOCH, _FAR_FUTURE, db_path=db_path)
    live_metrics = compute_trade_metrics(live_trades, starting_equity=args.starting_equity)
    anomaly_events = journal.get_anomaly_events_in_range(_EPOCH, _FAR_FUTURE, db_path=db_path)
    # Only a drawdown_halt counts as "heavy" (Appendix A §5.2, resolved
    # interpretation) -- risk/circuit_breaker.py's HEAVY_CB_MARKER is the
    # shared substring its record_equity() writes into that specific
    # trigger's details, distinct from the "daily_loss_halt:"/
    # "consecutive_loss_halt:"/"downgrade_to_paper:" prefixes its other
    # gates use. A substring match is the only way to tell them apart today
    # since AnomalyEventRecord.event_type is generically
    # "circuit_breaker_trigger" for all of them -- importing the constant
    # (rather than re-hardcoding the string here) keeps this in sync if
    # circuit_breaker.py's wording ever changes.
    heavy_cb_triggered = any(
        e.event_type == "circuit_breaker_trigger" and HEAVY_CB_MARKER in e.details for e in anomaly_events
    )
    result = evaluate_live_ramp_to_full_gate(
        live_metrics, months_elapsed=args.months_elapsed, heavy_cb_triggered=heavy_cb_triggered,
        thresholds=thresholds,
    )
    _print_gate_result("Live ramp -> Full size", result)
    _notify_promotion_gate_if_changed(args, "live", result.passed)
    return 0


def cmd_demotion(args: argparse.Namespace) -> int:
    cfg = load_yaml_config(args.config)
    thresholds = DemotionThresholds(**cfg["auditor"]["demotion"])

    envelope = _load_envelope_or_error(args.envelope)
    if envelope is None:
        return 1

    as_of = _parse_date(args.as_of_date) if args.as_of_date else _server_today(cfg)
    db_path = _resolve_db_path(args.mode, args.db_path)
    range_end = datetime.combine(as_of + timedelta(days=1), datetime.min.time())
    live_trades = journal.get_trades_in_range(_EPOCH, range_end, db_path=db_path)

    result = evaluate_demotion(live_trades, envelope.report, as_of, thresholds)

    print(f"action: {result.action}")
    if result.reasons:
        for reason in result.reasons:
            print(f"  - {reason}")
    else:
        print("  (no demotion conditions matched)")

    if getattr(args, "notify", False):
        changed, description = gate_state.check_demotion_changed(result.action)
        if not changed:
            logger.info("demotion --notify: action unchanged (%s) -- skipping", result.action)
        else:
            message = f"[AutoTrade] {description}"
            if result.reasons:
                message += ": " + "; ".join(result.reasons)
            if notify(message):
                gate_state.record_demotion(result.action)
            else:
                logger.warning(
                    "demotion --notify: action changed but the Telegram send failed -- NOT "
                    "persisting the new state, so this change is re-attempted on the next run "
                    "instead of being lost."
                )

    return 0


def cmd_borderline(args: argparse.Namespace) -> int:
    cfg = load_yaml_config(args.config)
    log_path = args.log_path or DEFAULT_BORDERLINE_LOG_PATH
    cases = load_borderline_cases(log_path)
    if not cases:
        print(f"No borderline cases logged yet at {log_path}.")
        return 0

    symbols = sorted({c["symbol"] for c in cases if "symbol" in c})
    price_data_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        csv_path = HISTORICAL_DIR / f"{symbol}_H1.csv"
        if csv_path.exists():
            price_data_by_symbol[symbol] = pd.read_csv(csv_path, parse_dates=["time"])
        else:
            logger.warning(
                "No historical data for %s at %s -- its borderline cases will be skipped", symbol, csv_path,
            )

    symbol_specs = {}
    if price_data_by_symbol:
        creds = load_mt5_credentials()
        with mt5_session(creds):
            for symbol in price_data_by_symbol:
                symbol_specs[symbol] = get_symbol_spec(symbol)

    # H1 bars trade 1 bar/hour (config/base.yaml's global.timeframe is fixed
    # H1 system-wide) -- so watchman.time_stop_hours converts directly to a
    # bar count without a separate bars-per-hour constant.
    time_stop_bars = int(cfg["watchman"]["time_stop_hours"])
    borderline_cfg = cfg["auditor"]["borderline"]
    cost_model = CostModelConfig(commission_per_lot=args.commission_per_lot)

    report = build_borderline_expectancy_report(
        cases, price_data_by_symbol, symbol_specs, cost_model, time_stop_bars,
        min_cases_for_signal=borderline_cfg["min_cases_for_signal"],
        min_avg_r_for_signal=borderline_cfg["min_avg_r_for_signal"],
    )

    print(f"Replayed: {report.replayed_count}   Unresolved: {report.unresolved_count}")
    print(f"TP: {report.tp_count}   SL: {report.sl_count}   Time-stop: {report.time_stop_count}")
    avg_net_r = f"{report.avg_net_r:.3f}" if report.avg_net_r is not None else "n/a"
    print(f"Avg net R: {avg_net_r}")
    print(f"Meets AI-consideration signal: {report.meets_ai_consideration_signal}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="base", help="config/<name>.yaml to load (default: base)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily_parser = subparsers.add_parser("daily", help="Appendix A §5.1 daily trade-autopsy report")
    daily_parser.add_argument("--date", help="YYYY-MM-DD server date (default: current MT5 server date)")
    daily_parser.add_argument("--mode", choices=["paper", "live"], default=None)
    daily_parser.add_argument("--db-path", type=Path, default=None)
    daily_parser.add_argument(
        "--notify", action="store_true",
        help="Send the report via Telegram, de-duped per server_date (see module docstring)",
    )
    daily_parser.set_defaults(func=cmd_daily)

    promotion_parser = subparsers.add_parser("promotion", help="Appendix A §5.2 promotion gates")
    promotion_parser.add_argument("--gate", choices=["backtest", "paper", "live"], required=True)
    promotion_parser.add_argument(
        "--envelope", type=Path, default=None,
        help="Backtest report envelope JSON (required for --gate backtest/paper)",
    )
    promotion_parser.add_argument("--weeks-elapsed", type=int, default=None, help="Required for --gate paper")
    promotion_parser.add_argument("--months-elapsed", type=int, default=None, help="Required for --gate live")
    promotion_parser.add_argument(
        "--starting-equity", type=float, default=10_000.0, help="For --gate live's drawdown calc",
    )
    promotion_parser.add_argument("--db-path", type=Path, default=None)
    promotion_parser.add_argument(
        "--notify", action="store_true",
        help="Send a Telegram message only if this gate's passed/failed result changed since last run",
    )
    promotion_parser.set_defaults(func=cmd_promotion)

    demotion_parser = subparsers.add_parser("demotion", help="Appendix A §5.3 demotion rules")
    demotion_parser.add_argument(
        "--envelope", type=Path, required=True, help="Backtest report envelope JSON (comparison baseline)",
    )
    demotion_parser.add_argument("--as-of-date", help="YYYY-MM-DD server date (default: current MT5 server date)")
    demotion_parser.add_argument("--mode", choices=["paper", "live"], default="live")
    demotion_parser.add_argument("--db-path", type=Path, default=None)
    demotion_parser.add_argument(
        "--notify", action="store_true",
        help="Send a Telegram message only if the demotion action changed since last run",
    )
    demotion_parser.set_defaults(func=cmd_demotion)

    borderline_parser = subparsers.add_parser("borderline", help="Appendix A §5.4 borderline-case expectancy")
    borderline_parser.add_argument("--log-path", type=Path, default=None)
    borderline_parser.add_argument(
        "--commission-per-lot", type=float, required=True,
        help="Currency per 1.0 lot round-trip. REQUIRED -- 0.0 is a legitimate value for a "
             "commission-free account (e.g. IC Markets Standard) but must be consciously chosen, "
             "never a silent default; see backtest/cost_model.py's CostModelConfig docstring.",
    )
    borderline_parser.set_defaults(func=cmd_borderline)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
