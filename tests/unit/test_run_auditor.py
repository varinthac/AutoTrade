"""Unit tests for scripts/run_auditor.py -- smoke tests for each subcommand
against seeded tmp_path journal DBs, same importlib-loading convention as
tests/unit/test_kill_switch_script.py (scripts/ has no __init__.py). MT5 is
mocked for the `borderline` subcommand's symbol-spec resolution, same
pattern as tests/unit/test_run_shadow_loop.py."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from autotrade.common.config import MT5Credentials
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.store import journal

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_auditor.py"
_spec = importlib.util.spec_from_file_location("run_auditor_script", SCRIPT_PATH)
run_auditor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_auditor
_spec.loader.exec_module(run_auditor)

CREDS = MT5Credentials(login=1, password="pw", server="srv", terminal_path=None)
SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def _write_envelope(path: Path, **overrides) -> Path:
    report = {
        "trade_count": 200, "win_count": 120, "loss_count": 80, "win_rate": 0.6,
        "gross_profit": 5000.0, "gross_loss": 3000.0, "profit_factor": 1.67,
        "total_net_pnl": 2000.0, "avg_r_multiple": 0.5, "max_drawdown_pct": 10.0,
        "profit_factor_excluding_top_5": 1.2,
    }
    envelope = {
        "symbol": "XAUUSD",
        "bar_range": {"start": "2024-01-01 00:00:00", "end": "2026-01-01 00:00:00"},
        "starting_equity": 10000.0,
        "cost_model": {"commission_per_lot": 3.5, "slippage_points": None},
        "cost_model_complete": True,
        "is_out_of_sample": True,
        "risk_voice_modeled": True,
        "watchman_exits_modeled": True,
        "shield_modeled": True,
        "min_lot_risk_cap_pct": 1.5,
        "report": report,
    }
    envelope.update(overrides)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


# --- daily ---

def test_cmd_daily_with_explicit_date_skips_mt5_entirely(tmp_path, capsys, monkeypatch):
    def _fail_if_called(*a, **k):
        raise AssertionError("MT5 must not be touched when --date is given explicitly")

    monkeypatch.setattr(run_auditor, "_server_today", _fail_if_called)

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite")
    rc = run_auditor.cmd_daily(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Daily report" in out
    assert "2026-07-19" in out


def test_cmd_daily_with_explicit_date_and_seeded_trade(tmp_path, capsys):
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )
    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path)
    rc = run_auditor.cmd_daily(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trades: 1" in out


def test_cmd_daily_without_notify_flag_never_calls_notify(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite")
    rc = run_auditor.cmd_daily(args)
    assert rc == 0
    assert calls == []


def test_cmd_daily_notify_sends_on_first_run_and_updates_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    rc = run_auditor.cmd_daily(args)
    assert rc == 0
    assert len(calls) == 1
    assert "2026-07-19" in calls[0]


def test_cmd_daily_notify_dedupes_same_server_date(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    run_auditor.cmd_daily(args)
    rc = run_auditor.cmd_daily(args)  # a second invocation for the SAME server_date

    assert rc == 0
    assert len(calls) == 1  # only the first call actually sent


def test_cmd_daily_notify_sends_again_for_a_new_server_date(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args1 = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    args2 = argparse.Namespace(
        config="base", date="2026-07-20", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    run_auditor.cmd_daily(args1)
    run_auditor.cmd_daily(args2)

    assert len(calls) == 2


def test_cmd_daily_notify_corrupt_state_file_does_not_crash_and_still_sends(tmp_path, capsys, monkeypatch):
    # Fail-open convention (same as notify/gate_state.py): an unreadable
    # dedupe state file must be treated as "never sent" -- so it still
    # notifies -- rather than crashing or silently staying quiet forever.
    state_path = tmp_path / "notify_last_daily.json"
    state_path.write_text("{not valid json at all", encoding="utf-8")
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", state_path)
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    assert len(calls) == 1


def test_cmd_daily_notify_missing_state_dir_does_not_crash_and_still_sends(tmp_path, capsys, monkeypatch):
    # The state file's parent directory not existing yet (first-ever run on
    # a fresh machine) must not crash cmd_daily.
    state_path = tmp_path / "does" / "not" / "exist" / "notify_last_daily.json"
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", state_path)
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    assert len(calls) == 1
    assert state_path.exists()


def test_cmd_daily_notify_message_contains_real_report_content(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path, notify=True)
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    assert len(calls) == 1
    # Not just "was called with some string" -- the seeded trade's actual
    # facts must be present in the notified text, matching what capsys shows
    # was printed for the same report.
    out = capsys.readouterr().out
    assert "Trades: 1" in out
    assert "Trades: 1" in calls[0]
    assert calls[0] == out.rstrip("\n")  # notify() sent the exact same report text that was printed


# --- daily --notify: chart attachments (notify/charts.py) --------------------
# charts.build_equity_curve_png/build_daily_pnl_png are always mocked here --
# this module only needs to prove the CALL/ordering contract (charts sent
# after text succeeds, over the full trade history, best-effort on failure),
# not that matplotlib itself renders correctly (that's tests/unit/notify/
# test_charts.py's job).


def _mock_charts(monkeypatch):
    calls = []

    def _fake_equity(trades):
        calls.append(("equity", trades))
        return b"EQUITY-PNG"

    def _fake_daily_pnl(trades):
        calls.append(("daily_pnl", trades))
        return b"DAILY-PNL-PNG"

    monkeypatch.setattr(run_auditor.charts, "build_equity_curve_png", _fake_equity)
    monkeypatch.setattr(run_auditor.charts, "build_daily_pnl_png", _fake_daily_pnl)
    return calls


def test_cmd_daily_notify_sends_both_charts_after_text_succeeds(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )
    chart_calls = _mock_charts(monkeypatch)
    monkeypatch.setattr(run_auditor, "notify", lambda text: True)
    photo_calls = []
    monkeypatch.setattr(
        run_auditor, "notify_photo",
        lambda png, caption=None: photo_calls.append((png, caption)) or True,
    )

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path, notify=True)
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    assert [name for name, _ in chart_calls] == ["equity", "daily_pnl"]
    assert photo_calls == [(b"EQUITY-PNG", "Equity curve"), (b"DAILY-PNL-PNG", "Daily net P/L")]


def test_cmd_daily_notify_skips_charts_when_text_notify_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )
    chart_calls = _mock_charts(monkeypatch)
    monkeypatch.setattr(run_auditor, "notify", lambda text: False)

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path, notify=True)
    run_auditor.cmd_daily(args)

    assert chart_calls == []  # never even attempted -- the text report itself never sent


def test_cmd_daily_notify_skips_charts_when_no_trades_at_all(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    chart_calls = _mock_charts(monkeypatch)
    monkeypatch.setattr(run_auditor, "notify", lambda text: True)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    run_auditor.cmd_daily(args)

    assert chart_calls == []


def test_cmd_daily_notify_chart_render_failure_does_not_crash_or_unmark_sent(tmp_path, capsys, monkeypatch):
    # Best-effort: a chart RENDERING failure (e.g. matplotlib unavailable on
    # this machine) must not crash cmd_daily or affect the dedup-on-server_date
    # state, since the report TEXT is the notification of record.
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )

    def _raise(trades):
        raise RuntimeError("matplotlib not available")

    monkeypatch.setattr(run_auditor.charts, "build_equity_curve_png", _raise)
    monkeypatch.setattr(run_auditor, "notify", lambda text: True)
    photo_calls = []
    monkeypatch.setattr(
        run_auditor, "notify_photo",
        lambda png, caption=None: photo_calls.append((png, caption)) or True,
    )

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path, notify=True)
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    assert photo_calls == []
    state_path = tmp_path / "notify_last_daily.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["server_date"] == "2026-07-19"


def test_cmd_daily_notify_chart_send_failure_does_not_unmark_sent(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    db_path = tmp_path / "journal.sqlite"
    journal.record_closed_trade(
        symbol="XAUUSD", direction="BUY", entry_time=datetime(2026, 7, 19, 9, 0),
        entry_price=2400.0, exit_time=datetime(2026, 7, 19, 10, 0), exit_price=2410.0,
        exit_reason="take_profit", lot_size=0.1, gross_pnl=100.0, cost=2.0, net_pnl=98.0,
        r_multiple=1.96, recorded_at=datetime(2026, 7, 19, 10, 0, 1), broker_ticket=1, db_path=db_path,
    )
    _mock_charts(monkeypatch)
    monkeypatch.setattr(run_auditor, "notify", lambda text: True)
    monkeypatch.setattr(run_auditor, "notify_photo", lambda png, caption=None: False)

    args = argparse.Namespace(config="base", date="2026-07-19", mode=None, db_path=db_path, notify=True)
    rc = run_auditor.cmd_daily(args)

    assert rc == 0
    state_path = tmp_path / "notify_last_daily.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["server_date"] == "2026-07-19"


def test_cmd_daily_notify_failure_does_not_mark_as_sent_and_retries_next_run(tmp_path, capsys, monkeypatch):
    # Regression guard: a Telegram outage during a daily report must not
    # permanently lose that day's notification -- unlike a genuinely
    # unchanged gate result, a failed SEND must be retried on the next run.
    monkeypatch.setattr(run_auditor, "DEFAULT_NOTIFY_DAILY_STATE_PATH", tmp_path / "notify_last_daily.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or False)

    args = argparse.Namespace(
        config="base", date="2026-07-19", mode=None, db_path=tmp_path / "journal.sqlite", notify=True,
    )
    run_auditor.cmd_daily(args)
    run_auditor.cmd_daily(args)  # same server_date, notify still failing

    assert len(calls) == 2  # both attempts actually tried to send, neither was skipped as "already sent"


def test_cmd_daily_defaults_to_server_date_not_local_today(tmp_path, capsys, monkeypatch):
    # No --date given -- must resolve via _server_today (MT5 server time),
    # never date.today()'s local wall-clock.
    monkeypatch.setattr(run_auditor, "_server_today", lambda cfg: date(2099, 3, 4))

    args = argparse.Namespace(config="base", date=None, mode=None, db_path=tmp_path / "journal.sqlite")
    rc = run_auditor.cmd_daily(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2099-03-04" in out


# --- _server_today ---

def test_server_today_uses_mt5_server_now_not_local_clock(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _fake_session(creds):
        yield

    monkeypatch.setattr(run_auditor, "load_mt5_credentials", lambda: CREDS)
    monkeypatch.setattr(run_auditor, "mt5_session", _fake_session)
    monkeypatch.setattr(run_auditor, "server_now", lambda broker_name: datetime(2099, 3, 4, 15, 30))

    cfg = {"symbols": {"XAUUSD": "XAUUSD"}}
    assert run_auditor._server_today(cfg) == date(2099, 3, 4)


# --- promotion ---

def test_cmd_promotion_gate_backtest_smoke_test(tmp_path, capsys):
    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Backtest -> Paper" in out
    assert "passed: True" in out


def test_cmd_promotion_notify_sends_on_first_evaluation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    assert len(calls) == 1
    assert "backtest" in calls[0]


def test_cmd_promotion_notify_silent_when_result_unchanged(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    run_auditor.cmd_promotion(args)
    rc = run_auditor.cmd_promotion(args)  # same envelope -- same passed=True result again

    assert rc == 0
    assert len(calls) == 1  # only the first (changed) evaluation notified


def test_cmd_promotion_notify_failure_does_not_persist_and_retries_next_run(tmp_path, capsys, monkeypatch):
    # Regression guard: a Telegram outage exactly when a promotion gate
    # changes must not permanently lose that notification -- gate_state
    # must NOT be updated on a failed send, so the next run (even with the
    # same unchanged result) still sees "changed" and retries.
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or False)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    run_auditor.cmd_promotion(args)
    run_auditor.cmd_promotion(args)  # same result again, but notify still failing

    assert len(calls) == 2  # both attempts tried to send -- neither was skipped as "already notified"


def test_cmd_promotion_without_notify_flag_never_calls_notify_or_touches_gate_state(
    tmp_path, capsys, monkeypatch,
):
    state_path = tmp_path / "notify_gate_state.json"
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", state_path)
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None,
        # `notify` attribute intentionally omitted, mirroring argparse when --notify wasn't passed
    )
    rc = run_auditor.cmd_promotion(args)

    assert rc == 0
    assert calls == []
    assert not state_path.exists()  # merely checking (without --notify) must never mutate gate state


def test_cmd_promotion_without_notify_does_not_suppress_a_later_notify_call(tmp_path, monkeypatch):
    # Regression guard: if cmd_promotion ever called gate_state.check_* even
    # when --notify wasn't passed, that would silently consume the
    # "first-ever evaluation is always changed" signal, so a LATER genuine
    # `--notify` run would wrongly see "unchanged" and stay silent.
    state_path = tmp_path / "notify_gate_state.json"
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", state_path)
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args_no_notify = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None,
    )
    run_auditor.cmd_promotion(args_no_notify)
    run_auditor.cmd_promotion(args_no_notify)

    args_with_notify = argparse.Namespace(
        config="base", gate="backtest", envelope=envelope_path, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    run_auditor.cmd_promotion(args_with_notify)

    assert len(calls) == 1  # the first-ever --notify evaluation still notified


def test_cmd_promotion_notify_sends_again_when_result_changes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    passing_envelope = _write_envelope(tmp_path / "passing.json")
    failing_envelope = _write_envelope(tmp_path / "failing.json", report={
        "trade_count": 200, "win_count": 80, "loss_count": 120, "win_rate": 0.4,
        "gross_profit": 1000.0, "gross_loss": 3000.0, "profit_factor": 0.5,
        "total_net_pnl": -2000.0, "avg_r_multiple": -0.5, "max_drawdown_pct": 25.0,
        "profit_factor_excluding_top_5": 0.4,
    })
    args_pass = argparse.Namespace(
        config="base", gate="backtest", envelope=passing_envelope, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    args_fail = argparse.Namespace(
        config="base", gate="backtest", envelope=failing_envelope, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None, notify=True,
    )
    run_auditor.cmd_promotion(args_pass)
    run_auditor.cmd_promotion(args_fail)

    assert len(calls) == 2
    assert "PASSED" in calls[0]
    assert "FAILED" in calls[1]


def test_cmd_promotion_gate_backtest_missing_envelope_returns_error():
    args = argparse.Namespace(
        config="base", gate="backtest", envelope=None, weeks_elapsed=None,
        months_elapsed=None, starting_equity=10000.0, db_path=None,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 1


def test_cmd_promotion_gate_paper_smoke_test_on_empty_db(tmp_path, capsys):
    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", gate="paper", envelope=envelope_path, weeks_elapsed=10,
        months_elapsed=None, starting_equity=10000.0, db_path=tmp_path / "paper.sqlite",
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Paper -> Live ramp" in out
    assert "passed: False" in out  # zero paper trades -- insufficient data


def test_cmd_promotion_gate_live_smoke_test_with_drawdown_halt_event(tmp_path, capsys):
    db_path = tmp_path / "live.sqlite"
    journal.record_anomaly_event(
        timestamp=datetime(2026, 5, 1, 9, 0), event_type="circuit_breaker_trigger",
        details="drawdown halt: equity drawdown from peak 9.00% >= 8.0%; halted, requires manual restart",
        db_path=db_path,
    )
    args = argparse.Namespace(
        config="base", gate="live", envelope=None, weeks_elapsed=None,
        months_elapsed=3, starting_equity=10000.0, db_path=db_path,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Live ramp -> Full size" in out
    assert "FAIL" in out  # heavy_circuit_breaker_triggered criterion fails


def test_cmd_promotion_gate_live_daily_loss_halt_does_not_block(tmp_path, capsys):
    db_path = tmp_path / "live.sqlite"
    journal.record_anomaly_event(
        timestamp=datetime(2026, 5, 1, 9, 0), event_type="circuit_breaker_trigger",
        details="daily_loss_halt: today's loss 2.50% of equity >= limit 2.0%; blocked until next server day",
        db_path=db_path,
    )
    args = argparse.Namespace(
        config="base", gate="live", envelope=None, weeks_elapsed=None,
        months_elapsed=3, starting_equity=10000.0, db_path=db_path,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    cb_line = next(line for line in out.splitlines() if "heavy_circuit_breaker_triggered" in line)
    assert "PASS" in cb_line  # daily_loss_halt alone must not count as "heavy"


def test_cmd_promotion_gate_live_consecutive_loss_halt_does_not_block(tmp_path, capsys):
    db_path = tmp_path / "live.sqlite"
    journal.record_anomaly_event(
        timestamp=datetime(2026, 5, 1, 9, 0), event_type="circuit_breaker_trigger",
        details="consecutive_loss_halt: 3 consecutive losses; halted until 2026-05-02T09:00:00",
        db_path=db_path,
    )
    args = argparse.Namespace(
        config="base", gate="live", envelope=None, weeks_elapsed=None,
        months_elapsed=3, starting_equity=10000.0, db_path=db_path,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    cb_line = next(line for line in out.splitlines() if "heavy_circuit_breaker_triggered" in line)
    assert "PASS" in cb_line  # consecutive_loss_halt alone must not count as "heavy"


def test_cmd_promotion_gate_live_both_light_and_heavy_cb_events_still_blocks(tmp_path, capsys):
    # Both a light (daily_loss_halt) and heavy (drawdown_halt) circuit
    # breaker fired across the live-ramp period -- must still block, and
    # must not merely reflect whichever event happens to be scanned last
    # (any() over the whole range, not a "last event wins" bug).
    db_path = tmp_path / "live.sqlite"
    journal.record_anomaly_event(
        timestamp=datetime(2026, 5, 1, 9, 0), event_type="circuit_breaker_trigger",
        details="daily_loss_halt: today's loss 2.50% of equity >= limit 2.0%; blocked until next server day",
        db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 5, 15, 9, 0), event_type="circuit_breaker_trigger",
        details="drawdown halt: equity drawdown from peak 9.00% >= 8.0%; halted, requires manual restart",
        db_path=db_path,
    )
    journal.record_anomaly_event(
        timestamp=datetime(2026, 6, 1, 9, 0), event_type="circuit_breaker_trigger",
        details="consecutive_loss_halt: 3 consecutive losses; halted until 2026-06-02T09:00:00",
        db_path=db_path,
    )
    args = argparse.Namespace(
        config="base", gate="live", envelope=None, weeks_elapsed=None,
        months_elapsed=3, starting_equity=10000.0, db_path=db_path,
    )
    rc = run_auditor.cmd_promotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    cb_line = next(line for line in out.splitlines() if "heavy_circuit_breaker_triggered" in line)
    assert "FAIL" in cb_line  # drawdown_halt anywhere in the range blocks, regardless of scan order


# --- demotion ---

def test_cmd_demotion_smoke_test_on_empty_db(tmp_path, capsys):
    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite",
    )
    rc = run_auditor.cmd_demotion(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "action: none" in out


def test_cmd_demotion_missing_envelope_returns_error(tmp_path):
    args = argparse.Namespace(
        config="base", envelope=tmp_path / "does_not_exist.json", as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite",
    )
    rc = run_auditor.cmd_demotion(args)
    assert rc == 1


def test_cmd_demotion_notify_sends_on_first_evaluation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite", notify=True,
    )
    rc = run_auditor.cmd_demotion(args)
    assert rc == 0
    assert len(calls) == 1
    assert "none" in calls[0]


def test_cmd_demotion_notify_silent_when_action_unchanged(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite", notify=True,
    )
    run_auditor.cmd_demotion(args)
    rc = run_auditor.cmd_demotion(args)  # same empty DB -- same action="none" again

    assert rc == 0
    assert len(calls) == 1


def test_cmd_demotion_notify_failure_does_not_persist_and_retries_next_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", tmp_path / "notify_gate_state.json")
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or False)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite", notify=True,
    )
    run_auditor.cmd_demotion(args)
    run_auditor.cmd_demotion(args)  # same action again, but notify still failing

    assert len(calls) == 2  # both attempts tried to send -- neither was skipped as "already notified"


def test_cmd_demotion_without_notify_flag_never_calls_notify(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite",
    )
    rc = run_auditor.cmd_demotion(args)
    assert rc == 0
    assert calls == []


def test_cmd_demotion_without_notify_flag_never_touches_gate_state(tmp_path, monkeypatch):
    state_path = tmp_path / "notify_gate_state.json"
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", state_path)
    monkeypatch.setattr(run_auditor, "notify", lambda text: None)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite",
    )
    run_auditor.cmd_demotion(args)

    assert not state_path.exists()


def test_cmd_demotion_without_notify_does_not_suppress_a_later_notify_call(tmp_path, monkeypatch):
    state_path = tmp_path / "notify_gate_state.json"
    monkeypatch.setattr(run_auditor.gate_state, "DEFAULT_STATE_PATH", state_path)
    calls = []
    monkeypatch.setattr(run_auditor, "notify", lambda text: calls.append(text) or True)

    envelope_path = _write_envelope(tmp_path / "envelope.json")
    args_no_notify = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite",
    )
    run_auditor.cmd_demotion(args_no_notify)
    run_auditor.cmd_demotion(args_no_notify)

    args_with_notify = argparse.Namespace(
        config="base", envelope=envelope_path, as_of_date="2026-07-19",
        mode=None, db_path=tmp_path / "live.sqlite", notify=True,
    )
    run_auditor.cmd_demotion(args_with_notify)

    assert len(calls) == 1  # the first-ever --notify evaluation still notified


# --- borderline ---

def test_main_borderline_requires_commission_per_lot_argument(monkeypatch):
    # 0.0 is a legitimate real commission (e.g. a commission-free "Standard"
    # account) so it cannot be a silent argparse default -- same rationale
    # and fix as scripts/run_backtest.py's --commission-per-lot.
    monkeypatch.setattr(sys, "argv", ["run_auditor.py", "borderline"])

    with pytest.raises(SystemExit):
        run_auditor.main()


def test_cmd_borderline_no_cases_logged_yet(tmp_path, capsys):
    args = argparse.Namespace(
        config="base", log_path=tmp_path / "borderline_log.jsonl", commission_per_lot=0.0,
    )
    rc = run_auditor.cmd_borderline(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No borderline cases logged yet" in out


def test_cmd_borderline_skips_symbol_with_no_historical_data_and_stays_mt5_free(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "borderline_log.jsonl"
    case = {
        "symbol": "ZZZUNKNOWN", "as_of_time": "2026-01-06 00:00:00", "hypothetical_direction": "BUY",
        "bull_score": 65, "bear_score": 40, "risk_voice_score": None,
        "order_plan": {"direction": "BUY", "entry": 100.0, "stop_loss": 90.0, "take_profit": 120.0, "stop_distance": 10.0},
        "spread_at_evaluation": 0.0,
    }
    log_path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    def _fail_if_called(*a, **k):
        raise AssertionError("mt5_session must not be entered when no symbol has price data")

    monkeypatch.setattr(run_auditor, "mt5_session", _fail_if_called)

    args = argparse.Namespace(config="base", log_path=log_path, commission_per_lot=0.0)
    rc = run_auditor.cmd_borderline(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Replayed: 0" in out


def test_cmd_borderline_full_replay_with_mocked_mt5(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "borderline_log.jsonl"
    case = {
        "symbol": "XAUUSD", "as_of_time": "2026-01-06 00:00:00", "hypothetical_direction": "BUY",
        "bull_score": 65, "bear_score": 40, "risk_voice_score": None,
        "order_plan": {"direction": "BUY", "entry": 100.0, "stop_loss": 90.0, "take_profit": 120.0, "stop_distance": 10.0},
        "spread_at_evaluation": 0.0,
    }
    log_path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    historical_dir = tmp_path / "historical"
    historical_dir.mkdir()
    times = pd.date_range("2026-01-06 00:00:00", periods=2, freq="h")
    df = pd.DataFrame({
        "time": times, "open": [101, 105], "high": [102, 121], "low": [99, 104],
        "close": [101, 118], "spread": [0, 0],
    })
    df.to_csv(historical_dir / "XAUUSD_H1.csv", index=False)

    monkeypatch.setattr(run_auditor, "HISTORICAL_DIR", historical_dir)
    monkeypatch.setattr(run_auditor, "load_mt5_credentials", lambda: CREDS)

    from contextlib import contextmanager

    @contextmanager
    def _fake_session(creds):
        yield

    monkeypatch.setattr(run_auditor, "mt5_session", _fake_session)
    monkeypatch.setattr(run_auditor, "get_symbol_spec", lambda symbol: SYMBOL)

    args = argparse.Namespace(config="base", log_path=log_path, commission_per_lot=0.0)
    rc = run_auditor.cmd_borderline(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Replayed: 1" in out
    assert "TP: 1" in out
