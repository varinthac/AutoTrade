"""Unit tests for store/models.py -- get_engine()'s per-resolved-path caching
(a fresh Engine/connection pool on every call, never disposed, was a slow
resource leak in the long-running orchestrator)."""
from __future__ import annotations

from autotrade.store import models
from autotrade.store.models import get_engine


def test_get_engine_returns_same_engine_for_the_same_explicit_path(tmp_path):
    db_path = tmp_path / "trade_journal.sqlite"

    first = get_engine(db_path)
    second = get_engine(db_path)

    assert first is second


def test_get_engine_returns_different_engines_for_different_paths(tmp_path):
    first = get_engine(tmp_path / "a.sqlite")
    second = get_engine(tmp_path / "b.sqlite")

    assert first is not second


def test_get_engine_default_and_explicit_default_path_share_one_engine(tmp_path, monkeypatch):
    # A db_path=None call and an explicit call using that SAME resolved
    # default path must share one cached engine, not two -- keyed by the
    # RESOLVED path, not the raw db_path argument (guards the common
    # db_path=None default case).
    default_path = tmp_path / "trade_journal.sqlite"
    monkeypatch.setattr(models, "DEFAULT_DB_PATH", default_path)

    via_default = get_engine()
    via_explicit = get_engine(default_path)

    assert via_default is via_explicit
