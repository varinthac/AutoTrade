"""MT5-query logic for currently open positions -- extracted out of
`dashboard/app.py` so it can be shared with `notify/telegram_control.py`'s
`/positions` command without that listener gaining a transitive Flask import
(this module has no Flask import, same "no Flask" property `dashboard/views.py`
already has and telegram_control.py already relies on for `views.to_trade_row`
et al). `dashboard/app.py` imports `get_open_positions_display` from here
rather than duplicating it.

Still the same accepted MT5-touching exception documented in
`dashboard/app.py`'s module docstring (alongside `get_current_server_time()`):
a brief, best-effort MT5 connection for display only, never letting an MT5
failure raise past this function.
"""
from __future__ import annotations

import logging

import MetaTrader5 as mt5

from autotrade.common.config import load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.dashboard import views

logger = logging.getLogger(__name__)

_MT5_TIMEOUT_MS = 3000


def get_open_positions_display() -> list[views.OpenPositionRow] | None:
    """A brief, best-effort MT5 connection for display only -- the same
    accepted exception to the dashboard's otherwise MT5-free design as
    `dashboard/app.py`'s `get_current_server_time()` (that module's own
    docstring), and following its exact philosophy: never let an MT5 failure
    break a caller's render. Returns `None` when MT5 itself is unavailable
    (session/connection raises, or `mt5.positions_get()` itself returns
    `None` on failure) -- distinct from `[]`, which means the connection
    succeeded and there are genuinely zero open positions right now. A caller
    must be able to tell these two cases apart (a `None` "MT5 unreachable"
    must never look like an empty "no trades open").

    Calls `mt5.positions_get()`/`mt5.account_info()` directly rather than
    constructing a full `ThrottledDemoAdapter` (`execution/demo_adapter.py`):
    that class needs a `Clock`, `order_cfg`, `journal_db_path`, etc. this
    read-only display has no other reason to construct, and its own
    `get_open_positions()` bakes in a Shield-specific `risk_pct`
    approximation this display doesn't need.
    """
    try:
        symbol_map = load_yaml_config("base")["symbols"]
        broker_to_canonical = {broker: canonical for canonical, broker in symbol_map.items()}
        creds = load_mt5_credentials()
        with mt5_session(creds, timeout_ms=_MT5_TIMEOUT_MS):
            positions = mt5.positions_get()
            if positions is None:
                return None

            result: list[views.OpenPositionRow] = []
            for pos in positions:
                canonical = broker_to_canonical.get(pos.symbol)
                if canonical is None:
                    logger.warning(
                        "get_open_positions_display(): broker symbol %r has no canonical mapping "
                        "in config/base.yaml symbols -- skipping", pos.symbol,
                    )
                    continue
                direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                result.append(views.to_open_position_row(views.OpenPositionData(
                    ticket=pos.ticket, symbol=canonical, direction=direction, volume=pos.volume,
                    price_open=pos.price_open, price_current=pos.price_current,
                    sl=pos.sl, tp=pos.tp, profit=pos.profit,
                )))
            return views.sort_open_positions(result)
    except Exception:
        logger.warning("get_open_positions_display: could not fetch open positions", exc_info=True)
        return None
