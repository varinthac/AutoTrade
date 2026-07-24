"""Unit tests for dashboard/webapp_auth.py -- Telegram Web App `initData`
HMAC verification. No Flask, no network I/O; `_build_init_data()` below
constructs a real signed `initData` string using the exact algorithm
verify_init_data() itself implements (module docstring), so a passing
`test_valid_init_data_*` test is a genuine round-trip check, not a mocked-out
assertion."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from autotrade.dashboard.webapp_auth import (
    MAX_INIT_DATA_AGE_SECONDS,
    is_operator,
    verify_init_data,
)

BOT_TOKEN = "123456:test-bot-token"
OPERATOR_USER = {"id": 8978823598, "first_name": "Operator", "username": "op_user"}


def _build_init_data(bot_token, user=None, auth_date=None, extra_fields=None, corrupt_hash=False):
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())), "query_id": "AAHabc123"}
    if user is not None:
        fields["user"] = json.dumps(user, separators=(",", ":"))
    if extra_fields:
        fields.update(extra_fields)

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if corrupt_hash:
        flipped_char = "0" if computed_hash[0] != "0" else "1"
        computed_hash = flipped_char + computed_hash[1:]

    fields["hash"] = computed_hash
    return urlencode(fields)


# --- verify_init_data(): valid signature ------------------------------------


def test_valid_init_data_returns_parsed_dict_with_decoded_user():
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER)

    result = verify_init_data(init_data, BOT_TOKEN)

    assert result is not None
    assert result["user"] == OPERATOR_USER
    assert "hash" not in result


def test_valid_init_data_without_a_user_field_still_verifies():
    init_data = _build_init_data(BOT_TOKEN)

    result = verify_init_data(init_data, BOT_TOKEN)

    assert result is not None
    assert "user" not in result


# --- verify_init_data(): tampering -------------------------------------------


def test_tampered_hash_is_rejected():
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER, corrupt_hash=True)

    assert verify_init_data(init_data, BOT_TOKEN) is None


def test_tampered_field_value_is_rejected():
    # A valid hash for a DIFFERENT user id, spliced onto a modified user
    # field -- must fail because the hash no longer matches the payload.
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER)
    tampered = init_data.replace(str(OPERATOR_USER["id"]), "999999999")

    assert verify_init_data(tampered, BOT_TOKEN) is None


def test_wrong_bot_token_is_rejected():
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER)

    assert verify_init_data(init_data, "some-other-bot-token") is None


# --- verify_init_data(): auth_date staleness ---------------------------------


def test_fresh_auth_date_within_window_is_accepted():
    stale_but_within_window = time.time() - (MAX_INIT_DATA_AGE_SECONDS - 60)
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER, auth_date=stale_but_within_window)

    assert verify_init_data(init_data, BOT_TOKEN) is not None


def test_auth_date_older_than_max_age_is_rejected():
    too_old = time.time() - (MAX_INIT_DATA_AGE_SECONDS + 60)
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER, auth_date=too_old)

    assert verify_init_data(init_data, BOT_TOKEN) is None


def test_custom_max_age_seconds_is_respected():
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER, auth_date=time.time() - 120)

    assert verify_init_data(init_data, BOT_TOKEN, max_age_seconds=60) is None
    assert verify_init_data(init_data, BOT_TOKEN, max_age_seconds=600) is not None


def test_missing_auth_date_field_is_rejected():
    fields = {"query_id": "AAHabc123"}
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    assert verify_init_data(urlencode(fields), BOT_TOKEN) is None


# --- verify_init_data(): malformed/missing input never raises ---------------


def test_empty_init_data_returns_none():
    assert verify_init_data("", BOT_TOKEN) is None


def test_empty_bot_token_returns_none():
    init_data = _build_init_data(BOT_TOKEN, user=OPERATOR_USER)

    assert verify_init_data(init_data, "") is None


def test_garbage_init_data_returns_none_not_raises():
    assert verify_init_data("not a valid query string at all !!!", BOT_TOKEN) is None


def test_init_data_missing_hash_field_returns_none():
    assert verify_init_data("auth_date=123&query_id=abc", BOT_TOKEN) is None


def test_init_data_with_malformed_user_json_returns_none():
    # A hand-crafted (not properly signed) payload whose `hash` was computed
    # over the exact string sent, but whose `user` value isn't valid JSON --
    # exercises the post-HMAC json.loads() failure path directly.
    fields = {"auth_date": str(int(time.time())), "user": "{not valid json"}
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    assert verify_init_data(urlencode(fields), BOT_TOKEN) is None


def test_non_integer_auth_date_returns_none():
    fields = {"auth_date": "not-a-number", "query_id": "abc"}
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    assert verify_init_data(urlencode(fields), BOT_TOKEN) is None


# --- is_operator() -----------------------------------------------------------


def test_is_operator_true_when_user_id_matches_configured_chat_id():
    parsed = {"user": {"id": 8978823598}}

    assert is_operator(parsed, "8978823598") is True


def test_is_operator_false_when_user_id_does_not_match():
    parsed = {"user": {"id": 111}}

    assert is_operator(parsed, "8978823598") is False


def test_is_operator_false_when_user_field_missing():
    assert is_operator({}, "8978823598") is False


def test_is_operator_false_when_user_field_not_a_dict():
    assert is_operator({"user": "not a dict"}, "8978823598") is False


def test_is_operator_false_when_user_has_no_id():
    assert is_operator({"user": {"first_name": "x"}}, "8978823598") is False
