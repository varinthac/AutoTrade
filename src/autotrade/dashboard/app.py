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
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, redirect, render_template, request, send_file, url_for

from autotrade.auditor.daily_report import build_daily_report
from autotrade.dashboard import views
from autotrade.store import journal
from autotrade.store.models import DEFAULT_PAPER_DB_PATH


def create_app(db_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    resolved_db_path = db_path if db_path is not None else DEFAULT_PAPER_DB_PATH

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
