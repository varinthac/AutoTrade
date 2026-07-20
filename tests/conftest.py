"""Shared test fixtures.

Autouse, session-wide isolation for `store/`'s SQLite trade journal: any
component that defaults its `journal_db_path`/`db_path` to `None` (e.g. a
`CircuitBreaker`/`ConnectivityWatchdog`/`ShadowLoop`/`WatchmanLoop`/
`ThrottledDemoAdapter` constructed without an explicit path, same convention
as this codebase's other `state_path`/`borderline_log_path` defaults) would
otherwise fall through to `store.models.DEFAULT_DB_PATH`
(`data/db/trade_journal.sqlite`) and silently write real rows into the repo's
actual data directory during a test run. Monkeypatching that default to a
`tmp_path`-based file for every test, regardless of which module ends up
calling `store.journal.record_*`, is far less error-prone than hunting down
every construction site that might indirectly reach it.
"""
from __future__ import annotations

import pytest

from autotrade.store import models


@pytest.fixture(autouse=True)
def _isolate_trade_journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DEFAULT_DB_PATH", tmp_path / "trade_journal.sqlite")
