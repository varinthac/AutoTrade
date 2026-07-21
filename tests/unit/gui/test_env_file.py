"""Unit tests for autotrade.gui.env_file -- pure .env parse/edit/write logic,
no tkinter involved. Never touches the repo's real .env/.env.example;
ensure_env_exists tests always use tmp_path."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from autotrade.gui.env_file import (
    SECRET_KEYS,
    ensure_env_exists,
    parse_env,
    validate_field,
    write_env_atomic,
)

REAL_ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"

SYNTHETIC_FIXTURE = (
    "# leading comment\n"
    "\n"
    "FOO=bar\n"
    "# a comment about BAZ\n"
    "BAZ=\n"
    "\n"
    "QUX=has=equals=in=it\n"
)


# --- round-trip -------------------------------------------------------


def test_round_trip_real_env_example():
    text = REAL_ENV_EXAMPLE.read_text(encoding="utf-8")

    assert parse_env(text).render() == text


def test_round_trip_synthetic_fixture():
    assert parse_env(SYNTHETIC_FIXTURE).render() == SYNTHETIC_FIXTURE


# --- set() on an existing key -------------------------------------------


def test_set_existing_key_only_changes_that_lines_value():
    doc = parse_env(SYNTHETIC_FIXTURE)

    doc.set("FOO", "changed")

    expected = SYNTHETIC_FIXTURE.replace("FOO=bar", "FOO=changed")
    assert doc.render() == expected


def test_set_preserves_value_containing_equals_signs():
    doc = parse_env(SYNTHETIC_FIXTURE)

    doc.set("QUX", "new=value=here")

    assert doc.get("QUX") == "new=value=here"
    assert "QUX=new=value=here" in doc.render()


# --- set() on a new key --------------------------------------------------


def test_set_new_key_appends_at_end():
    doc = parse_env(SYNTHETIC_FIXTURE)

    doc.set("NEW_KEY", "new_value")

    assert doc.get("NEW_KEY") == "new_value"
    rendered = doc.render()
    assert rendered.startswith(SYNTHETIC_FIXTURE.rstrip("\n"))
    assert rendered.rstrip("\n").endswith("NEW_KEY=new_value")


def test_set_new_key_on_text_without_trailing_newline():
    text = "FOO=bar"
    doc = parse_env(text)

    doc.set("NEW_KEY", "1")

    assert doc.render() == "FOO=bar\nNEW_KEY=1"


# --- validate_field --------------------------------------------------------


def test_validate_mt5_login_accepts_digits():
    assert validate_field("MT5_LOGIN", "12345") is None


def test_validate_mt5_login_rejects_non_digits():
    assert validate_field("MT5_LOGIN", "abc") is not None


def test_validate_mt5_login_allows_blank():
    assert validate_field("MT5_LOGIN", "") is None


def test_validate_field_other_keys_always_none():
    assert validate_field("TELEGRAM_BOT_TOKEN", "") is None
    assert validate_field("TELEGRAM_BOT_TOKEN", "anything at all") is None


# --- SECRET_KEYS -------------------------------------------------------


def test_secret_keys_contains_exactly_the_expected_set():
    assert SECRET_KEYS == frozenset({
        "MT5_PASSWORD",
        "ANTHROPIC_API_KEY",
        "FINNHUB_API_KEY",
        "FMP_API_KEY",
        "EODHD_API_TOKEN",
        "RAPIDAPI_KEY",
        "ALPHAVANTAGE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    })


def test_secret_keys_excludes_non_secret_fields():
    non_secret = {"MT5_LOGIN", "MT5_SERVER", "MT5_TERMINAL_PATH", "TELEGRAM_CHAT_ID"}
    assert SECRET_KEYS.isdisjoint(non_secret)


# --- ensure_env_exists ---------------------------------------------------


def test_ensure_env_exists_creates_copy_when_absent(tmp_path):
    example_path = tmp_path / ".env.example"
    example_path.write_text("FOO=bar\n", encoding="utf-8")
    env_path = tmp_path / ".env"

    created = ensure_env_exists(env_path, example_path)

    assert created is True
    assert env_path.read_text(encoding="utf-8") == "FOO=bar\n"


def test_ensure_env_exists_leaves_existing_file_untouched(tmp_path):
    example_path = tmp_path / ".env.example"
    example_path.write_text("FOO=bar\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=already_customized\n", encoding="utf-8")

    created = ensure_env_exists(env_path, example_path)

    assert created is False
    assert env_path.read_text(encoding="utf-8") == "FOO=already_customized\n"


# --- write_env_atomic ----------------------------------------------------


def test_write_env_atomic_writes_content_and_leaves_no_temp_file(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=old\n", encoding="utf-8")

    write_env_atomic(env_path, "FOO=new\n")

    assert env_path.read_text(encoding="utf-8") == "FOO=new\n"
    assert not (tmp_path / ".env.tmp").exists()
    assert list(tmp_path.iterdir()) == [env_path]


def test_write_env_atomic_leaves_original_untouched_on_replace_failure(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=old\n", encoding="utf-8")

    def _raise_replace(*_args, **_kwargs):
        raise OSError("simulated disk-full/lock failure")

    monkeypatch.setattr(os, "replace", _raise_replace)

    with pytest.raises(OSError):
        write_env_atomic(env_path, "FOO=new\n")

    assert env_path.read_text(encoding="utf-8") == "FOO=old\n"
