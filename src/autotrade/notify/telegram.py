"""Best-effort Telegram notifications -- a single chat receives all four
notification categories (safety-critical events, position opened/closed,
daily summary, promotion/demotion gate changes); see spec resolved decisions
for why this is deliberately NOT split per-category and deliberately has no
rate-limiting/dedupe machinery (a circuit-breaker cascade sending several
messages close together is acceptable).

`notify()` takes only a pre-formatted string -- it never reads a clock
itself. Every caller is responsible for building message text from
already-server-time-stamped source data (this codebase's data already
carries correct timestamps); do not re-stamp with `datetime.now()`/
`RealClock` anywhere in this feature.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request

from autotrade.common.config import load_telegram_credentials, load_yaml_config

logger = logging.getLogger(__name__)

_LOG_TRUNCATE_CHARS = 200


def notify(text: str, *, timeout_sec: float = 4.0) -> bool:
    """Send `text` to the configured Telegram chat. Never raises -- EVERY
    call site (including safety-critical ones like `scripts/kill_switch.py`'s
    `do_activate` and `watchman/loop.py`'s trade-close bookkeeping) depends
    on this being an unconditionally safe, at-most-logged no-op, not just for
    a missing credential/`notifications.enabled: False`/network failure but
    also for a transiently-unreadable or malformed `config/base.yaml` (e.g.
    its root isn't a dict) or any other unexpected error while preparing the
    request -- so the ENTIRE body, not just the network call, is wrapped in
    one broad `except Exception`.

    Returns `True` only if the message was actually sent, `False` for every
    skip/failure case (not configured, disabled, or any exception). Most
    call sites ignore this (fire-and-forget is fine for them), but a caller
    that gates a persisted "already notified" state on this happening --
    e.g. `scripts/run_auditor.py`'s promotion/demotion `--notify` dedup via
    `notify/gate_state.py` -- MUST check it: persisting that state on a
    failed send would permanently lose the notification (the next run with
    an unchanged gate result would see "no change" and never retry)."""
    try:
        creds = load_telegram_credentials()
        if creds is None:
            logger.debug(
                "notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured -- skipping"
            )
            return False

        cfg = load_yaml_config("base")
        if not cfg.get("notifications", {}).get("enabled", True):
            logger.debug("notify: notifications.enabled is False in config/base.yaml -- skipping")
            return False

        token, chat_id = creds
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")

        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            response.read()
        return True
    except Exception as exc:
        truncated = text if len(text) <= _LOG_TRUNCATE_CHARS else text[:_LOG_TRUNCATE_CHARS] + "..."
        logger.warning("notify: failed (%s) for message: %r", exc, truncated)
        return False
