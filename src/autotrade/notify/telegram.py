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
import uuid

from autotrade.common.config import load_telegram_credentials, load_yaml_config

logger = logging.getLogger(__name__)

_LOG_TRUNCATE_CHARS = 200
_PHOTO_TIMEOUT_SEC = 8.0
"""Longer default than sendMessage's 4.0s -- a PNG chart upload is a larger
payload than a short text message and multipart/form-data POSTs to
Telegram's API have been observed to take longer than plain urlencoded
text."""


def _post_message(token: str, chat_id: str, text: str, timeout_sec: float) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")

    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        response.read()


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _build_send_photo_body(boundary: str, chat_id: str, png_bytes: bytes, caption: str | None) -> bytes:
    """multipart/form-data body for Telegram's sendPhoto -- a genuinely
    different request shape than `_post_message`'s urlencoded sendMessage
    (binary file part, not just string fields), so this is built by hand
    rather than reusing `urllib.parse.urlencode`."""
    parts = [_multipart_field(boundary, "chat_id", chat_id)]
    if caption:
        parts.append(_multipart_field(boundary, "caption", caption))
    parts.append(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    .encode("utf-8"))
    parts.append(png_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


def _post_photo(token: str, chat_id: str, png_bytes: bytes, caption: str | None, timeout_sec: float) -> None:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    # uuid4, not a fixed string -- the boundary must not collide with any
    # byte sequence that could plausibly appear inside png_bytes/caption.
    boundary = uuid.uuid4().hex
    body = _build_send_photo_body(boundary, chat_id, png_bytes, caption)

    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        response.read()


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
        _post_message(token, chat_id, text, timeout_sec)
        return True
    except Exception as exc:
        truncated = text if len(text) <= _LOG_TRUNCATE_CHARS else text[:_LOG_TRUNCATE_CHARS] + "..."
        logger.warning("notify: failed (%s) for message: %r", exc, truncated)
        return False


def notify_photo(png_bytes: bytes, *, caption: str | None = None, timeout_sec: float = _PHOTO_TIMEOUT_SEC) -> bool:
    """Chart-image counterpart to `notify()` -- same `notifications.enabled`
    gate, same missing-credential no-op, same never-raises/returns-bool
    contract, but posts a PNG (multipart/form-data via `_post_photo`)
    instead of `sendMessage` text. Used by `scripts/run_auditor.py`'s
    `daily --notify` to attach the equity-curve/daily-P&L charts to the
    existing automatic daily report notification, after the report text
    itself has already sent successfully."""
    try:
        creds = load_telegram_credentials()
        if creds is None:
            logger.debug(
                "notify_photo: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured -- skipping"
            )
            return False

        cfg = load_yaml_config("base")
        if not cfg.get("notifications", {}).get("enabled", True):
            logger.debug("notify_photo: notifications.enabled is False in config/base.yaml -- skipping")
            return False

        token, chat_id = creds
        _post_photo(token, chat_id, png_bytes, caption, timeout_sec)
        return True
    except Exception as exc:
        logger.warning("notify_photo: failed (%s)", exc)
        return False


def send_message(text: str, *, timeout_sec: float = 4.0) -> bool:
    """Send `text` to the configured Telegram chat unconditionally of
    `notifications.enabled` -- this is for command REPLIES on the inbound
    control channel (scripts/run_telegram_control.py), where the user is
    actively waiting for a response to a command they just sent, not a
    best-effort outbound notification `notify()`'s toggle is meant to gate.
    Still a no-op (returns False) if credentials are missing, same as
    `notify()`."""
    try:
        creds = load_telegram_credentials()
        if creds is None:
            logger.debug(
                "send_message: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured -- skipping"
            )
            return False

        token, chat_id = creds
        _post_message(token, chat_id, text, timeout_sec)
        return True
    except Exception as exc:
        truncated = text if len(text) <= _LOG_TRUNCATE_CHARS else text[:_LOG_TRUNCATE_CHARS] + "..."
        logger.warning("send_message: failed (%s) for message: %r", exc, truncated)
        return False


def send_photo(png_bytes: bytes, *, caption: str | None = None, timeout_sec: float = _PHOTO_TIMEOUT_SEC) -> bool:
    """Chart-image counterpart to `send_message()` -- unconditional of
    `notifications.enabled`, same reasoning: this is for a command REPLY on
    the inbound control channel (`/daily`'s charts, see
    `notify/telegram_control.py`), not a best-effort outbound notification.
    Still a no-op (returns False) if credentials are missing, same as
    `send_message()`."""
    try:
        creds = load_telegram_credentials()
        if creds is None:
            logger.debug(
                "send_photo: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured -- skipping"
            )
            return False

        token, chat_id = creds
        _post_photo(token, chat_id, png_bytes, caption, timeout_sec)
        return True
    except Exception as exc:
        logger.warning("send_photo: failed (%s)", exc)
        return False
