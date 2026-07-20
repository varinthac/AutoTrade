#!/usr/bin/env python3
"""Phase 3d: continuous demo shadow-running loop -- the full
feed -> council(trivial_signal) -> risk(CFO sizing) -> execution pipeline,
run continuously against the MT5 demo account (spec.md §6 Phase 3d). This is
the Phase-3 stand-in for what will eventually become run_sandbox.py/run_live.py.

Defaults to --adapter noop (dry run, no MT5 orders ever sent). Passing
--adapter demo is what actually places real orders on the demo account and
must be requested explicitly every run, per spec.md §7 fail-safe defaults.

    python scripts/run_shadow_loop.py [--adapter noop|demo] [--seed-bars N]
"""
from __future__ import annotations

import argparse
import logging
import sys

import MetaTrader5 as mt5
import pandas as pd

from autotrade.common.clock import RealClock
from autotrade.common.config import (
    MT5Credentials,
    load_finnhub_api_key,
    load_mt5_credentials,
    load_yaml_config,
)
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.mt5_time import ServerClock
from autotrade.common.symbols import to_broker_name
from autotrade.council.finnhub_news_calendar import FinnhubNewsCalendarProvider
from autotrade.council.mql5_calendar_provider import MQL5CalendarProvider, resolve_commondata_path
from autotrade.council.news_calendar import NewsCalendarProvider, StubNewsCalendarProvider
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.execution.adapter import BrokerAdapter
from autotrade.execution.demo_adapter import ThrottledDemoAdapter
from autotrade.execution.noop_adapter import NoOpBrokerAdapter
from autotrade.feed.poller import TIMEFRAME_MAP
from autotrade.orchestrator.shadow_loop import ShadowLoop, ShadowLoopConfig
from autotrade.risk.circuit_breaker import DEFAULT_STATE_PATH, CircuitBreaker
from autotrade.shield.checkpoint import Shield
from autotrade.watchman.connectivity_watchdog import ConnectivityWatchdog, ConnectivityWatchdogConfig
from autotrade.watchman.evaluate import WatchmanConfig
from autotrade.watchman.loop import WatchmanLoop
from autotrade.watchman.news_protection import NewsProtectionConfig
from autotrade.watchman.position_metadata import DEFAULT_STATE_PATH as DEFAULT_POSITION_METADATA_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SEED_BARS = 200


def seed_history(symbol: str, timeframe: str, bars: int, symbol_map: dict[str, str]) -> pd.DataFrame:
    """Fetch the last `bars` fully-closed bars to seed the rolling window
    build_trade_idea() needs for EMA20/50 and swing detection. Requires an
    active mt5_session(). Position 1 (not 0) skips the still-forming bar,
    same convention as feed/poller.fetch_last_closed_bar."""
    broker_symbol = to_broker_name(symbol, symbol_map)
    mt5_timeframe = TIMEFRAME_MAP[timeframe]
    rates = mt5.copy_rates_from_pos(broker_symbol, mt5_timeframe, 1, bars)
    if rates is None or len(rates) == 0:
        code, desc = mt5.last_error()
        raise RuntimeError(
            f"copy_rates_from_pos({broker_symbol!r}) returned nothing while seeding history: [{code}] {desc}"
        )

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def build_adapter(
    name: str, creds: MT5Credentials, clock: RealClock, order_cfg: dict | None = None,
) -> BrokerAdapter:
    if name == "noop":
        return NoOpBrokerAdapter()
    if name == "demo":
        order_cfg = order_cfg or {}
        return ThrottledDemoAdapter(
            creds, clock,
            max_retries=order_cfg.get("max_retries", 2),
            retry_delay_sec=order_cfg.get("retry_delay_sec", 3),
            max_entry_slippage_atr=order_cfg.get("max_entry_slippage_atr", 0.3),
            min_rr_after_slippage=order_cfg.get("min_rr_after_slippage", 1.3),
        )
    raise ValueError(f"unknown --adapter {name!r}")


def build_news_provider(clock: RealClock) -> NewsCalendarProvider:
    """Provider priority order (highest first) -- must be called from
    inside an active `mt5_session()` for (1) to ever be selected (see
    `main()`'s wiring):

      1. `MQL5CalendarProvider` -- MT5's own free, built-in economic
         calendar, exported to a CSV by `mql5/NewsCalendarExporter.mq5` (an
         MQL5 Service running inside the terminal) and read here via
         `resolve_commondata_path()`. The only genuinely-working candidate
         found so far -- `council/news_calendar.py`'s module docstring has
         the full history of why every paid-API candidate below it was
         rejected. Selected whenever `mt5.terminal_info()` resolves a
         `commondata_path`, i.e. whenever an MT5 session is actually
         active, regardless of whether the exporting Service has been
         started in the terminal yet -- if it hasn't,
         `MQL5CalendarProvider` itself fails safe (returns `None` on every
         call) until it is, per its own module docstring.
      2. `FinnhubNewsCalendarProvider` -- fallback if `FINNHUB_API_KEY` is
         set, though currently gated behind a paid plan (HTTP 403) on
         every key tried so far -- see `finnhub_news_calendar.py`'s module
         docstring.
      3. `StubNewsCalendarProvider` -- always returns `None`; see
         `council/news_calendar.py`'s module docstring for why the stub
         means every trade gets vetoed on the news condition.
    """
    commondata_path = resolve_commondata_path()
    if commondata_path:
        logger.info(
            "Using MQL5CalendarProvider -- MT5 terminal_info() resolved commondata_path=%s "
            "(still fails safe to None on every call until NewsCalendarExporter.mq5's Service "
            "is started in the terminal's Navigator panel).",
            commondata_path,
        )
        return MQL5CalendarProvider(commondata_path, clock=clock)

    api_key = load_finnhub_api_key()
    if api_key:
        logger.info("Using FinnhubNewsCalendarProvider -- FINNHUB_API_KEY is configured.")
        return FinnhubNewsCalendarProvider(api_key, clock=clock)
    logger.warning(
        "MQL5 calendar unavailable (no active MT5 session?) and FINNHUB_API_KEY not set in .env -- "
        "falling back to StubNewsCalendarProvider. Per Risk Voice's fail-safe rule (Appendix A "
        "§1.5), this means EVERY trade will be vetoed on the news condition until a real provider "
        "is available (see council/news_calendar.py)."
    )
    return StubNewsCalendarProvider()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--adapter", choices=["noop", "demo"], default="noop",
        help="noop (default, dry run, no orders sent) or demo (places real orders on the MT5 demo account)",
    )
    parser.add_argument(
        "--seed-bars", type=int, default=DEFAULT_SEED_BARS,
        help=f"Number of closed bars to seed the rolling history with (default {DEFAULT_SEED_BARS})",
    )
    parser.add_argument("--poll-interval-sec", type=float, default=5.0)
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after this many poll iterations (mainly for smoke-testing); default runs forever",
    )
    args = parser.parse_args()

    creds = load_mt5_credentials()
    cfg = load_yaml_config("base")
    symbols = list(cfg["symbols"].keys())
    symbol_map = cfg["symbols"]
    timeframe = cfg["global"]["timeframe"]

    # Adapter throttle timing only needs a monotonically-advancing clock, not
    # server time specifically -- RealClock is fine here and avoids an extra
    # MT5 round-trip per place_order() call.
    adapter_clock = RealClock()
    adapter = build_adapter(args.adapter, creds, adapter_clock, order_cfg=cfg["order"])
    if args.adapter == "noop":
        logger.info("Using NoOpBrokerAdapter -- dry run, no orders will be sent to any broker.")
    else:
        logger.warning("Using ThrottledDemoAdapter -- REAL orders will be sent to the MT5 demo account.")

    # CircuitBreaker/ShadowLoop need MT5 broker server time, not wall-clock
    # UTC -- server_now()'s daily-loss reset boundary is server-day-based
    # (see risk/circuit_breaker.py's module docstring).
    reference_symbol_broker_name = to_broker_name(symbols[0], symbol_map)
    loop_clock = ServerClock(reference_symbol_broker_name)

    circuit_breaker = CircuitBreaker(
        daily_loss_limit_pct=cfg["cfo"]["daily_loss_limit_pct"],
        max_consecutive_losses=cfg["cfo"]["max_consecutive_losses"],
        max_drawdown_halt_pct=cfg["cfo"]["max_drawdown_halt_pct"],
        state_path=DEFAULT_STATE_PATH,
    )
    shield = Shield(
        min_rr=cfg["shield"]["min_rr"],
        max_correlation=cfg["shield"]["max_correlation"],
        max_positions_per_symbol=cfg["shield"]["max_positions_per_symbol"],
        max_positions_total=cfg["shield"]["max_positions_total"],
        total_risk_ceiling_pct=cfg["shield"]["total_risk_ceiling_pct"],
        duplicate_signal_cooldown_hours=cfg["shield"]["duplicate_signal_cooldown_hours"],
    )
    loop_cfg = ShadowLoopConfig(
        risk_per_trade_pct=cfg["cfo"]["risk_per_trade_pct"],
        sl_buffer_atr=cfg["order"]["sl_buffer_atr"],
        sl_min_atr=cfg["order"]["sl_min_atr"],
        sl_max_atr=cfg["order"]["sl_max_atr"],
        tp_r_multiple=cfg["order"]["tp_r_multiple"],
        bull_threshold=cfg["council"]["bull_threshold"],
        bear_threshold=cfg["council"]["bear_threshold"],
        conflict_threshold=cfg["council"]["conflict_threshold"],
    )
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
    # build_news_provider() must run inside an active mt5_session() --
    # MQL5CalendarProvider (its top-priority candidate) needs
    # mt5.terminal_info() to resolve where to read the calendar export from
    # (see build_news_provider's own docstring) -- so this whole tail of
    # main() (news provider, Watchman wiring, seeding, and the run loop
    # itself) now lives inside one `with mt5_session(creds):` block instead
    # of opening a second, separate session further down. mt5_session() is
    # reentrant (common/mt5_connection.py), so this is just a scope widening,
    # not a behavior change for anything that was already inside it.
    with mt5_session(creds):
        # RealClock is fine here too -- the Finnhub provider's cache TTL
        # only needs monotonically-advancing wall-clock time, not server
        # time (same rationale as adapter_clock above).
        news_provider = build_news_provider(adapter_clock)

        watchman_config = WatchmanConfig(
            breakeven_at_r=cfg["watchman"]["breakeven_at_r"],
            trail_start_r=cfg["watchman"]["trail_start_r"],
            trail_distance_atr=cfg["watchman"]["trail_distance_atr"],
            time_stop_hours=cfg["watchman"]["time_stop_hours"],
            dead_trade_r_band=cfg["watchman"]["dead_trade_r_band"],
        )
        news_protection_cfg = NewsProtectionConfig(
            news_window_minutes=cfg["watchman"]["news_window_minutes"],
            profit_threshold_r=cfg["watchman"]["news_profit_threshold_r"],
            close_mode=cfg["watchman"]["news_close_mode"],
        )
        # Connectivity is a wall-clock-elapsed-time question ("how long has
        # it actually been"), not a server-day-boundary one -- RealClock,
        # same rationale as adapter_clock/news_provider's clock above.
        connectivity_watchdog = ConnectivityWatchdog(
            adapter_clock, ConnectivityWatchdogConfig(timeout_minutes=cfg["watchman"]["connectivity_timeout_minutes"]),
        )
        watchman_loop = WatchmanLoop(
            adapter=adapter, watchman_config=watchman_config, news_provider=news_provider,
            news_protection_config=news_protection_cfg, connectivity_watchdog=connectivity_watchdog,
            symbol_map=symbol_map, state_path=DEFAULT_POSITION_METADATA_PATH,
        )

        initial_history = {
            symbol: seed_history(symbol, timeframe, args.seed_bars, symbol_map) for symbol in symbols
        }
        for symbol, df in initial_history.items():
            logger.info(
                "Seeded %s with %d bars (%s -> %s)", symbol, len(df), df["time"].iloc[0], df["time"].iloc[-1],
            )

        shadow_loop = ShadowLoop(
            adapter=adapter, circuit_breaker=circuit_breaker, shield=shield, cfg=loop_cfg,
            initial_history=initial_history, symbol_map=symbol_map, clock=loop_clock,
            news_provider=news_provider, risk_voice_cfg=risk_voice_cfg,
            watchman_loop=watchman_loop, position_metadata_path=DEFAULT_POSITION_METADATA_PATH,
        )
        logger.info(
            "Shadow loop starting: symbols=%s timeframe=%s adapter=%s -- waiting for next bar close",
            symbols, timeframe, args.adapter,
        )
        shadow_loop.run(
            symbols, timeframe, poll_interval_sec=args.poll_interval_sec, max_iterations=args.max_iterations,
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Shadow loop stopped (Ctrl+C).")
        sys.exit(0)
