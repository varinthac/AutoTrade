#!/usr/bin/env python3
"""Standalone manual kill-switch (Phase 3c) — per spec.md §7 "Safety Gates":
independent of the main loop's responsiveness, and per
trading_system_summary_v2.md Appendix B §B.4, a single command that both
halts the system AND closes every open position. Talks to MT5 directly via
common/mt5_connection.py -- deliberately does not depend on execution/ (which
may not even be running), so this works even against a hung/crashed
orchestrator.

    python scripts/kill_switch.py --status
    python scripts/kill_switch.py --activate "reason for halting"
    python scripts/kill_switch.py --deactivate --confirm

The halt flag is written FIRST, before any attempt to close positions --
per spec.md §7 fail-safe defaults, a half-finished kill sequence must never
leave the system looking safe to keep trading.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

import MetaTrader5 as mt5

from autotrade.common import kill_switch_flag
from autotrade.common.config import load_mt5_credentials
from autotrade.common.mt5_connection import mt5_session
from autotrade.notify.telegram import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEVIATION_POINTS = 20
MAGIC = 999_999


@dataclass(frozen=True)
class CloseResult:
    ticket: int
    symbol: str
    volume: float
    success: bool
    message: str


def close_all_open_positions() -> list[CloseResult]:
    """Close every open position across every symbol immediately at market.
    Must be called from inside an active mt5_session(). A per-position
    failure is captured in its CloseResult rather than raised, so one bad
    close never aborts attempts on the rest."""
    positions = mt5.positions_get()
    if positions is None:
        code, desc = mt5.last_error()
        raise RuntimeError(f"positions_get() failed: [{code}] {desc}")

    return [_close_position(position) for position in positions]


def _close_position(position) -> CloseResult:
    if position.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
    else:
        order_type = mt5.ORDER_TYPE_BUY

    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None or tick.time == 0:
        code, desc = mt5.last_error()
        message = f"symbol_info_tick({position.symbol!r}) failed: [{code}] {desc}"
        logger.error("FAILED to close ticket=%s symbol=%s: %s", position.ticket, position.symbol, message)
        return CloseResult(position.ticket, position.symbol, position.volume, False, message)

    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": order_type,
        "price": price,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": "autotrade kill_switch close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    send_result = mt5.order_send(request)
    if send_result is None:
        code, desc = mt5.last_error()
        message = f"order_send() returned None: [{code}] {desc}"
        logger.error("FAILED to close ticket=%s symbol=%s: %s", position.ticket, position.symbol, message)
        return CloseResult(position.ticket, position.symbol, position.volume, False, message)

    if send_result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        message = f"order_send rejected: retcode={send_result.retcode} comment={send_result.comment!r}"
        logger.error("FAILED to close ticket=%s symbol=%s: %s", position.ticket, position.symbol, message)
        return CloseResult(position.ticket, position.symbol, position.volume, False, message)

    message = f"closed volume={send_result.volume} at price={send_result.price}"
    logger.info("CLOSED ticket=%s symbol=%s: %s", position.ticket, position.symbol, message)
    return CloseResult(position.ticket, position.symbol, position.volume, True, message)


def do_activate(reason: str) -> int:
    reason = reason.strip()
    if not reason:
        logger.error("--activate requires a non-empty reason")
        return 1

    kill_switch_flag.activate(reason)
    notify(f"[AutoTrade] KILL SWITCH ACTIVATED: {reason}")
    logger.warning("KILL SWITCH ACTIVATED: %s", reason)

    try:
        creds = load_mt5_credentials()
        with mt5_session(creds):
            results = close_all_open_positions()
    except Exception as exc:
        logger.exception(
            "Kill switch: halt flag is set, but connecting to MT5 / closing positions FAILED. "
            "Trading is halted but existing positions may still be open -- check the terminal manually."
        )
        notify(
            f"[AutoTrade] \U0001F6A8 KILL SWITCH: halt flag is set, but closing positions FAILED "
            f"({exc}). Existing positions may still be OPEN -- check the terminal manually."
        )
        return 1

    if not results:
        logger.info("0 positions to close.")
        return 0

    for r in results:
        status = "OK" if r.success else "FAILED"
        logger.info("[%s] ticket=%s symbol=%s volume=%s: %s", status, r.ticket, r.symbol, r.volume, r.message)

    failures = [r for r in results if not r.success]
    if failures:
        logger.error(
            "%d/%d position(s) FAILED to close -- MANUAL INTERVENTION REQUIRED. "
            "Trading is halted (flag set) but not all positions are flat.",
            len(failures), len(results),
        )
        failed_tickets = ", ".join(str(r.ticket) for r in failures)
        notify(
            f"[AutoTrade] \U0001F6A8 KILL SWITCH: {len(failures)}/{len(results)} position(s) FAILED "
            f"to close (tickets: {failed_tickets}) -- MANUAL INTERVENTION REQUIRED. Trading is "
            "halted but not all positions are flat."
        )
        return 1

    logger.info("All %d position(s) closed successfully.", len(results))
    return 0


def do_status() -> int:
    status = kill_switch_flag.get_status()
    if status is None:
        logger.info("Kill switch is NOT active. Trading is not halted.")
        return 0

    logger.warning(
        "Kill switch IS ACTIVE. activated_at=%s reason=%s",
        status.get("activated_at"), status.get("reason"),
    )
    return 0


def do_deactivate(confirm: bool) -> int:
    if not confirm:
        logger.error("--deactivate requires --confirm as well (never lift a halt accidentally).")
        return 1

    status = kill_switch_flag.get_status()
    if status is None:
        logger.info("Kill switch was not active; nothing to do.")
        return 0

    logger.warning(
        "Deactivating kill switch (was activated_at=%s reason=%s). "
        "Review what happened before resuming trading -- never restart just because "
        "you 'want to keep trading'.",
        status.get("activated_at"), status.get("reason"),
    )
    kill_switch_flag.deactivate()
    logger.info("Kill switch deactivated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--activate", metavar="REASON", help="Halt trading and close every open position at market")
    group.add_argument("--status", action="store_true", help="Report whether the kill switch is currently active")
    group.add_argument("--deactivate", action="store_true", help="Clear the halt flag (requires --confirm)")
    parser.add_argument("--confirm", action="store_true", help="Required alongside --deactivate")
    args = parser.parse_args()

    if args.activate is not None:
        return do_activate(args.activate)
    if args.status:
        return do_status()
    return do_deactivate(args.confirm)


if __name__ == "__main__":
    sys.exit(main())
