#!/usr/bin/env python3
"""Telegram inbound control listener -- long-polls Telegram's getUpdates API
and dispatches /start, /stop, /status, /emergency_stop (with confirmation)
via autotrade.notify.telegram_control.handle_update(), replying through
autotrade.notify.telegram.send_message(). See
AutoTrade_TelegramControl_Start.bat (repo root) for how this is normally
launched -- the .bat's own console window IS the running listener; there is
no separate PID/stop mechanism, closing the window stops it.

    python scripts/run_telegram_control.py
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request

from autotrade.common.clock import RealClock
from autotrade.common.config import load_telegram_credentials
from autotrade.notify import telegram
from autotrade.notify.telegram_control import PendingConfirmation, handle_update, has_text_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_POLL_TIMEOUT_SEC = 30
_SOCKET_TIMEOUT_SEC = 40
_ERROR_BACKOFF_SEC = 4
_BACKLOG_DISCARD_MAX_ATTEMPTS = 3


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


def run_poll_loop(
    token: str,
    chat_id: str,
    poll_fn=_get_updates,
    send_fn=None,
    sleep_fn=time.sleep,
    clock=None,
    max_iterations: int | None = None,
) -> None:
    clock = clock or RealClock()
    send_fn = send_fn or telegram.send_message
    pending = PendingConfirmation()

    offset = _discard_backlog(token, poll_fn, sleep_fn)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            updates = poll_fn(token, offset, _POLL_TIMEOUT_SEC)
            for update in sorted(updates, key=lambda u: u["update_id"]):
                offset = max(offset, update["update_id"] + 1)
                if not has_text_message(update):
                    continue

                reply = handle_update(update, chat_id, pending, clock)
                if reply is not None:
                    send_fn(reply)
                else:
                    logger.warning("Ignoring update from an unauthorized sender.")
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

    token, chat_id = creds
    logger.info("Telegram control listener starting (authorized chat_id=%s).", chat_id)
    run_poll_loop(
        token, chat_id, poll_fn=_get_updates, send_fn=telegram.send_message,
        sleep_fn=time.sleep, clock=RealClock(), max_iterations=args.max_iterations,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Telegram control listener stopped (Ctrl+C).")
        sys.exit(0)
