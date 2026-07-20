"""Environment + YAML config loading.

Credentials come from `.env` (git-ignored); everything else (symbol maps,
timeframe, thresholds) comes from `config/*.yaml` so it can change without
touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@dataclass(frozen=True)
class MT5Credentials:
    login: int
    password: str
    server: str
    terminal_path: str | None

    def __repr__(self) -> str:
        """Mask `password` so logging/printing a `MT5Credentials` (whole
        object, e.g. an accidental `logger.debug(creds)` or an exception
        formatter dumping locals) never leaks it in full."""
        return (
            f"MT5Credentials(login={self.login!r}, password='***', "
            f"server={self.server!r}, terminal_path={self.terminal_path!r})"
        )


def load_mt5_credentials(env_file: Path | None = None) -> MT5Credentials:
    load_dotenv(env_file or REPO_ROOT / ".env")

    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    if not login or not password or not server:
        raise RuntimeError(
            "MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER must be set in .env "
            "(copy .env.example to .env and fill in your demo account details)"
        )

    return MT5Credentials(
        login=int(login),
        password=password,
        server=server,
        terminal_path=os.environ.get("MT5_TERMINAL_PATH") or None,
    )


def load_finnhub_api_key(env_file: Path | None = None) -> str | None:
    """Finnhub API key (finnhub.io) for `council/finnhub_news_calendar.py`'s
    real economic-calendar provider. Unlike `load_mt5_credentials`, this key
    is OPTIONAL -- `council/news_calendar.StubNewsCalendarProvider` remains a
    valid (if conservative) fallback, so a missing/blank key is not a hard
    error, just `None` for the caller to act on (e.g. `scripts/run_shadow_loop.py`
    falls back to the stub with a warning)."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    key = os.environ.get("FINNHUB_API_KEY")
    return key or None


def load_fmp_api_key(env_file: Path | None = None) -> str | None:
    """Financial Modeling Prep API key (financialmodelingprep.com) for a
    prospective `council/fmp_news_calendar.py` economic-calendar provider.
    Same optional/nullable pattern as `load_finnhub_api_key` -- a missing/
    blank key is not a hard error, just `None` for the caller to act on.

    As of 2026-07-20, FMP's `/stable/economic-calendar` endpoint was
    confirmed live to return `HTTP 402 Payment Required` ("Restricted
    Endpoint: This endpoint is not available under your current
    subscription") on the currently-configured `FMP_API_KEY` -- the same
    key succeeds (HTTP 200) against a known free-tier endpoint
    (`/stable/quote`), confirming the key itself is valid and the 402 is
    specifically about this endpoint's tier gating. No `FMPNewsCalendarProvider`
    has been built against this key for that reason -- see
    council/news_calendar.py's module docstring."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    key = os.environ.get("FMP_API_KEY")
    return key or None


def load_eodhd_api_token(env_file: Path | None = None) -> str | None:
    """EODHD API token (eodhd.com) for a prospective
    `council/eodhd_news_calendar.py` economic-calendar provider. Same
    optional/nullable pattern as `load_finnhub_api_key`/`load_fmp_api_key` --
    a missing/blank token is not a hard error, just `None` for the caller to
    act on.

    As of 2026-07-20, EODHD's `/api/economic-events` endpoint was confirmed
    live to return `HTTP 403` ("Only EOD data allowed for free users. Please,
    contact our support team: support@eodhistoricaldata.com") on the
    currently-configured `EODHD_API_TOKEN` -- the same token succeeds
    (HTTP 200) against a known free-tier endpoint (`/api/eod/AAPL.US`),
    confirming the token itself is valid and the 403 is specifically about
    this endpoint's tier gating (EODHD's docs list it as included only in
    the "All-In-One" and "Fundamentals Data Feed" plans). No
    `EODHDNewsCalendarProvider` has been built against this token for that
    reason -- see council/news_calendar.py's module docstring."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    token = os.environ.get("EODHD_API_TOKEN")
    return token or None


def load_yaml_config(name: str) -> dict:
    """Load a YAML file from config/, e.g. load_yaml_config('base')."""
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
