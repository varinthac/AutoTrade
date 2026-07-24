"""Unit tests for common/config.py — .env credential loading and YAML config
loading. No MT5/network dependency."""
from __future__ import annotations

import pytest

from autotrade.common.config import (
    MT5Credentials,
    load_eodhd_api_token,
    load_finnhub_api_key,
    load_fmp_api_key,
    load_mt5_credentials,
    load_telegram_credentials,
    load_webapp_url,
    load_yaml_config,
)

# A path that doesn't exist: load_dotenv() on a missing file is a documented
# safe no-op, so these tests are isolated from the repo's real .env and from
# each other purely via monkeypatch'd os.environ.
_MISSING_ENV_FILE = None


@pytest.fixture
def clean_mt5_env(monkeypatch, tmp_path):
    """Ensure MT5_* vars start unset, and point load_dotenv() at a file that
    doesn't exist so it can't leak the repo's real .env into the test."""
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_mt5_credentials_success(monkeypatch, clean_mt5_env):
    monkeypatch.setenv("MT5_LOGIN", "12345678")
    monkeypatch.setenv("MT5_PASSWORD", "hunter2")
    monkeypatch.setenv("MT5_SERVER", "ICMarketsSC-Demo")

    creds = load_mt5_credentials(clean_mt5_env)

    assert creds == MT5Credentials(
        login=12345678, password="hunter2", server="ICMarketsSC-Demo", terminal_path=None
    )


def test_load_mt5_credentials_login_is_coerced_to_int(monkeypatch, clean_mt5_env):
    monkeypatch.setenv("MT5_LOGIN", "999")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "srv")

    creds = load_mt5_credentials(clean_mt5_env)

    assert creds.login == 999
    assert isinstance(creds.login, int)


def test_load_mt5_credentials_includes_terminal_path_when_set(monkeypatch, clean_mt5_env):
    monkeypatch.setenv("MT5_LOGIN", "1")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "srv")
    monkeypatch.setenv("MT5_TERMINAL_PATH", r"C:\MT5\terminal64.exe")

    creds = load_mt5_credentials(clean_mt5_env)

    assert creds.terminal_path == r"C:\MT5\terminal64.exe"


@pytest.mark.parametrize("missing_var", ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"])
def test_load_mt5_credentials_raises_when_a_required_var_is_missing(monkeypatch, clean_mt5_env, missing_var):
    all_vars = {"MT5_LOGIN": "1", "MT5_PASSWORD": "pw", "MT5_SERVER": "srv"}
    for key, value in all_vars.items():
        if key != missing_var:
            monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER"):
        load_mt5_credentials(clean_mt5_env)


def test_load_mt5_credentials_raises_when_all_vars_missing(clean_mt5_env):
    with pytest.raises(RuntimeError):
        load_mt5_credentials(clean_mt5_env)


def test_load_mt5_credentials_treats_blank_string_as_missing(monkeypatch, clean_mt5_env):
    # .env.example ships with blank values (MT5_LOGIN=) — must not pass validation
    monkeypatch.setenv("MT5_LOGIN", "")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "srv")

    with pytest.raises(RuntimeError):
        load_mt5_credentials(clean_mt5_env)


def test_mt5_credentials_repr_masks_password():
    creds = MT5Credentials(login=1, password="hunter2", server="srv", terminal_path=None)

    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds)
    assert "password='***'" in repr(creds)
    assert "login=1" in repr(creds)
    assert "server='srv'" in repr(creds)


@pytest.fixture
def clean_finnhub_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_finnhub_api_key_returns_key_when_set(monkeypatch, clean_finnhub_env):
    monkeypatch.setenv("FINNHUB_API_KEY", "abc123")

    assert load_finnhub_api_key(clean_finnhub_env) == "abc123"


def test_load_finnhub_api_key_returns_none_when_unset(clean_finnhub_env):
    assert load_finnhub_api_key(clean_finnhub_env) is None


def test_load_finnhub_api_key_treats_blank_string_as_none(monkeypatch, clean_finnhub_env):
    monkeypatch.setenv("FINNHUB_API_KEY", "")

    assert load_finnhub_api_key(clean_finnhub_env) is None


@pytest.fixture
def clean_fmp_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_fmp_api_key_returns_key_when_set(monkeypatch, clean_fmp_env):
    monkeypatch.setenv("FMP_API_KEY", "abc123")

    assert load_fmp_api_key(clean_fmp_env) == "abc123"


def test_load_fmp_api_key_returns_none_when_unset(clean_fmp_env):
    assert load_fmp_api_key(clean_fmp_env) is None


def test_load_fmp_api_key_treats_blank_string_as_none(monkeypatch, clean_fmp_env):
    monkeypatch.setenv("FMP_API_KEY", "")

    assert load_fmp_api_key(clean_fmp_env) is None


@pytest.fixture
def clean_eodhd_env(monkeypatch, tmp_path):
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_eodhd_api_token_returns_token_when_set(monkeypatch, clean_eodhd_env):
    monkeypatch.setenv("EODHD_API_TOKEN", "abc123")

    assert load_eodhd_api_token(clean_eodhd_env) == "abc123"


def test_load_eodhd_api_token_returns_none_when_unset(clean_eodhd_env):
    assert load_eodhd_api_token(clean_eodhd_env) is None


def test_load_eodhd_api_token_treats_blank_string_as_none(monkeypatch, clean_eodhd_env):
    monkeypatch.setenv("EODHD_API_TOKEN", "")

    assert load_eodhd_api_token(clean_eodhd_env) is None


@pytest.fixture
def clean_telegram_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_telegram_credentials_returns_pair_when_both_set(monkeypatch, clean_telegram_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-456")

    assert load_telegram_credentials(clean_telegram_env) == ("bot-token-123", "chat-456")


def test_load_telegram_credentials_returns_none_when_both_unset(clean_telegram_env):
    assert load_telegram_credentials(clean_telegram_env) is None


def test_load_telegram_credentials_returns_none_when_only_token_set(monkeypatch, clean_telegram_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token-123")

    assert load_telegram_credentials(clean_telegram_env) is None


def test_load_telegram_credentials_returns_none_when_only_chat_id_set(monkeypatch, clean_telegram_env):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-456")

    assert load_telegram_credentials(clean_telegram_env) is None


def test_load_telegram_credentials_treats_blank_strings_as_missing(monkeypatch, clean_telegram_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    assert load_telegram_credentials(clean_telegram_env) is None


@pytest.fixture
def clean_webapp_url_env(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    return tmp_path / "does_not_exist.env"


def test_load_webapp_url_returns_url_when_set(monkeypatch, clean_webapp_url_env):
    monkeypatch.setenv("WEBAPP_URL", "https://trade.kylerlink.com")

    assert load_webapp_url(clean_webapp_url_env) == "https://trade.kylerlink.com"


def test_load_webapp_url_returns_none_when_unset(clean_webapp_url_env):
    assert load_webapp_url(clean_webapp_url_env) is None


def test_load_webapp_url_treats_blank_string_as_none(monkeypatch, clean_webapp_url_env):
    monkeypatch.setenv("WEBAPP_URL", "")

    assert load_webapp_url(clean_webapp_url_env) is None


def test_load_yaml_config_reads_real_base_config():
    cfg = load_yaml_config("base")

    assert cfg["global"]["timeframe"] == "H1"
    assert cfg["symbols"]["XAUUSD"] == "XAUUSD"
    assert isinstance(cfg["historical"]["default_days"], int)


def test_load_yaml_config_missing_file_raises_file_not_found_error():
    with pytest.raises(FileNotFoundError):
        load_yaml_config("this_config_does_not_exist")


def test_load_yaml_config_empty_file_returns_empty_dict(tmp_path, monkeypatch):
    import autotrade.common.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")

    assert load_yaml_config("empty") == {}
