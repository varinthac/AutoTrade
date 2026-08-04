"""Thin Flask shell for the read-only trade dashboard -- route handlers wire
`dashboard/views.py`'s pure logic to `store/journal.py`'s existing read
functions and `auditor/daily_report.py`'s `build_daily_report`; no business
logic lives here. Same framework-shell-vs-pure-logic split as
`scripts/autotrade_gui.py` vs. `gui/control.py`/`gui/env_file.py`.

Entirely read-only: nothing in this whole `dashboard/` package ever calls
`session.add`/`.commit()` or otherwise writes -- safe to run continuously
alongside a live trading loop, and never exposed off `127.0.0.1` (see
`scripts/run_dashboard.py`). Two accepted MT5-touching exceptions to this
package's otherwise MT5-free design, both display-only: `get_current_server_time()`
(current broker server time) and `dashboard/positions.py`'s
`get_open_positions_display()` (currently open positions, re-exported here so
existing callers/imports of this module are unaffected) -- see each
function's own docstring.
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import MetaTrader5 as mt5
import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for

from autotrade.auditor.daily_report import build_daily_report
from autotrade.common.config import load_mt5_credentials, load_telegram_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.mt5_time import server_now
from autotrade.common.symbols import to_broker_name
from autotrade.dashboard import views, webapp_auth
from autotrade.dashboard.positions import get_open_positions_display
from autotrade.store import journal
from autotrade.store.models import DEFAULT_PAPER_DB_PATH

logger = logging.getLogger(__name__)

_SERVER_TIME_MT5_TIMEOUT_MS = 3000

# Session key holding the authenticated operator's Telegram user id, and the
# two places `initData` can arrive from (a header, for a future JS `fetch()`
# call, or a query param, for the one-time redirect `templates/
# webapp_unauthorized.html`'s bootstrap script performs -- see
# `create_app()`'s `before_request` hook below for why both exist).
SESSION_USER_ID_KEY = "webapp_user_id"
_INIT_DATA_HEADER = "X-Telegram-Init-Data"
_INIT_DATA_PARAM = "initData"


def get_current_server_time() -> datetime | None:
    """A brief, best-effort MT5 connection for display only -- this
    dashboard is otherwise deliberately MT5-free (module docstring) so it
    keeps working even when the MT5 terminal isn't running; this is the one
    exception, and must never let an MT5 failure break a page render. Same
    reference-symbol convention `scripts/run_shadow_loop.py`'s
    `reference_symbol_broker_name` uses (first configured symbol).

    This opens its own MT5 connection from the dashboard's own OS process --
    `mt5_session()`'s reentrancy guard (`common/mt5_connection.py`) only
    dedupes nested/sequential calls WITHIN one process, so it provides no
    synchronization at all with the live trading loop's own long-lived
    `mt5_session()` (e.g. `scripts/run_shadow_loop.py`), which runs as a
    separate process. A clean failure here is already handled by the
    broad except below (degrades to "(unavailable)"); genuine interference
    with the live loop's own session is a known, accepted risk at this
    project's current single-operator scale, not something this function
    attempts to prevent.

    Opens a fresh connection per request rather than caching a background
    value: a cached value could silently go stale (e.g. keep showing a
    server time from before the terminal was closed) -- staleness would
    defeat the whole point of a "current" time display. A short timeout is
    passed to mt5.initialize() (unlike every other mt5_session() caller,
    which keeps the package's own default wait) so a dashboard page load
    stays responsive even if the terminal is running but unresponsive."""
    try:
        symbol_map = load_yaml_config("base")["symbols"]
        symbols = list(symbol_map.keys())
        if not symbols:
            return None
        reference_symbol_broker_name = to_broker_name(symbols[0], symbol_map)
        creds = load_mt5_credentials()
        with mt5_session(creds, timeout_ms=_SERVER_TIME_MT5_TIMEOUT_MS):
            return server_now(reference_symbol_broker_name)
    except Exception:
        logger.warning("get_current_server_time: could not fetch MT5 server time", exc_info=True)
        return None


def create_app(db_path: Path | None = None, on_request: Callable[[], None] | None = None) -> Flask:
    app = Flask(__name__)
    resolved_db_path = db_path if db_path is not None else DEFAULT_PAPER_DB_PATH

    if on_request is not None:
        # `scripts/run_dashboard.py`'s idle-TTL auto-shutdown watchdog hooks
        # in here -- every inbound request, authorized or not, counts as
        # activity, matching "any HTTP hit keeps it alive" rather than only
        # successfully-authenticated ones. Registered before the auth hook
        # below so it still fires on a request that `abort(401)`s.
        @app.before_request
        def _touch_activity():
            on_request()

    telegram_creds = load_telegram_credentials()
    if telegram_creds is not None:
        bot_token, _configured_chat_id = telegram_creds
        # Deterministic, not `secrets.token_bytes()`: a random key generated
        # fresh on every process start would invalidate every operator's
        # signed session cookie on each dashboard restart, forcing a fresh
        # `initData` round-trip from the Telegram Mini App every time.
        # Deriving from the bot token (itself a protected `.env` secret,
        # never logged) is a real per-deployment secret, not Flask's shared
        # insecure default dev key, while staying stable across restarts.
        app.secret_key = hashlib.sha256(f"autotrade-dashboard-webapp-session:{bot_token}".encode("utf-8")).digest()

    @app.before_request
    def _require_telegram_webapp_auth():
        # Auth is opt-in, the same "unconfigured == feature doesn't exist
        # yet" pattern every optional integration in common/config.py
        # already follows: the Web App button (notify/telegram_control.py)
        # can't exist without TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured
        # in the first place, so there is no way to reach this dashboard
        # through Telegram while they're unset -- gating in that case would
        # only break this package's pre-existing local-only (127.0.0.1, no
        # auth) workflow this whole test suite exercises, for no security
        # benefit.
        if telegram_creds is None:
            return None
        if session.get(SESSION_USER_ID_KEY) is not None:
            return None

        bot_token, configured_chat_id = telegram_creds
        init_data = request.headers.get(_INIT_DATA_HEADER) or request.args.get(_INIT_DATA_PARAM, "")
        parsed = webapp_auth.verify_init_data(init_data, bot_token)
        if parsed is None or not webapp_auth.is_operator(parsed, configured_chat_id):
            abort(401)

        session[SESSION_USER_ID_KEY] = parsed["user"]["id"]
        return None

    @app.errorhandler(401)
    def _webapp_auth_required(_error):
        # Telegram never attaches initData to the page URL automatically --
        # only `window.Telegram.WebApp.initData`, populated client-side once
        # telegram-web-app.js runs, has it. This page's own script reads it
        # and redirects once with it attached as a query param, letting
        # _require_telegram_webapp_auth() above verify it and establish the
        # session; a plain browser outside Telegram (no window.Telegram)
        # just sees the fallback message instead of any real dashboard data.
        return render_template("webapp_unauthorized.html"), 401

    @app.context_processor
    def inject_current_server_time():
        server_time = get_current_server_time()
        return {"current_server_time": views.format_server_time(server_time) if server_time is not None else None}

    @app.route("/")
    def index():
        return redirect(url_for("trades"))

    @app.route("/trades")
    def trades():
        start_param = request.args.get("start", "")
        end_param = request.args.get("end", "")
        start = views.parse_date_param(start_param) or views.EPOCH
        parsed_end = views.parse_date_param(end_param)
        # Date pickers are inclusive from the user's perspective; get_trades_in_range's
        # end bound is exclusive, so an explicit end date must advance by a day here.
        end = parsed_end + timedelta(days=1) if parsed_end is not None else views.FAR_FUTURE
        # Clamped here (not just inside views.paginate) so the clamped value
        # -- not a raw/negative one a user typed into the URL -- is what
        # has_prev/has_next/the rendered "Page N" label all agree on.
        page = max(request.args.get("page", default=1, type=int) or 1, 1)

        all_trades = views.newest_first(journal.get_trades_in_range(start, end, db_path=resolved_db_path))
        rows = [views.to_trade_row(t) for t in views.paginate(all_trades, page)]
        open_positions = get_open_positions_display()

        return render_template(
            "trades.html",
            rows=rows,
            page=page,
            has_prev=page > 1,
            has_next=page * views.TRADES_PER_PAGE < len(all_trades),
            total_count=len(all_trades),
            start_param=start_param,
            end_param=end_param,
            open_positions=open_positions,
        )

    @app.route("/trades/export")
    def trades_export():
        start_param = request.args.get("start", "")
        end_param = request.args.get("end", "")
        start = views.parse_date_param(start_param) or views.EPOCH
        parsed_end = views.parse_date_param(end_param)
        # Date pickers are inclusive from the user's perspective; get_trades_in_range's
        # end bound is exclusive, so an explicit end date must advance by a day here.
        end = parsed_end + timedelta(days=1) if parsed_end is not None else views.FAR_FUTURE

        all_trades = journal.get_trades_in_range(start, end, db_path=resolved_db_path)
        df = pd.DataFrame(views.trades_to_export_rows(all_trades), columns=views.EXPORT_COLUMNS)

        # In-memory buffer, never a temp file -- this route must stay a pure
        # request/response cycle with no filesystem side effect.
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, engine="openpyxl")
        buffer.seek(0)

        # Filename date is cosmetic, not trade data -- unlike every other
        # date/time in this package, `date.today()` here is fine.
        filename = f"autotrade_trades_{date.today().isoformat()}.xlsx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/daily")
    def daily():
        date_param = views.parse_date_param(request.args.get("date"))
        used_default = date_param is None

        if date_param is not None:
            server_date = date_param.date()
        else:
            all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=resolved_db_path)
            server_date = views.default_daily_date(all_trades)
            if server_date is None:
                return render_template("daily.html", report=None)

        report = build_daily_report(server_date, db_path=resolved_db_path)
        return render_template("daily.html", report=report, used_default=used_default)

    return app
