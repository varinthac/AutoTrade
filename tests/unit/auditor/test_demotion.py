"""Tests for auditor/demotion.py -- Appendix A §5.3's demotion rules, each
rule individually plus the halt-vs-revert precedence tie-break."""
from __future__ import annotations

from datetime import date, datetime

from autotrade.auditor.demotion import DemotionThresholds, evaluate_demotion
from autotrade.backtest.report import BacktestReport
from autotrade.store.models import TradeRecord

THRESHOLDS = DemotionThresholds()

BACKTEST_REPORT = BacktestReport(
    trade_count=200, win_count=110, loss_count=90, win_rate=0.55, gross_profit=5000.0,
    gross_loss=2000.0, profit_factor=1.5, total_net_pnl=3000.0, avg_r_multiple=0.6,
    max_drawdown_pct=10.0, profit_factor_excluding_top_5=1.1,
)


def _record(net_pnl: float, r_multiple: float, exit_time: datetime) -> TradeRecord:
    return TradeRecord(
        symbol="XAUUSD", direction="BUY", entry_time=exit_time, entry_price=100.0,
        exit_time=exit_time, exit_price=100.0, exit_reason="take_profit", lot_size=1.0,
        gross_pnl=net_pnl, cost=0.0, net_pnl=net_pnl, r_multiple=r_multiple, recorded_at=exit_time,
    )


def test_no_records_yields_no_action():
    result = evaluate_demotion([], BACKTEST_REPORT, date(2026, 3, 15), THRESHOLDS)
    assert result.action == "none"
    assert result.reasons == []


def test_two_consecutive_calendar_months_of_net_loss_reverts_to_paper():
    records = [
        _record(-100.0, -1.0, datetime(2026, 2, 5)),
        _record(-50.0, -0.5, datetime(2026, 2, 20)),
        _record(-80.0, -0.8, datetime(2026, 3, 10)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, date(2026, 3, 15), THRESHOLDS)
    assert result.action == "revert_to_paper"
    assert any("consecutive calendar months" in r for r in result.reasons)


def test_one_losing_month_and_one_profitable_month_does_not_trigger():
    records = [
        _record(-100.0, -1.0, datetime(2026, 2, 5)),
        _record(200.0, 2.0, datetime(2026, 3, 10)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, date(2026, 3, 15), THRESHOLDS)
    assert result.action == "none"


def test_missing_data_for_one_of_the_two_months_does_not_trigger():
    # Only March has any trades at all -- Feb is simply unknown, not a loss.
    # Break even (not < 1.0 PF) so the separate rolling-60-day rule doesn't
    # also fire and mask what this test is actually checking.
    records = [
        _record(-100.0, -1.0, datetime(2026, 3, 10)),
        _record(100.0, 1.0, datetime(2026, 3, 11)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, date(2026, 3, 15), THRESHOLDS)
    assert result.action == "none"


def test_rolling_60_day_profit_factor_below_1_reverts_to_paper():
    as_of = date(2026, 6, 1)
    records = [
        _record(-100.0, -1.0, datetime(2026, 5, 1)),
        _record(-100.0, -1.0, datetime(2026, 5, 10)),
        _record(50.0, 0.5, datetime(2026, 5, 20)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)
    assert result.action == "revert_to_paper"
    assert any("rolling 60-server-day profit factor" in r for r in result.reasons)


def test_two_consecutive_calendar_months_of_loss_triggers_without_rolling_pf_also_firing():
    # Proves the two "recent window" concepts are genuinely independent
    # (not accidentally sharing a date range): May+June are both net-losing
    # calendar months (revert condition), but a large profitable trade in
    # April -- inside the rolling 60-day window but NOT one of the two
    # months the calendar rule inspects -- pulls the rolling-60-day PF
    # comfortably above 1.0, so only the calendar-months reason should fire.
    as_of = date(2026, 6, 1)
    records = [
        _record(1000.0, 10.0, datetime(2026, 4, 10)),  # in the 60-day window, but not in {May, June}
        _record(-20.0, -0.2, datetime(2026, 5, 1)),
        _record(-20.0, -0.2, datetime(2026, 5, 10)),
        _record(-20.0, -0.2, datetime(2026, 5, 20)),
        _record(-20.0, -0.2, datetime(2026, 5, 28)),
        _record(-20.0, -0.2, datetime(2026, 6, 1)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)

    assert result.action == "revert_to_paper"
    assert any("consecutive calendar months" in r for r in result.reasons)
    assert not any("rolling" in r for r in result.reasons)
    assert len(result.reasons) == 1


def test_rolling_60_day_window_is_exactly_60_days_not_61():
    # as_of=2026-06-01; a true 60-day window is [2026-04-03, 2026-06-01]
    # inclusive. A trade sitting exactly one day further back
    # (2026-04-02, "day 61-back") must be excluded -- if the window were
    # off-by-one (61 days), this large winning trade would flip the profit
    # factor and the rule would wrongly NOT fire.
    as_of = date(2026, 6, 1)
    records = [
        _record(1000.0, 10.0, datetime(2026, 4, 2)),  # day 61-back -- must be excluded
        _record(-100.0, -1.0, datetime(2026, 5, 1)),
        _record(-100.0, -1.0, datetime(2026, 5, 10)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)
    assert result.action == "revert_to_paper"
    assert any("rolling 60-server-day profit factor" in r for r in result.reasons)


def test_rolling_60_day_window_includes_the_boundary_day_itself():
    # A trade exactly on the window's start boundary (2026-04-03, "day
    # 60-back") must be INCLUDED -- confirms the fix isn't off-by-one in the
    # other direction either.
    as_of = date(2026, 6, 1)
    records = [
        _record(1000.0, 10.0, datetime(2026, 4, 3)),  # day 60-back -- must be included
        _record(-100.0, -1.0, datetime(2026, 5, 1)),
        _record(-100.0, -1.0, datetime(2026, 5, 10)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)
    # 1000 profit included alongside 200 loss -- PF = 5.0, well above 1.0.
    assert not any("rolling 60-server-day profit factor" in r for r in result.reasons)


def test_rolling_60_day_window_excludes_older_trades():
    as_of = date(2026, 6, 1)
    records = [
        _record(-1000.0, -10.0, datetime(2026, 1, 1)),  # outside the 60-day window
        _record(100.0, 1.0, datetime(2026, 5, 20)),
    ]
    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)
    assert result.action == "none"


def test_win_rate_divergence_above_15_points_at_50_plus_trades_halts_and_investigates():
    # backtest win_rate=0.55; live win_rate=0.30 (~11 wins/50-ish trades) diverges by 25pp.
    records = []
    for i in range(15):
        records.append(_record(100.0, 1.0, datetime(2026, 4, 1 + i % 28)))
    for i in range(35):
        records.append(_record(-50.0, -0.5, datetime(2026, 4, 1 + i % 28)))
    assert len(records) == 50

    result = evaluate_demotion(records, BACKTEST_REPORT, date(2026, 6, 1), THRESHOLDS)
    assert result.action == "halt_and_investigate"
    assert any("diverges from backtest" in r for r in result.reasons)


def test_win_rate_divergence_below_50_trades_does_not_trigger():
    records = [_record(-50.0, -0.5, datetime(2026, 4, 1)) for _ in range(49)]
    result = evaluate_demotion(records, BACKTEST_REPORT, date(2026, 6, 1), THRESHOLDS)
    assert result.action == "none"


def test_precedence_halt_and_investigate_wins_over_revert_and_surfaces_both_reasons():
    as_of = date(2026, 6, 1)
    records = []
    # 2 consecutive losing calendar months (May and June-to-date) -- revert condition.
    for i in range(30):
        records.append(_record(-10.0, -0.1, datetime(2026, 5, 1 + i % 28)))
    for i in range(5):
        records.append(_record(-10.0, -0.1, datetime(2026, 6, 1)))
    # >=50 trades total with a win rate far below backtest's 0.55 -- halt condition.
    for i in range(20):
        records.append(_record(-20.0, -0.2, datetime(2026, 5, 1 + i % 28)))

    result = evaluate_demotion(records, BACKTEST_REPORT, as_of, THRESHOLDS)

    assert result.action == "halt_and_investigate"
    assert any("diverges from backtest" in r for r in result.reasons)
    assert any("consecutive calendar months" in r for r in result.reasons)
