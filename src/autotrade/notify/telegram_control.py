"""Pure dispatch logic for Telegram inbound control commands (start/stop/
status/emergency-stop) -- no network I/O here, see
scripts/run_telegram_control.py for the polling/`urllib` boundary. Kept
separate so the authorization/confirmation/dispatch logic is unit-testable
without mocking `urllib` at all.

/positions is this module's one accepted exception to being otherwise
MT5-free (same "kept here so this listener stays MT5-free" reasoning
_handle_daily() applies to daily-report date derivation): it calls
`dashboard/positions.py`'s `get_open_positions_display()`, a Flask-free MT5
query shared with `dashboard/app.py`'s `/trades` page, rather than
duplicating the mt5.positions_get() + symbol-mapping + None-vs-empty-list
logic here or importing `dashboard/app.py` itself (which would pull in a
transitive Flask dependency this listener has no other reason to need).

`handle_update()` returns a `ControlReply` (text + optional chart `photos`),
not a plain `str`, so /daily's equity-curve/daily-P&L chart PNGs
(notify/charts.py -- pure PNG-bytes rendering, still no network I/O) can
travel alongside its text report through the same single reply shape every
other command already uses (with an empty `photos` list).
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.common.clock import Clock
from autotrade.dashboard import views
from autotrade.dashboard.positions import get_open_positions_display
from autotrade.gui import control as gui_control
from autotrade.notify import charts
from autotrade.store import journal
from autotrade.store.models import DEFAULT_PAPER_DB_PATH

logger = logging.getLogger(__name__)

UNKNOWN_COMMAND = "unknown"

_CONFIRMATION_WINDOW_SEC = 60
_STDERR_TRUNCATE_CHARS = 300

# Telegram messages have a real length limit, so /trades can't dump an
# unbounded trade history -- same "cap it" reasoning as dashboard/views.py's
# own pagination, just a smaller fixed count instead of a page param.
_TRADES_REPLY_LIMIT = 10

_COMMANDS = {"/start", "/stop", "/status", "/emergency_stop", "/trades", "/positions", "/daily", "/help"}

_USAGE_TEXT = (
    "AutoTrade control commands:\n"
    "/start - launch the shadow loop\n"
    "/stop - request a graceful stop\n"
    "/status - report loop/kill-switch/stop-flag state\n"
    "/emergency_stop - halt trading AND close every open position at market (requires confirmation)\n"
    "/trades - most recent 10 trades (paper mode)\n"
    "/positions - currently open positions\n"
    "/daily - daily trade-autopsy report for the most recent recorded day"
)


@dataclass(frozen=True)
class ChartPhoto:
    png: bytes
    caption: str


@dataclass(frozen=True)
class ControlReply:
    """`handle_update()`'s return type -- `text` alone for every command
    except `/daily`, which additionally carries `photos` (the equity-curve
    and daily-P/L chart PNGs, see notify/charts.py) so
    scripts/run_telegram_control.py's poll loop has exactly ONE reply shape
    to send instead of branching on which command produced it. A list
    (rather than a single `photo_png`) because /daily always produces TWO
    distinct charts, not one; captions are per-photo since each chart needs
    its own short label, not one caption shared across both."""

    text: str
    photos: list[ChartPhoto] = field(default_factory=list)


def is_authorized(update: dict, configured_chat_id: str) -> bool:
    try:
        sender_chat_id = update["message"]["chat"]["id"]
    except (KeyError, TypeError):
        return False
    return str(sender_chat_id) == str(configured_chat_id)


def has_text_message(update: dict) -> bool:
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    return isinstance(message.get("text"), str)


def parse_command(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return UNKNOWN_COMMAND

    token = stripped.split()[0].lower()
    token = token.split("@", 1)[0]
    return token if token in _COMMANDS else UNKNOWN_COMMAND


def _looks_like_confirmation_attempt(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) == 4 and stripped.isdigit()


@dataclass
class PendingConfirmation:
    """Holds at most one pending `/emergency_stop` confirmation. The code is
    random-per-request (not a fixed phrase) so a stray/replayed message can
    never accidentally trigger a real emergency stop, and state lives only
    in memory (never persisted to disk) so a process restart can't leave a
    dangling pending code the operator no longer remembers issuing."""

    code: str | None = None
    expires_at: datetime | None = None

    def request(self, clock: Clock) -> str:
        # secrets.randbelow (not random.randint) even though the real
        # security here is chat-id gating + single-guess-then-clear, not
        # code unpredictability -- CSPRNG-backed generation is free, so
        # there's no reason to leave a non-cryptographic RNG in a
        # security-adjacent code path.
        code = f"{secrets.randbelow(10000):04d}"
        self.code = code
        self.expires_at = clock.now() + timedelta(seconds=_CONFIRMATION_WINDOW_SEC)
        return code

    def confirm(self, text: str, clock: Clock) -> bool:
        matched = (
            self.code is not None
            and self.expires_at is not None
            and clock.now() <= self.expires_at
            and text.strip() == self.code
        )
        self.clear()
        return matched

    def clear(self) -> None:
        self.code = None
        self.expires_at = None


def _format_result(result, success_text: str) -> str:
    if result.returncode == 0:
        return success_text
    stderr = (result.stderr or "").strip()
    if len(stderr) > _STDERR_TRUNCATE_CHARS:
        stderr = stderr[:_STDERR_TRUNCATE_CHARS] + "..."
    return f"Failed (exit code {result.returncode}): {stderr}"


def _run_gui_action(command: str, action, success_text: str) -> str:
    # Distinct from _format_result's "ran but returned non-zero exit code"
    # path: an unexpected exception (e.g. a bad interpreter path) here means
    # the action never ran at all, so the operator -- who may be mid-emergency
    # for /emergency_stop specifically -- must get an explicit reply rather
    # than silence (the exception would otherwise only surface in the poll
    # loop's own log, not back to whoever sent the command).
    try:
        result = action()
    except Exception as exc:
        return f"Failed to execute {command}: {exc}. Check the console/logs."
    return _format_result(result, success_text)


def _format_trade_line(trade) -> str:
    row = views.to_trade_row(trade)
    return f"{row.exit_time}  {row.symbol} {row.direction}  net={row.net_pnl:+.2f}  R={row.r_multiple:.2f}  {row.exit_reason}"


def _handle_trades() -> str:
    # Unlike /start /stop /emergency_stop's _run_gui_action, journal reads
    # here can raise from transient SQLite contention with the live loop's
    # own writes to the same file (e.g. "database is locked") -- must still
    # guarantee a reply, since run_poll_loop advances the update offset
    # before this return value is used, so an uncaught exception here would
    # silently drop the operator's message with no reply at all.
    try:
        all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=DEFAULT_PAPER_DB_PATH)
    except Exception as exc:
        return f"Failed to fetch trade data: {exc}. Try again."
    if not all_trades:
        return "No trades recorded yet."
    recent = views.newest_first(all_trades)[:_TRADES_REPLY_LIMIT]
    lines = [f"Most recent {len(recent)} trade(s) (paper mode):"]
    lines.extend(_format_trade_line(trade) for trade in recent)
    return "\n".join(lines)


def _format_position_line(row: views.OpenPositionRow) -> str:
    return (
        f"{row.symbol} {row.direction} {row.volume} @{row.price_open:.2f} "
        f"(now {row.price_current:.2f}) P/L={row.profit:+.2f} SL={row.sl:.2f} TP={row.tp:.2f}"
    )


def _handle_positions() -> str:
    # get_open_positions_display() already catches its own MT5 failures
    # (returning None rather than raising, per its own docstring), so this
    # try/except is only a last line of defense against something outside
    # that scope (e.g. a formatting bug in _format_position_line) -- same
    # "must still guarantee a reply" reasoning as _handle_trades/_handle_daily,
    # since run_poll_loop advances the update offset before this return
    # value is used.
    try:
        rows = get_open_positions_display()
    except Exception as exc:
        return f"Failed to fetch open positions: {exc}. Try again."
    if rows is None:
        return "Could not reach MT5 -- open positions unavailable. Check the terminal/connection."
    if not rows:
        return "No open positions."
    lines = [f"{len(rows)} open position(s):"]
    lines.extend(_format_position_line(row) for row in rows)
    return "\n".join(lines)


def _handle_daily() -> ControlReply:
    # Same failure-reply guarantee as _handle_trades, and for the same
    # reason (run_poll_loop's offset advancement means an uncaught exception
    # here silently drops the operator's message).
    try:
        # The reported date comes from the trade data itself (views.default_daily_date),
        # never MT5/wall-clock -- same convention dashboard/app.py's /daily route
        # already uses, kept here so this listener stays MT5-free (module docstring).
        all_trades = journal.get_trades_in_range(views.EPOCH, views.FAR_FUTURE, db_path=DEFAULT_PAPER_DB_PATH)
        server_date = views.default_daily_date(all_trades)
        if server_date is None:
            return ControlReply(text="No trades recorded yet.")
        report = build_daily_report(server_date, db_path=DEFAULT_PAPER_DB_PATH)
        text = format_daily_report(report)
    except Exception as exc:
        return ControlReply(text=f"Failed to fetch trade data: {exc}. Try again.")

    try:
        # Charts are built from the FULL trade history already fetched above
        # (all_trades), not just server_date's single day -- an equity
        # curve/daily P/L bar chart is only meaningful across the whole
        # recorded history, unlike the day-scoped report text itself. A
        # separate try/except from the fetch/report block above: a chart
        # RENDERING failure must not swallow an otherwise-successful text
        # report into the "Failed to fetch trade data" message -- same
        # "must still guarantee a reply" reasoning, just degrading to a
        # text-only reply here instead of no reply at all.
        photos = [
            ChartPhoto(png=charts.build_equity_curve_png(all_trades), caption="Equity curve"),
            ChartPhoto(png=charts.build_daily_pnl_png(all_trades), caption="Daily net P/L"),
        ]
    except Exception as exc:
        logger.warning("/daily: chart rendering failed (%s) -- replying with text only.", exc)
        return ControlReply(text=text)

    return ControlReply(text=text, photos=photos)


def handle_update(
    update: dict, configured_chat_id: str, pending: PendingConfirmation, clock: Clock,
) -> ControlReply | None:
    if not is_authorized(update, configured_chat_id):
        return None

    text = update["message"]["text"]
    command = parse_command(text)

    if command == "/start":
        return ControlReply(text=_run_gui_action("/start", gui_control.start_bot, "AutoTrade start requested."))
    if command == "/stop":
        return ControlReply(text=_run_gui_action("/stop", gui_control.stop_bot, "Graceful stop requested."))
    if command == "/status":
        return ControlReply(text=gui_control.format_status(gui_control.build_status()))
    if command == "/emergency_stop":
        code = pending.request(clock)
        return ControlReply(
            text=(
                "EMERGENCY STOP requested -- this halts trading AND closes every open "
                f"position at market. Reply with {code} within 60 seconds to confirm."
            )
        )
    if command == "/trades":
        return ControlReply(text=_handle_trades())
    if command == "/positions":
        return ControlReply(text=_handle_positions())
    if command == "/daily":
        return _handle_daily()
    if command == "/help":
        return ControlReply(text=_USAGE_TEXT)

    had_pending = pending.code is not None
    if pending.confirm(text, clock):
        return ControlReply(
            text=_run_gui_action("/emergency_stop", gui_control.emergency_stop_bot, "Emergency stop executed.")
        )

    if _looks_like_confirmation_attempt(text):
        return ControlReply(text="Confirmation code did not match or has expired -- emergency stop NOT executed.")

    if had_pending:
        # PendingConfirmation.confirm() already cleared the state above (same
        # single-guess-then-clear property as a wrong code) -- this branch
        # only changes the REPLY, so the operator knows an interleaved
        # message cancelled it rather than reading it as their code timing out.
        return ControlReply(
            text=(
                "Emergency-stop confirmation cancelled (a different message was received first). "
                "Re-issue /emergency_stop if you still want to close all positions."
            )
        )

    return ControlReply(text=_USAGE_TEXT)
