"""Thin Flask shell for the read-only trade dashboard -- route handlers wire
`dashboard/views.py`'s pure logic to `store/journal.py`'s existing read
functions and `auditor/daily_report.py`'s `build_daily_report`; no business
logic lives here. Same framework-shell-vs-pure-logic split as
`scripts/autotrade_gui.py` vs. `gui/control.py`/`gui/env_file.py`.

Entirely read-only: nothing in this whole `dashboard/` package ever calls
`session.add`/`.commit()` or otherwise writes -- safe to run continuously
alongside a live trading loop, and never exposed off `127.0.0.1` (see
`scripts/run_dashboard.py`).
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, redirect, render_template, request, send_file, url_for

from autotrade.auditor.daily_report import build_daily_report
from autotrade.common.config import load_mt5_credentials, load_yaml_config
from autotrade.common.mt5_connection import mt5_session
from autotrade.common.mt5_time import server_now
from autotrade.common.symbols import to_broker_name
from autotrade.dashboard import views
from autotrade.store import journal
from autotrade.store.models import DEFAULT_PAPER_DB_PATH

logger = logging.getLogger(__name__)

_SERVER_TIME_MT5_TIMEOUT_MS = 3000


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


def create_app(db_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    resolved_db_path = db_path if db_path is not None else DEFAULT_PAPER_DB_PATH

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

        return render_template(
            "trades.html",
            rows=rows,
            page=page,
            has_prev=page > 1,
            has_next=page * views.TRADES_PER_PAGE < len(all_trades),
            total_count=len(all_trades),
            start_param=start_param,
            end_param=end_param,
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
