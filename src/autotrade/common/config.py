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


def load_yaml_config(name: str) -> dict:
    """Load a YAML file from config/, e.g. load_yaml_config('base')."""
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
