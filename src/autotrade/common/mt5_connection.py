"""Single shared entry point for the MT5 terminal connection.

Per spec.md §2.3: the MetaTrader5 package holds one global terminal
connection and is not thread-safe. Every module that needs MT5 (feed/,
execution/, backtest history download) goes through mt5_session() here
rather than calling mt5.initialize()/mt5.login() itself.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import MetaTrader5 as mt5

from autotrade.common.config import MT5Credentials

logger = logging.getLogger(__name__)


class MT5ConnectionError(RuntimeError):
    pass


@contextmanager
def mt5_session(creds: MT5Credentials) -> Iterator[None]:
    """Initialize the MT5 terminal connection and log in for the duration
    of the `with` block; always shuts down cleanly on exit."""
    init_kwargs = dict(login=creds.login, password=creds.password, server=creds.server)
    if creds.terminal_path:
        init_kwargs["path"] = creds.terminal_path

    if not mt5.initialize(**init_kwargs):
        code, desc = mt5.last_error()
        raise MT5ConnectionError(
            f"MT5 initialize() failed: [{code}] {desc}. "
            "Check that the terminal is installed, the demo account is valid, "
            "and MT5_SERVER in .env exactly matches the server name shown in the terminal."
        )

    account = mt5.account_info()
    if account is None:
        mt5.shutdown()
        code, desc = mt5.last_error()
        raise MT5ConnectionError(f"MT5 login succeeded but account_info() failed: [{code}] {desc}")

    logger.info(
        "MT5 connected: login=%s server=%s balance=%s currency=%s",
        account.login, account.server, account.balance, account.currency,
    )

    try:
        yield
    finally:
        mt5.shutdown()
        logger.info("MT5 connection shut down")
