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


def load_rapidapi_key(env_file: Path | None = None) -> str | None:
    """RapidAPI key (rapidapi.com) for a prospective
    `council/rapidapi_news_calendar.py` economic-calendar provider, backed by
    the "Ultimate Economic Calendar" API (provider "toplistaai"). Same
    optional/nullable pattern as `load_finnhub_api_key`/`load_fmp_api_key`/
    `load_eodhd_api_token` -- a missing/blank key is not a hard error, just
    `None` for the caller to act on.

    As of 2026-07-20, this API's `/economic-events/tradingview` endpoint was
    confirmed live to return `HTTP 402 Payment Required` ("DEPLOYMENT_DISABLED",
    a Vercel-platform-level error, not a RapidAPI subscription message) on
    the currently-configured `RAPIDAPI_KEY`, using the exact query shape from
    the provider's own documented example. The same key succeeds with a
    proper RapidAPI-gateway response (HTTP 404 `{"message": "Endpoint '...'
    does not exist"}`, not an auth/subscription error) against other paths on
    the same host, confirming the key and gateway routing are valid and the
    402 is specifically this endpoint's backend being disabled by the API
    provider. No `RapidAPINewsCalendarProvider` has been built against this
    key for that reason -- see council/news_calendar.py's module docstring."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    key = os.environ.get("RAPIDAPI_KEY")
    return key or None


def load_alphavantage_api_key(env_file: Path | None = None) -> str | None:
    """Alpha Vantage API key (alphavantage.co) -- checked as a fifth
    candidate for a real `council/*_news_calendar.py` economic-calendar
    provider. Same optional/nullable pattern as `load_finnhub_api_key`/
    `load_fmp_api_key`/`load_eodhd_api_token`/`load_rapidapi_key` -- a
    missing/blank key is not a hard error, just `None` for the caller to act
    on.

    As of 2026-07-20, Alpha Vantage's own documentation
    (alphavantage.co/documentation) was reviewed and confirmed to have NO
    forward-looking macro economic-events calendar with a timestamp and an
    impact/importance rating -- unlike the previous four candidates (all of
    which had the right *kind* of endpoint but were paywalled), Alpha
    Vantage simply does not offer this *kind* of data at all. Its
    "Economic Indicators" section (`REAL_GDP`, `CPI`, `FEDERAL_FUNDS_RATE`,
    `UNEMPLOYMENT`, `NONFARM_PAYROLL`, etc.) is individual historical/
    current time series (sourced from FRED), not a calendar of upcoming
    releases -- there is no "impact" field or forward release schedule
    anywhere in the docs. `EARNINGS_CALENDAR`/`IPO_CALENDAR` are
    company-specific (earnings/IPO events per stock symbol), not macro
    economic events. `NEWS_SENTIMENT` is a sentiment-scored feed of
    already-published news articles filtered by ticker/topic/time range,
    not a schedule of known-in-advance upcoming events. No
    `AlphaVantageNewsCalendarProvider` has been built against this key for
    that reason -- see council/news_calendar.py's module docstring."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    return key or None


def load_telegram_credentials(env_file: Path | None = None) -> tuple[str, str] | None:
    """Telegram bot token + chat id (t.me/BotFather) for `notify/telegram.py`'s
    best-effort notification feature. Same optional/nullable pattern as
    `load_finnhub_api_key` -- BOTH `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
    must be set (non-blank) for this to return anything; otherwise `None`,
    which `notify()` treats as "not configured" (silent no-op, not an
    error)."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    return token, chat_id


def load_webapp_url(env_file: Path | None = None) -> str | None:
    """Public HTTPS URL (Cloudflare Tunnel, see `ops/cloudflared_tunnel.ps1`)
    the read-only trade dashboard is reachable at -- used only to add a Web
    App button to `notify/telegram_control.py`'s `/status` inline keyboard.
    Same optional/nullable pattern as `load_finnhub_api_key` -- a missing/
    blank URL is not a hard error, the button is simply omitted. Note this
    loader has no bearing on whether the dashboard is SAFE to expose there --
    that's `dashboard/app.py`'s own `initData`-verifying auth gate (see
    `dashboard/webapp_auth.py`), independent of whether this button exists."""
    load_dotenv(env_file or REPO_ROOT / ".env")
    url = os.environ.get("WEBAPP_URL")
    return url or None


def load_yaml_config(name: str) -> dict:
    """Load a YAML file from config/, e.g. load_yaml_config('base')."""
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
