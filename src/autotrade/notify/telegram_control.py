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

`ControlReply` additionally carries an optional `reply_markup` -- Telegram's
inline-keyboard structure -- for /status and /help, offering one-tap buttons
for the read-only, always-safe commands (/positions, /trades, /daily) only.
/start, /stop, /emergency_stop, and /dashboard are deliberately NEVER
offered as buttons: /start /stop /emergency_stop are consequential actions
that must stay explicit-typed-command-only, matching this project's "no
bypassing manual safety gates" caution elsewhere in the codebase; /dashboard
is grouped with them not for safety but because it spawns a background
process the same way /start does, so it gets the same "must be typed, never
one-tap" treatment rather than its own special case. `handle_callback_query()`
handles a tapped button's resulting `callback_query` update --
`parse_callback_data()` maps its stable `"cmd:..."` callback data back to
the same command tokens `parse_command()` already produces, and dispatch
reuses the exact same `_handle_positions`/`_handle_trades`/`_handle_daily`
functions `handle_update()` uses, so a tapped button produces an identical
reply to typing the command.

/dashboard (2026-08-04, lean-plan P1, docs/vps_lean_plan.md) is this
module's second accepted MT5-adjacent-package exception, alongside
/positions: it launches `scripts/run_dashboard.py` directly via
`common/service_watchdog.py`'s `spawn_detached()` -- the SAME
`CREATE_BREAKAWAY_FROM_JOB` detached-spawn helper `run_health_check.py`'s
own restart path uses -- rather than going through `gui_control`/
`autotrade_control.py` like /start does, because the dashboard is a Flask
process, not the shadow loop `autotrade_control.py` knows how to manage.
Reuses `common/pid_file.py` directly (same as `scripts/run_dashboard.py`
itself) to check whether it's already running before spawning a second,
redundant instance. **`run_health_check.py` deliberately no longer
auto-restarts the dashboard** (see that script's own module docstring) --
/dashboard here is now the ONLY way to bring it back up.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from autotrade.auditor.daily_report import build_daily_report, format_daily_report
from autotrade.common import pid_file
from autotrade.common.clock import Clock
from autotrade.common.config import REPO_ROOT
from autotrade.common.service_watchdog import spawn_detached
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

_COMMANDS = {
    "/start", "/stop", "/status", "/emergency_stop", "/trades", "/positions", "/daily", "/dashboard", "/help",
}

_USAGE_TEXT = (
    "AutoTrade control commands:\n"
    "/start - launch the shadow loop\n"
    "/stop - request a graceful stop\n"
    "/status - report loop/kill-switch/stop-flag state\n"
    "/emergency_stop - halt trading AND close every open position at market (requires confirmation)\n"
    "/trades - most recent 10 trades (paper mode)\n"
    "/positions - currently open positions\n"
    "/daily - daily trade-autopsy report for the most recent recorded day\n"
    "/dashboard - start the on-demand web dashboard (or report it's already running)"
)

# scripts/ has no __init__.py, so run_dashboard.py is a path, not an import
# -- same convention common/service_watchdog.py's own script_path arguments
# already use. PID path matches scripts/run_dashboard.py's own PID_PATH
# exactly (both must agree on where the running instance records itself).
_DASHBOARD_SCRIPT = REPO_ROOT / "scripts" / "run_dashboard.py"
_DASHBOARD_PID_PATH = REPO_ROOT / "data" / "db" / "dashboard.pid"

# How long to wait after spawning before checking whether it actually came
# up -- run_dashboard.py's own PID-file write happens early in main(), well
# before Flask's (slower, pandas/MetaTrader5-importing) app.run(), so this
# only needs to cover process-launch latency, not a full server-ready wait.
_DASHBOARD_SPAWN_CONFIRM_WAIT_SEC = 3

# Duplicated from scripts/run_dashboard.py's own _DEFAULT_IDLE_TTL_MINUTES
# (scripts/ has no __init__.py to import it from) -- same "kept in sync by
# hand, no shared source of truth" convention scripts/run_telegram_control.py's
# own _BOT_COMMANDS comment already documents for the same reason.
_DASHBOARD_IDLE_TTL_MINUTES = 30


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
    its own short label, not one caption shared across both.

    `reply_markup`, when not `None`, is Telegram's inline-keyboard structure
    (see module docstring) attached to `/status`/`/help` replies only --
    `notify/telegram.py`'s `send_message()` sends it as-is."""

    text: str
    photos: list[ChartPhoto] = field(default_factory=list)
    reply_markup: dict | None = None


def is_authorized(update: dict, configured_chat_id: str) -> bool:
    try:
        if "callback_query" in update:
            # A callback_query update has a different shape than a message
            # update -- the chat lives at update["callback_query"]["message"]
            # ["chat"]["id"], not update["message"]["chat"]["id"].
            sender_chat_id = update["callback_query"]["message"]["chat"]["id"]
        else:
            sender_chat_id = update["message"]["chat"]["id"]
    except (KeyError, TypeError):
        return False
    return str(sender_chat_id) == str(configured_chat_id)


def has_text_message(update: dict) -> bool:
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    return isinstance(message.get("text"), str)


def has_callback_query(update: dict) -> bool:
    callback_query = update.get("callback_query")
    if not isinstance(callback_query, dict):
        return False
    return isinstance(callback_query.get("data"), str)


def parse_command(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return UNKNOWN_COMMAND

    token = stripped.split()[0].lower()
    token = token.split("@", 1)[0]
    return token if token in _COMMANDS else UNKNOWN_COMMAND


# Inline-keyboard callback data is a small stable string per button (rather
# than the command text itself) so the wire format never changes shape even
# if a command's display text/wording changes later. Restricted to the
# read-only, always-safe commands -- see module docstring for why /start,
# /stop, /emergency_stop are deliberately excluded.
_CALLBACK_DATA_POSITIONS = "cmd:positions"
_CALLBACK_DATA_TRADES = "cmd:trades"
_CALLBACK_DATA_DAILY = "cmd:daily"

_CALLBACK_COMMAND_MAP = {
    _CALLBACK_DATA_POSITIONS: "/positions",
    _CALLBACK_DATA_TRADES: "/trades",
    _CALLBACK_DATA_DAILY: "/daily",
}


def parse_callback_data(data: str) -> str:
    """Maps an inline-keyboard button's callback data back to the same
    command token `parse_command()` would produce for the equivalent typed
    command (e.g. `"cmd:positions"` -> `"/positions"`), so
    `handle_callback_query()` can dispatch through the identical
    `_handle_*` functions `handle_update()` uses."""
    return _CALLBACK_COMMAND_MAP.get(data, UNKNOWN_COMMAND)


def _quick_access_keyboard(webapp_url: str | None = None) -> dict:
    """Inline keyboard attached to /status and /help replies -- one-tap
    buttons for the read-only, always-safe commands only. See module
    docstring for why /start, /stop, /emergency_stop are never offered here.

    When `webapp_url` is provided (see `common/config.py`'s
    `load_webapp_url()`), a second row adds a Telegram Web App button (Phase
    3 of the Telegram bot UX upgrade -- opens the dashboard inside the
    Telegram chat, gated by `dashboard/webapp_auth.py`'s `initData`
    verification) -- `{"web_app": {"url": ...}}`, distinct from a plain
    `{"url": ...}` button. Omitted entirely when not configured, never a
    crash."""
    keyboard = [
        [
            {"text": "Positions", "callback_data": _CALLBACK_DATA_POSITIONS},
            {"text": "Trades", "callback_data": _CALLBACK_DATA_TRADES},
            {"text": "Daily", "callback_data": _CALLBACK_DATA_DAILY},
        ]
    ]
    if webapp_url:
        keyboard.append([{"text": "Open Dashboard", "web_app": {"url": webapp_url}}])
    return {"inline_keyboard": keyboard}


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
        f"#{row.ticket} {row.symbol} {row.direction} {row.volume} @{row.price_open:.2f} "
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


def _dashboard_url_note(webapp_url: str | None) -> str:
    # Same "unconfigured == omit, never crash" pattern as
    # _quick_access_keyboard()'s own webapp_url handling -- WEBAPP_URL is
    # loaded once at scripts/run_telegram_control.py's own startup
    # (common/config.py's load_webapp_url()) and threaded through here as
    # the same `webapp_url` param /status and /help already receive, rather
    # than this module re-reading .env or hardcoding trade.kylerlink.com.
    return f"URL: {webapp_url}" if webapp_url else "No WEBAPP_URL configured in .env -- no clickable link available."


def _handle_dashboard(webapp_url: str | None, sleep_fn=None) -> str:
    # `sleep_fn or time.sleep`, not a `time.sleep` default argument value --
    # a default is bound once at function-definition time, so it would
    # capture the ORIGINAL time.sleep and stay unaffected by a test's own
    # `monkeypatch.setattr(telegram_control.time, "sleep", ...)` (a common
    # gotcha with mutable/callable default arguments).
    sleep_fn = sleep_fn or time.sleep

    # Mirrors scripts/run_dashboard.py's own double-launch guard (same
    # pid_file.read() + is_pid_running() pair) rather than blindly spawning
    # -- a second Flask instance would just fail to bind the port anyway,
    # but checking first lets this reply usefully report "already running"
    # instead of a spurious launch attempt.
    existing_pid = pid_file.read(_DASHBOARD_PID_PATH)
    if existing_pid is not None and pid_file.is_pid_running(existing_pid):
        return f"Dashboard already running (PID {existing_pid}). {_dashboard_url_note(webapp_url)}"

    if not spawn_detached("Dashboard", _DASHBOARD_SCRIPT, REPO_ROOT):
        return "Failed to launch the dashboard -- check the console/logs."

    # A brief wait for run_dashboard.py's own PID-file write (early in its
    # main(), well before Flask's app.run()) so this reply can confirm a
    # real PID rather than always claiming success the instant Popen()
    # returns (which only proves the OS accepted the launch request, not
    # that the script itself came up).
    sleep_fn(_DASHBOARD_SPAWN_CONFIRM_WAIT_SEC)

    new_pid = pid_file.read(_DASHBOARD_PID_PATH)
    if new_pid is not None and pid_file.is_pid_running(new_pid):
        return (
            f"Dashboard started (PID {new_pid}). {_dashboard_url_note(webapp_url)} "
            f"Auto-stops after {_DASHBOARD_IDLE_TTL_MINUTES} min idle."
        )
    # Not confirmable within the short wait -- on the 1-core VPS the
    # pandas/Flask import alone takes ~20-30s (docs/vps_lean_plan.md P1's
    # own risk note), so this is the NORMAL first-start path, not an error.
    # Waiting the full 30s here would block the poll loop for every other
    # command; instead reply honestly and let the operator re-issue
    # /dashboard for a real confirmation (the already-running branch above).
    return (
        f"Dashboard launching -- first start takes ~30s on the VPS. {_dashboard_url_note(webapp_url)} "
        f"Send /dashboard again to confirm it's up. Auto-stops after {_DASHBOARD_IDLE_TTL_MINUTES} min idle."
    )


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
    webapp_url: str | None = None,
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
        return ControlReply(
            text=gui_control.format_status(gui_control.build_status()),
            reply_markup=_quick_access_keyboard(webapp_url),
        )
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
    if command == "/dashboard":
        return ControlReply(text=_handle_dashboard(webapp_url))
    if command == "/help":
        return ControlReply(text=_USAGE_TEXT, reply_markup=_quick_access_keyboard(webapp_url))

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


def handle_callback_query(update: dict, configured_chat_id: str) -> ControlReply | None:
    """`handle_update()`'s counterpart for a tapped inline-keyboard button
    (a `callback_query` update, not a `message` update -- see
    `has_callback_query()`). Only ever reachable via `_quick_access_keyboard()`
    's buttons, so only /positions, /trades, /daily are dispatched here --
    reuses the exact same `_handle_*` functions `handle_update()` uses, so a
    tapped button produces an identical reply to typing the command. No
    `PendingConfirmation`/`Clock` needed: none of these three commands ever
    enter the /emergency_stop confirmation flow."""
    if not is_authorized(update, configured_chat_id):
        return None

    data = update["callback_query"].get("data", "")
    command = parse_callback_data(data)

    if command == "/trades":
        return ControlReply(text=_handle_trades())
    if command == "/positions":
        return ControlReply(text=_handle_positions())
    if command == "/daily":
        return _handle_daily()

    return ControlReply(text=_USAGE_TEXT)
