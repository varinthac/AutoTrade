"""Tests for auditor/borderline.py -- JSONL parsing + expectancy
computation against synthetic BorderlineCase-shaped JSON lines and small
hand-built historical CSV slices (Appendix A §5.4)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from autotrade.auditor.borderline import (
    DEFAULT_MIN_AVG_R_FOR_SIGNAL,
    DEFAULT_MIN_CASES_FOR_SIGNAL,
    build_borderline_expectancy_report,
    load_borderline_cases,
)
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.common.symbol_spec import SymbolSpec

SYMBOL = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=0.01, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)
SYMBOL_SPECS = {"XAUUSD": SYMBOL}
NO_COST = CostModelConfig(commission_per_lot=0.0)


def _case(symbol: str, as_of_time: str, direction="BUY", entry=100.0, stop_loss=90.0, take_profit=120.0, stop_distance=10.0, spread=0.0) -> dict:
    return {
        "symbol": symbol, "as_of_time": as_of_time, "hypothetical_direction": direction,
        "bull_score": 65, "bear_score": 40, "risk_voice_score": None,
        "order_plan": {
            "direction": direction, "entry": entry, "stop_loss": stop_loss,
            "take_profit": take_profit, "stop_distance": stop_distance,
        },
        "spread_at_evaluation": spread,
    }


def _bars(rows: list[dict], start="2026-01-06 00:00:00") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(rows), freq="h")
    return pd.DataFrame([{"time": t, **row} for t, row in zip(times, rows)])


# --- load_borderline_cases ---

def test_load_borderline_cases_parses_jsonl(tmp_path):
    path = tmp_path / "borderline_log.jsonl"
    case1 = _case("XAUUSD", "2026-01-06 00:00:00")
    case2 = _case("EURUSD", "2026-01-06 01:00:00")
    path.write_text(json.dumps(case1) + "\n" + json.dumps(case2) + "\n", encoding="utf-8")

    cases = load_borderline_cases(path)

    assert len(cases) == 2
    assert cases[0]["symbol"] == "XAUUSD"
    assert cases[1]["symbol"] == "EURUSD"


def test_load_borderline_cases_missing_file_returns_empty_list(tmp_path):
    assert load_borderline_cases(tmp_path / "does_not_exist.jsonl") == []


def test_load_borderline_cases_skips_blank_lines(tmp_path):
    path = tmp_path / "borderline_log.jsonl"
    path.write_text(json.dumps(_case("XAUUSD", "2026-01-06 00:00:00")) + "\n\n", encoding="utf-8")
    assert len(load_borderline_cases(path)) == 1


# --- build_borderline_expectancy_report: TP/SL/time-stop classification ---

def test_take_profit_case_is_classified_and_counted():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},  # as-of bar
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 0},  # TP touched
    ])
    cases = [_case("XAUUSD", "2026-01-06 00:00:00")]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48)

    assert report.replayed_count == 1
    assert report.tp_count == 1
    assert report.sl_count == 0
    assert report.time_stop_count == 0
    assert report.unresolved_count == 0
    assert report.avg_net_r == pytest.approx(2.0)


def test_stop_loss_case_is_classified_and_counted():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 95, "high": 96, "low": 85, "close": 90, "spread": 0},  # SL touched
    ])
    cases = [_case("XAUUSD", "2026-01-06 00:00:00")]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48)

    assert report.replayed_count == 1
    assert report.sl_count == 1
    assert report.avg_net_r == pytest.approx(-1.0)


def test_time_stop_case_is_classified_and_counted():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},  # as-of bar
        {"open": 101, "high": 103, "low": 100, "close": 102, "spread": 0},  # window bar, no touch
    ])
    cases = [_case("XAUUSD", "2026-01-06 00:00:00")]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)

    assert report.replayed_count == 1
    assert report.time_stop_count == 1
    assert report.avg_net_r == pytest.approx(0.2)  # (102-100)/10


def test_zero_stop_distance_case_is_skipped_as_malformed_not_counted_or_crashed():
    # A malformed case (stop_distance <= 0) must not raise (ZeroDivisionError
    # or otherwise) and crash the whole batch -- it's skipped like any other
    # malformed case, not counted as replayed OR unresolved.
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 0},
    ])
    bad_case = _case("XAUUSD", "2026-01-06 00:00:00", stop_distance=0.0)
    good_case = _case("XAUUSD", "2026-01-06 00:00:00")

    report = build_borderline_expectancy_report(
        [bad_case, good_case], {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48,
    )

    assert report.replayed_count == 1
    assert report.unresolved_count == 0
    assert report.tp_count == 1


def test_unresolved_case_is_counted_separately_and_excluded_from_avg_net_r():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},  # as-of bar
        {"open": 101, "high": 103, "low": 100, "close": 102, "spread": 0},  # only 1 bar left; window needs more
    ])
    cases = [_case("XAUUSD", "2026-01-06 00:00:00")]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48)

    assert report.replayed_count == 0
    assert report.unresolved_count == 1
    assert report.avg_net_r is None


def test_case_for_symbol_with_no_supplied_price_data_is_skipped_entirely():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 0},
    ])
    cases = [
        _case("XAUUSD", "2026-01-06 00:00:00"),
        _case("GBPUSD", "2026-01-06 00:00:00"),  # no price data supplied for GBPUSD
    ]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48)

    assert report.replayed_count == 1
    assert report.unresolved_count == 0  # skipped, not "unresolved"


def test_as_of_time_not_found_in_supplied_data_is_skipped_entirely():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 0},
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 0},
    ])
    cases = [_case("XAUUSD", "2099-01-01 00:00:00")]

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=48)

    assert report.replayed_count == 0
    assert report.unresolved_count == 0


def test_commission_and_spread_reduce_net_r_for_take_profit_case():
    df = _bars([
        {"open": 101, "high": 102, "low": 99, "close": 101, "spread": 5},
        {"open": 105, "high": 121, "low": 104, "close": 118, "spread": 0},
    ])
    cases = [_case("XAUUSD", "2026-01-06 00:00:00", spread=5.0)]
    cost_model = CostModelConfig(commission_per_lot=2.0)

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, cost_model, time_stop_bars=48)

    # gross_r = 2.0; cost_r = (5*0.01 + 2.0/1.0)/10 = 0.205 -> net_r = 1.795
    assert report.avg_net_r == pytest.approx(1.795)


# --- meets_ai_consideration_signal boundary ---

def _time_stop_case_series(net_r_values: list[float]) -> tuple[pd.DataFrame, list[dict]]:
    """Builds one continuous df where every 2-bar (as-of, window) pair
    resolves as a `time_stop` case with an exactly-controlled `net_r` (no
    spread/commission, so net_r == gross_r == (close - entry)/stop_distance
    for a BUY with entry=100, stop_distance=10)."""
    rows = []
    cases = []
    times = pd.date_range("2026-01-06 00:00:00", periods=2 * len(net_r_values), freq="h")
    for i, net_r in enumerate(net_r_values):
        as_of_time = times[2 * i]
        window_time = times[2 * i + 1]
        rows.append({"time": as_of_time, "open": 101, "high": 102, "low": 99, "close": 101, "spread": 0})
        close = 100.0 + net_r * 10.0
        rows.append({"time": window_time, "open": 100.5, "high": max(101, close + 1), "low": min(99, close - 1), "close": close, "spread": 0})
        cases.append(_case("XAUUSD", str(as_of_time)))
    df = pd.DataFrame(rows)
    return df, cases


def test_meets_ai_consideration_signal_is_none_below_the_case_floor():
    net_rs = [DEFAULT_MIN_AVG_R_FOR_SIGNAL] * (DEFAULT_MIN_CASES_FOR_SIGNAL - 1)
    df, cases = _time_stop_case_series(net_rs)

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)

    assert report.replayed_count == DEFAULT_MIN_CASES_FOR_SIGNAL - 1
    assert report.meets_ai_consideration_signal is None


def test_meets_ai_consideration_signal_true_at_floor_case_count_and_avg_r():
    net_rs = [DEFAULT_MIN_AVG_R_FOR_SIGNAL] * DEFAULT_MIN_CASES_FOR_SIGNAL
    df, cases = _time_stop_case_series(net_rs)

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)

    assert report.replayed_count == DEFAULT_MIN_CASES_FOR_SIGNAL
    assert report.avg_net_r == pytest.approx(DEFAULT_MIN_AVG_R_FOR_SIGNAL)
    assert report.meets_ai_consideration_signal is True


def test_meets_ai_consideration_signal_false_when_avg_r_just_below_threshold():
    net_rs = [DEFAULT_MIN_AVG_R_FOR_SIGNAL - 0.01] * DEFAULT_MIN_CASES_FOR_SIGNAL
    df, cases = _time_stop_case_series(net_rs)

    report = build_borderline_expectancy_report(cases, {"XAUUSD": df}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)

    assert report.replayed_count == DEFAULT_MIN_CASES_FOR_SIGNAL
    assert report.meets_ai_consideration_signal is False


def test_meets_ai_consideration_signal_exact_boundary_0_2000_true_0_1999_false():
    net_rs_at_boundary = [0.2000] * DEFAULT_MIN_CASES_FOR_SIGNAL
    df_at, cases_at = _time_stop_case_series(net_rs_at_boundary)
    report_at = build_borderline_expectancy_report(cases_at, {"XAUUSD": df_at}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)
    assert report_at.avg_net_r == pytest.approx(0.2000)
    assert report_at.meets_ai_consideration_signal is True

    net_rs_below = [0.1999] * DEFAULT_MIN_CASES_FOR_SIGNAL
    df_below, cases_below = _time_stop_case_series(net_rs_below)
    report_below = build_borderline_expectancy_report(cases_below, {"XAUUSD": df_below}, SYMBOL_SPECS, NO_COST, time_stop_bars=1)
    assert report_below.avg_net_r == pytest.approx(0.1999)
    assert report_below.meets_ai_consideration_signal is False
