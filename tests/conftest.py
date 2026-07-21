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


@pytest.fixture(autouse=True)
def _block_real_telegram_notifications(monkeypatch):
    """`store/journal.py`'s `record_anomaly_event`, `watchman/loop.py`'s
    `_write_trade_record`, `orchestrator/shadow_loop.py`'s order-placed path,
    and `scripts/kill_switch.py`'s `do_activate` all call `notify()` as a
    plain (non-mocked-away) side effect in most existing tests. Set (not
    delete) both vars to a blank string: `common/config.py`'s
    `load_telegram_credentials` calls `load_dotenv(..., override=False)`
    (the library default), which only fills in variables ABSENT from
    `os.environ` -- deleting them would let a real local `.env`'s values
    reappear via that same `load_dotenv` call, while setting them blank here
    means they're already "present" and `load_dotenv` leaves them alone.
    This guarantees `load_telegram_credentials()` returns `None` and
    `notify()` no-ops before ever reaching a real HTTP call, regardless of
    what a developer's real local `.env` happens to contain -- the test
    suite must never depend on, or risk triggering, a real Telegram send."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
