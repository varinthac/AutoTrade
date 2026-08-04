"""Tests for auditor/promotion.py -- Appendix A §5.2's three promotion
gates, exercised at their documented pass/fail/insufficient-data
boundaries."""
from __future__ import annotations

from datetime import datetime

import pytest

from autotrade.auditor.backtest_results import BacktestReportEnvelope
from autotrade.auditor.metrics import TradeMetrics
from autotrade.auditor.promotion import (
    PromotionThresholds,
    evaluate_backtest_to_paper_gate,
    evaluate_live_ramp_to_full_gate,
    evaluate_paper_to_live_gate,
)
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.report import BacktestReport

THRESHOLDS = PromotionThresholds()


def _envelope(
    profit_factor=1.35, max_drawdown_pct=10.0, trade_count=210,
    profit_factor_excluding_top_5=1.1, cost_model_complete=True, is_out_of_sample=True,
    risk_voice_modeled=True, watchman_exits_modeled=True, shield_modeled=True,
    news_protection_modeled=True, risk_voice_news_modeled=True,
) -> BacktestReportEnvelope:
    report = BacktestReport(
        trade_count=trade_count, win_count=120, loss_count=trade_count - 120,
        win_rate=0.55, gross_profit=5000.0, gross_loss=2000.0, profit_factor=profit_factor,
        total_net_pnl=3000.0, avg_r_multiple=0.6, max_drawdown_pct=max_drawdown_pct,
        profit_factor_excluding_top_5=profit_factor_excluding_top_5,
    )
    return BacktestReportEnvelope(
        symbol="XAUUSD", bar_range_start=datetime(2024, 1, 1), bar_range_end=datetime(2026, 1, 1),
        starting_equity=10_000.0, cost_model=CostModelConfig(commission_per_lot=3.5, slippage_points=None),
        cost_model_complete=cost_model_complete, is_out_of_sample=is_out_of_sample,
        risk_voice_modeled=risk_voice_modeled, watchman_exits_modeled=watchman_exits_modeled,
        shield_modeled=shield_modeled, news_protection_modeled=news_protection_modeled,
        risk_voice_news_modeled=risk_voice_news_modeled, min_lot_risk_cap_pct=1.5, report=report,
    )


# --- Gate 1: Backtest -> Paper ---

def test_gate1_passes_when_every_criterion_clears():
    result = evaluate_backtest_to_paper_gate(_envelope(), THRESHOLDS)
    assert result.passed is True
    assert all(c.passed for c in result.criteria)


def test_gate1_fails_outright_when_cost_model_incomplete_regardless_of_other_numbers():
    result = evaluate_backtest_to_paper_gate(_envelope(cost_model_complete=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "cost_model_complete"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_when_not_out_of_sample_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only is_out_of_sample=False.
    result = evaluate_backtest_to_paper_gate(_envelope(is_out_of_sample=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "is_out_of_sample"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_reporting_both_hard_fail_criteria_when_both_missing():
    result = evaluate_backtest_to_paper_gate(
        _envelope(is_out_of_sample=False, cost_model_complete=False), THRESHOLDS,
    )
    assert result.passed is False
    names = {c.name for c in result.criteria}
    assert names == {"is_out_of_sample", "cost_model_complete"}


def test_gate1_fails_outright_when_risk_voice_not_modeled_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only risk_voice_modeled=False.
    result = evaluate_backtest_to_paper_gate(_envelope(risk_voice_modeled=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "risk_voice_modeled"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_when_watchman_exits_not_modeled_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only watchman_exits_modeled=False.
    result = evaluate_backtest_to_paper_gate(_envelope(watchman_exits_modeled=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "watchman_exits_modeled"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_when_shield_not_modeled_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only shield_modeled=False.
    result = evaluate_backtest_to_paper_gate(_envelope(shield_modeled=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "shield_modeled"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_when_news_protection_not_modeled_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only news_protection_modeled=False.
    result = evaluate_backtest_to_paper_gate(_envelope(news_protection_modeled=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "news_protection_modeled"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_when_risk_voice_news_not_modeled_regardless_of_otherwise_passing_numbers():
    # Every number here would otherwise pass -- only risk_voice_news_modeled=False.
    result = evaluate_backtest_to_paper_gate(_envelope(risk_voice_news_modeled=False), THRESHOLDS)
    assert result.passed is False
    assert len(result.criteria) == 1
    assert result.criteria[0].name == "risk_voice_news_modeled"
    assert result.criteria[0].passed is False


def test_gate1_fails_outright_reporting_all_seven_hard_fail_criteria_when_all_missing():
    result = evaluate_backtest_to_paper_gate(
        _envelope(
            is_out_of_sample=False, cost_model_complete=False,
            risk_voice_modeled=False, watchman_exits_modeled=False, shield_modeled=False,
            news_protection_modeled=False, risk_voice_news_modeled=False,
        ),
        THRESHOLDS,
    )
    assert result.passed is False
    names = {c.name for c in result.criteria}
    assert names == {
        "is_out_of_sample", "cost_model_complete", "risk_voice_modeled",
        "watchman_exits_modeled", "shield_modeled", "news_protection_modeled",
        "risk_voice_news_modeled",
    }


def test_gate1_profit_factor_boundary_1_30_passes_1_29_fails():
    passing = evaluate_backtest_to_paper_gate(_envelope(profit_factor=1.30), THRESHOLDS)
    failing = evaluate_backtest_to_paper_gate(_envelope(profit_factor=1.29), THRESHOLDS)
    assert passing.passed is True
    assert failing.passed is False


def test_gate1_profit_factor_exact_boundary_1_3_passes_1_2999_fails():
    passing = evaluate_backtest_to_paper_gate(_envelope(profit_factor=1.3), THRESHOLDS)
    failing = evaluate_backtest_to_paper_gate(_envelope(profit_factor=1.2999), THRESHOLDS)
    assert passing.passed is True
    assert failing.passed is False


def test_gate1_max_drawdown_boundary_15_00_passes_15_01_fails():
    passing = evaluate_backtest_to_paper_gate(_envelope(max_drawdown_pct=15.00), THRESHOLDS)
    failing = evaluate_backtest_to_paper_gate(_envelope(max_drawdown_pct=15.01), THRESHOLDS)
    assert passing.passed is True
    assert failing.passed is False


def test_gate1_trade_count_boundary_200_passes_199_fails():
    passing = evaluate_backtest_to_paper_gate(_envelope(trade_count=200), THRESHOLDS)
    failing = evaluate_backtest_to_paper_gate(_envelope(trade_count=199), THRESHOLDS)
    assert passing.passed is True
    assert failing.passed is False


def test_gate1_profit_factor_excluding_top_5_must_exceed_1_0_not_just_equal():
    equal_to_one = evaluate_backtest_to_paper_gate(_envelope(profit_factor_excluding_top_5=1.0), THRESHOLDS)
    above_one = evaluate_backtest_to_paper_gate(_envelope(profit_factor_excluding_top_5=1.01), THRESHOLDS)
    assert equal_to_one.passed is False
    assert above_one.passed is True


def test_gate1_profit_factor_excluding_top_5_exact_boundary_1_0_fails_1_0001_passes():
    equal_to_one = evaluate_backtest_to_paper_gate(_envelope(profit_factor_excluding_top_5=1.0), THRESHOLDS)
    just_above = evaluate_backtest_to_paper_gate(_envelope(profit_factor_excluding_top_5=1.0001), THRESHOLDS)
    assert equal_to_one.passed is False
    assert just_above.passed is True


def test_gate1_insufficient_data_when_trade_count_zero_marks_criteria_none_not_false():
    result = evaluate_backtest_to_paper_gate(
        _envelope(profit_factor=None, max_drawdown_pct=None, trade_count=0, profit_factor_excluding_top_5=None),
        THRESHOLDS,
    )
    assert result.passed is False
    pf_criterion = next(c for c in result.criteria if c.name == "profit_factor")
    assert pf_criterion.passed is None


# --- Gate 2: Paper -> Live ramp ---

def _paper_metrics(profit_factor=1.25, max_drawdown_pct=8.0, win_rate=0.55, avg_r_multiple=0.6) -> TradeMetrics:
    return TradeMetrics(
        trade_count=120, win_count=66, loss_count=54, win_rate=win_rate,
        gross_profit=5000.0, gross_loss=2000.0, profit_factor=profit_factor,
        profit_factor_excluding_top_5=1.1, max_drawdown_pct=max_drawdown_pct,
        avg_r_multiple=avg_r_multiple, total_net_pnl=3000.0,
    )


BACKTEST_REPORT = BacktestReport(
    trade_count=210, win_count=120, loss_count=90, win_rate=0.55, gross_profit=5000.0,
    gross_loss=2000.0, profit_factor=1.35, total_net_pnl=3000.0, avg_r_multiple=0.6,
    max_drawdown_pct=10.0, profit_factor_excluding_top_5=1.1,
)


def test_gate2_passes_with_enough_trades_and_weeks():
    result = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS)
    assert result.passed is True


def test_gate2_fails_when_weeks_below_the_absolute_floor_even_with_enough_trades():
    result = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=7, trade_count=150, thresholds=THRESHOLDS)
    assert result.passed is False
    sample = next(c for c in result.criteria if c.name == "sample_size")
    assert sample.passed is False


def test_gate2_fewer_than_100_trades_in_16_weeks_still_passes_sample_size_with_non_blocking_recommendation():
    result = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=16, trade_count=40, thresholds=THRESHOLDS)
    sample = next(c for c in result.criteria if c.name == "sample_size")
    assert sample.passed is True
    assert sample.note is not None
    assert result.recommendation is not None
    assert "small paper sample" in result.recommendation.lower()


def test_gate2_fails_when_neither_trade_count_nor_weeks_fast_track_met():
    result = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=10, trade_count=40, thresholds=THRESHOLDS)
    sample = next(c for c in result.criteria if c.name == "sample_size")
    assert sample.passed is False
    assert result.passed is False


def test_gate2_sample_size_compound_condition_all_four_quadrants():
    # (trades>=100, weeks>=16) quadrant matrix, always with weeks>=8 (the
    # floor) so only the OR-branch is being exercised here.
    both_true = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=20, trade_count=150, thresholds=THRESHOLDS)
    only_trades_true = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS)
    only_weeks_true = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=16, trade_count=40, thresholds=THRESHOLDS)
    neither_true = evaluate_paper_to_live_gate(_paper_metrics(), BACKTEST_REPORT, weeks_elapsed=10, trade_count=40, thresholds=THRESHOLDS)

    def _sample(result):
        return next(c for c in result.criteria if c.name == "sample_size")

    assert _sample(both_true).passed is True
    assert _sample(only_trades_true).passed is True
    assert _sample(only_weeks_true).passed is True
    assert _sample(neither_true).passed is False

    # Reaching via trades>=100 alone (both_true here has trades>=100 too, so
    # use only_trades_true) should NOT carry the "reached via weeks-elapsed
    # branch with a small sample" note -- that note is specific to the
    # weeks-only path.
    assert _sample(both_true).note is None
    assert _sample(only_trades_true).note is None
    assert _sample(only_weeks_true).note is not None


def test_gate2_win_rate_deviation_29_percent_passes_31_percent_fails():
    # backtest win_rate=0.55; 29% relative deviation -> 0.55*0.71=0.3905; 31% -> 0.55*0.69=0.3795
    passing = evaluate_paper_to_live_gate(
        _paper_metrics(win_rate=0.55 * 1.29), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    failing = evaluate_paper_to_live_gate(
        _paper_metrics(win_rate=0.55 * 1.31), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    assert passing.passed is True
    assert failing.passed is False


def test_gate2_avg_r_deviation_29_percent_passes_31_percent_fails():
    passing = evaluate_paper_to_live_gate(
        _paper_metrics(avg_r_multiple=0.6 * 1.29), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    failing = evaluate_paper_to_live_gate(
        _paper_metrics(avg_r_multiple=0.6 * 1.31), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    assert passing.passed is True
    assert failing.passed is False


def test_gate2_win_rate_deviation_exact_30_percent_passes_30_01_percent_fails():
    # backtest win_rate=0.55; +30% relative deviation -> 0.715 exactly; +30.01% -> 0.55*1.3001.
    passing = evaluate_paper_to_live_gate(
        _paper_metrics(win_rate=0.715), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    failing = evaluate_paper_to_live_gate(
        _paper_metrics(win_rate=0.55 * 1.3001), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    win_rate_dev_passing = next(c for c in passing.criteria if c.name == "win_rate_deviation_pct")
    win_rate_dev_failing = next(c for c in failing.criteria if c.name == "win_rate_deviation_pct")
    assert win_rate_dev_passing.passed is True
    assert win_rate_dev_failing.passed is False


def test_gate2_avg_r_deviation_exact_30_percent_passes_30_01_percent_fails():
    # backtest avg_r_multiple=0.6; 0.7799999999999999 lands the computed
    # relative deviation at ~29.999999999999993 (<= 30 boundary, passes) --
    # picked empirically since naive 0.6*1.30/0.78 land a hair over 30.0 due
    # to float rounding in the division itself; 0.6*1.3001 is unambiguously
    # over the boundary either way.
    passing = evaluate_paper_to_live_gate(
        _paper_metrics(avg_r_multiple=0.7799999999999999), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    failing = evaluate_paper_to_live_gate(
        _paper_metrics(avg_r_multiple=0.6 * 1.3001), BACKTEST_REPORT, weeks_elapsed=10, trade_count=120, thresholds=THRESHOLDS,
    )
    avg_r_dev_passing = next(c for c in passing.criteria if c.name == "avg_r_deviation_pct")
    avg_r_dev_failing = next(c for c in failing.criteria if c.name == "avg_r_deviation_pct")
    assert avg_r_dev_passing.passed is True
    assert avg_r_dev_failing.passed is False


def test_gate2_profit_factor_and_drawdown_boundaries():
    passing_pf = evaluate_paper_to_live_gate(_paper_metrics(profit_factor=1.2), BACKTEST_REPORT, 10, 120, THRESHOLDS)
    failing_pf = evaluate_paper_to_live_gate(_paper_metrics(profit_factor=1.19), BACKTEST_REPORT, 10, 120, THRESHOLDS)
    assert passing_pf.passed is True
    assert failing_pf.passed is False

    passing_dd = evaluate_paper_to_live_gate(_paper_metrics(max_drawdown_pct=12.0), BACKTEST_REPORT, 10, 120, THRESHOLDS)
    failing_dd = evaluate_paper_to_live_gate(_paper_metrics(max_drawdown_pct=12.01), BACKTEST_REPORT, 10, 120, THRESHOLDS)
    assert passing_dd.passed is True
    assert failing_dd.passed is False


def test_gate2_insufficient_data_when_zero_paper_trades():
    empty_metrics = TradeMetrics(
        trade_count=0, win_count=0, loss_count=0, win_rate=None, gross_profit=0.0, gross_loss=0.0,
        profit_factor=None, profit_factor_excluding_top_5=None, max_drawdown_pct=None,
        avg_r_multiple=None, total_net_pnl=0.0,
    )
    result = evaluate_paper_to_live_gate(empty_metrics, BACKTEST_REPORT, weeks_elapsed=0, trade_count=0, thresholds=THRESHOLDS)
    assert result.passed is False
    pf_criterion = next(c for c in result.criteria if c.name == "profit_factor")
    assert pf_criterion.passed is None


# --- Gate 3: Live ramp -> Full size ---

def _live_metrics(profit_factor=1.25, avg_r_multiple=0.3) -> TradeMetrics:
    return TradeMetrics(
        trade_count=80, win_count=44, loss_count=36, win_rate=0.55, gross_profit=4000.0,
        gross_loss=1800.0, profit_factor=profit_factor, profit_factor_excluding_top_5=1.1,
        max_drawdown_pct=9.0, avg_r_multiple=avg_r_multiple, total_net_pnl=2200.0,
    )


def test_gate3_passes_when_every_criterion_clears():
    result = evaluate_live_ramp_to_full_gate(_live_metrics(), months_elapsed=3, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    assert result.passed is True


def test_gate3_fails_when_months_below_threshold():
    result = evaluate_live_ramp_to_full_gate(_live_metrics(), months_elapsed=2, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    assert result.passed is False


def test_gate3_heavy_circuit_breaker_blocks_regardless_of_other_numbers():
    result = evaluate_live_ramp_to_full_gate(_live_metrics(), months_elapsed=3, heavy_cb_triggered=True, thresholds=THRESHOLDS)
    assert result.passed is False
    cb_criterion = next(c for c in result.criteria if c.name == "heavy_circuit_breaker_triggered")
    assert cb_criterion.passed is False


def test_gate3_avg_net_r_must_be_strictly_positive():
    zero = evaluate_live_ramp_to_full_gate(_live_metrics(avg_r_multiple=0.0), months_elapsed=3, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    positive = evaluate_live_ramp_to_full_gate(_live_metrics(avg_r_multiple=0.01), months_elapsed=3, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    assert zero.passed is False
    assert positive.passed is True


def test_gate3_profit_factor_boundary():
    passing = evaluate_live_ramp_to_full_gate(_live_metrics(profit_factor=1.2), months_elapsed=3, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    failing = evaluate_live_ramp_to_full_gate(_live_metrics(profit_factor=1.19), months_elapsed=3, heavy_cb_triggered=False, thresholds=THRESHOLDS)
    assert passing.passed is True
    assert failing.passed is False
