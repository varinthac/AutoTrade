#!/usr/bin/env python3
"""Telegram inbound control listener -- long-polls Telegram's getUpdates API
and dispatches /start, /stop, /status, /emergency_stop (with confirmation),
plus tapped inline-keyboard buttons (`callback_query` updates), via
autotrade.notify.telegram_control.handle_update()/handle_callback_query(),
replying through autotrade.notify.telegram.send_message() (text, optionally
carrying an inline keyboard) and, for /daily's chart attachments,
autotrade.notify.telegram.send_photo(). A tapped button's loading spinner is
cleared via autotrade.notify.telegram.answer_callback_query(). See
AutoTrade_TelegramControl_Start.bat (repo root) for how this is normally
launched -- the .bat's own console window IS the running listener; there is
no separate PID/stop mechanism, closing the window stops it.

    python scripts/run_telegram_control.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

from autotrade.common import pid_file
from autotrade.common.clock import RealClock
from autotrade.common.config import REPO_ROOT, load_telegram_credentials, load_webapp_url
from autotrade.notify import telegram
from autotrade.notify.telegram_control import (
    ControlReply,
    PendingConfirmation,
    handle_callback_query,
    handle_update,
    has_callback_query,
    has_text_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_POLL_TIMEOUT_SEC = 30
_SOCKET_TIMEOUT_SEC = 40
_ERROR_BACKOFF_SEC = 4
_BACKLOG_DISCARD_MAX_ATTEMPTS = 3

# 2026-07-24: an RDP reconnect re-fires every "At log on" Task Scheduler
# trigger for that logon event (Shadow Loop, Dashboard, Telegram Control
# alike -- see docs/vps_deployment.md Section 6a), not just the first time.
# run_shadow_loop.py already refuses a second instance via its own PID file
# (common/pid_file.py); this listener had no equivalent, so a reconnect
# could launch a second poller against the same bot token, and both would
# fight over Telegram's getUpdates offset (each answering/mis-answering
# some fraction of updates) rather than one cleanly refusing to start.
PID_PATH = REPO_ROOT / "data" / "db" / "telegram_control.pid"

# Registers Telegram's persistent bottom-of-keyboard command menu (see
# telegram.set_my_commands()) -- command names given WITHOUT the leading
# slash, matching that endpoint's own convention. Kept in sync with
# telegram_control.py's _USAGE_TEXT/_COMMANDS by hand (no shared source of
# truth to derive this from without importing MT5-adjacent GUI text).
_BOT_COMMANDS = [
    ("start", "Launch the shadow loop"),
    ("stop", "Request a graceful stop"),
    ("status", "Report loop/kill-switch/stop-flag state"),
    ("emergency_stop", "Halt trading AND close every open position at market"),
    ("trades", "Most recent 10 trades (paper mode)"),
    ("positions", "Currently open positions"),
    ("daily", "Daily trade-autopsy report for the most recent recorded day"),
    ("help", "Show this help text"),
]


def _get_updates(token: str, offset: int, timeout_sec: int) -> list[dict]:
    """The only place a Telegram API URL (embeds the bot token) is ever
    built on the polling side -- never pass the resulting URL/request to a
    logger, matching notify/telegram.py's own token-redaction discipline."""
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout={timeout_sec}&offset={offset}"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=_SOCKET_TIMEOUT_SEC) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("result", [])


def _drain_backlog_once(token: str, poll_fn) -> tuple[int, int]:
    """Fully drains every page of stale, already-queued updates via
    non-blocking (0-timeout) getUpdates calls -- Telegram caps a single
    getUpdates response at ~100 results, so one call is not enough to
    discard a large backlog; looping on the returned offset until a page
    comes back empty (or the offset stops advancing) is required to drain
    it all."""
    offset = 0
    discarded = 0
    while True:
        page = poll_fn(token, offset, 0)
        if not page:
            return offset, discarded
        next_offset = max(update["update_id"] for update in page) + 1
        discarded += len(page)
        if next_offset <= offset:
            return offset, discarded
        offset = next_offset


def _discard_backlog(token: str, poll_fn, sleep_fn) -> int:
    """A stale /emergency_stop sent hours ago while this listener was down
    must never be acted on the moment it comes back online, so the backlog
    is drained (see _drain_backlog_once) before computing the starting
    offset. Wrapped in the same log+backoff+retry pattern as the main loop
    (bounded, unlike the main loop's forever-retry) so a transient network
    hiccup right at startup (e.g. the .bat launching before the network
    stack is up after a reboot) doesn't crash the whole process."""
    for attempt in range(1, _BACKLOG_DISCARD_MAX_ATTEMPTS + 1):
        try:
            offset, discarded = _drain_backlog_once(token, poll_fn)
            if discarded:
                logger.info("Discarded %d backlogged update(s) received while offline.", discarded)
            return offset
        except Exception as exc:
            # Never log exc itself/its args -- HTTPError/URLError instances
            # can carry the request URL, which embeds the bot token.
            logger.warning(
                "Backlog-discard attempt %d/%d failed (%s) -- retrying after backoff.",
                attempt, _BACKLOG_DISCARD_MAX_ATTEMPTS, type(exc).__name__,
            )
            sleep_fn(_ERROR_BACKOFF_SEC)

    # WHY offset=0 rather than exiting: a listener that starts able to
    # respond to commands (even if it re-surfaces an already-stale backlog
    # page as "live") is safer than one that refuses to start at all --
    # an operator can still see and react to a re-surfaced stale command,
    # but can't react to anything from a process that never came up.
    logger.warning(
        "Backlog-discard failed after %d attempts -- proceeding with offset=0.",
        _BACKLOG_DISCARD_MAX_ATTEMPTS,
    )
    return 0


def _send_reply(reply: ControlReply, send_fn, send_photo_fn) -> None:
    if reply.reply_markup is not None:
        send_fn(reply.text, reply_markup=reply.reply_markup)
    else:
        send_fn(reply.text)
    for photo in reply.photos:
        send_photo_fn(photo.png, caption=photo.caption)


def run_poll_loop(
    token: str,
    chat_id: str,
    poll_fn=_get_updates,
    send_fn=None,
    send_photo_fn=None,
    answer_callback_fn=None,
    sleep_fn=time.sleep,
    clock=None,
    max_iterations: int | None = None,
    webapp_url: str | None = None,
) -> None:
    clock = clock or RealClock()
    send_fn = send_fn or telegram.send_message
    send_photo_fn = send_photo_fn or telegram.send_photo
    answer_callback_fn = answer_callback_fn or (
        lambda callback_query_id: telegram.answer_callback_query(callback_query_id, bot_token=token)
    )
    pending = PendingConfirmation()

    offset = _discard_backlog(token, poll_fn, sleep_fn)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            updates = poll_fn(token, offset, _POLL_TIMEOUT_SEC)
            for update in sorted(updates, key=lambda u: u["update_id"]):
                # Offset advances before the reply is used, for EVERY update
                # kind (text message or callback_query) -- an uncaught
                # exception below must never cause the same update to be
                # reprocessed forever.
                offset = max(offset, update["update_id"] + 1)

                if has_text_message(update):
                    reply = handle_update(update, chat_id, pending, clock, webapp_url=webapp_url)
                    if reply is not None:
                        _send_reply(reply, send_fn, send_photo_fn)
                    else:
                        logger.warning("Ignoring update from an unauthorized sender.")
                elif has_callback_query(update):
                    reply = handle_callback_query(update, chat_id)
                    if reply is not None:
                        _send_reply(reply, send_fn, send_photo_fn)
                    else:
                        logger.warning("Ignoring callback query from an unauthorized sender.")
                    answer_callback_fn(update["callback_query"]["id"])
        except Exception as exc:
            # Never log exc itself/its args -- HTTPError/URLError instances
            # can carry the request URL, which embeds the bot token.
            logger.warning("Poll iteration failed (%s) -- retrying after backoff.", type(exc).__name__)
            sleep_fn(_ERROR_BACKOFF_SEC)

        iterations += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after this many poll iterations (mainly for tests); default runs forever",
    )
    args = parser.parse_args()

    creds = load_telegram_credentials()
    if creds is None:
        print(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set in .env -- refusing to start. Unlike "
            "notify()'s silent no-op, an inbound listener that can never receive commands must not "
            "run silently-doing-nothing-forever.",
            file=sys.stderr,
        )
        return 1

    existing_pid = pid_file.read(PID_PATH)
    if existing_pid is not None and pid_file.is_pid_running(existing_pid):
        logger.error(
            "Telegram control listener already running (PID %d) -- refusing to start a second "
            "instance (would fight the running one over Telegram's getUpdates offset).", existing_pid,
        )
        return 1
    try:
        pid_file.write(os.getpid(), PID_PATH)
    except FileExistsError as exc:
        logger.error("Lost the race to claim the PID file: %s", exc)
        return 1

    token, chat_id = creds
    webapp_url = load_webapp_url()
    logger.info("Telegram control listener starting (authorized chat_id=%s).", chat_id)
    try:
        telegram.set_my_commands(_BOT_COMMANDS, bot_token=token)
        run_poll_loop(
            token, chat_id, poll_fn=_get_updates, send_fn=telegram.send_message,
            send_photo_fn=telegram.send_photo,
            answer_callback_fn=lambda callback_query_id: telegram.answer_callback_query(
                callback_query_id, bot_token=token
            ),
            sleep_fn=time.sleep, clock=RealClock(),
            max_iterations=args.max_iterations,
            webapp_url=webapp_url,
        )
    finally:
        pid_file.remove(PID_PATH)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Telegram control listener stopped (Ctrl+C).")
        sys.exit(0)
