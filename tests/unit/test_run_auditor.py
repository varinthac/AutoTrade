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


# --- borderline ---

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
