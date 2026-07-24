"""Telegram Web App `initData` HMAC verification -- pure, no Flask, no
network I/O, fully unit-testable in isolation from `dashboard/app.py`'s own
Flask request-gating logic (which calls this module but owns the
session/cookie/response-shape concerns itself).

Implements Telegram's own documented validation algorithm
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app):
a `secret_key` is derived as `HMAC-SHA256(key="WebAppData", msg=bot_token)`,
then every received field except `hash` (sorted alphabetically, joined as
`key=value` lines with `\n`) is itself HMAC-SHA256'd with that `secret_key`
and compared to the received `hash` field.

`verify_init_data()` alone only proves "this came from Telegram" -- it says
nothing about WHICH Telegram user sent it. `is_operator()` is the actual
authorization decision, matching this project's existing "only this
configured chat id" gating pattern (see `notify/telegram_control.py`'s
`is_authorized()`)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# Telegram re-signs `initData` (with a fresh `auth_date`) every time the Mini
# App is (re)opened -- a stale `auth_date` means a captured/replayed
# initData string being replayed later, not a legitimately still-open
# session (long-lived access is instead carried by dashboard/app.py's own
# signed session cookie, established once from a fresh initData). A few
# hours is generous enough to absorb clock skew/a slow page load without
# meaningfully weakening the replay check.
MAX_INIT_DATA_AGE_SECONDS = 3 * 3600


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS) -> dict | None:
    """Verifies `init_data` (the raw query string Telegram's Web App JS SDK
    exposes as `window.Telegram.WebApp.initData`) against Telegram's own
    HMAC-SHA256 validation algorithm (module docstring), and rejects a stale
    `auth_date`. Never raises on malformed input -- a bad/missing/tampered/
    expired `init_data` is just `None`, not a crash.

    On success, returns the parsed field dict (with `user` decoded from its
    JSON string into a `dict`, if present) -- this alone is NOT proof of who
    the operator is, see `is_operator()`."""
    if not init_data or not bot_token:
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None

    parsed: dict[str, str] = {}
    received_hash = None
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            parsed[key] = value

    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date_raw = parsed.get("auth_date")
    if auth_date_raw is None:
        return None
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        return None
    if time.time() - auth_date > max_age_seconds:
        return None

    result = dict(parsed)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except (ValueError, TypeError):
            return None

    return result


def is_operator(parsed_init_data: dict, configured_chat_id: str) -> bool:
    """The actual authorization decision -- `verify_init_data()`'s HMAC check
    alone only proves the data came from Telegram, not that it's the
    operator. Same "only this configured chat id" restriction as
    `notify/telegram_control.py`'s `is_authorized()` (a Telegram user id and
    the operator's own chat id are the same numeric space for a private bot
    chat). Never raises on a malformed/missing `user` field -- just `False`."""
    user = parsed_init_data.get("user")
    if not isinstance(user, dict):
        return False
    user_id = user.get("id")
    if user_id is None:
        return False
    return str(user_id) == str(configured_chat_id)
