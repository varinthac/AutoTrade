"""The Auditor's promotion gates (trading_system_summary_v2.md Appendix A
§5.2 "เกณฑ์เลื่อนขั้น"): Backtest -> Paper, Paper -> Live ramp, Live ramp ->
Full size. Every threshold is injected via `PromotionThresholds` -- this
module never reads `config/base.yaml` itself, same `RiskVoiceConfig`-style
injection convention `council/risk_voice.py` already uses (the caller,
`scripts/run_auditor.py`, loads the YAML `auditor:` block and constructs
`PromotionThresholds` from it).

**`CriterionResult.passed=None`** means "insufficient data to evaluate yet"
(e.g. zero trades so far), distinct from a real fail -- honestly the current
state of every gate today, since there is no accumulated live/paper history.
A gate's own `GateResult.passed` is a hard bool: `None` on any criterion
counts as "gate not yet passed", same as an outright `False`, but the
criterion-level distinction is preserved for a human reading the report.

**Resolved interpretations used here** (trading_system_summary_v2.md's
prose leaves these underspecified; the following are this phase's fixed
decisions, not automatically re-derivable from the spec text alone):
- Gate 1's "still profitable excluding top-5 trades" = profit factor
  computed over the trade set with the 5 largest-winning trades removed
  must be **> 1.0** (not just a raw net-P&L-positive check).
- Gate 2's "ผล paper ไม่ต่างจาก backtest เกิน 30%" is a **relative**
  percentage deviation (`|paper - backtest| / |backtest| * 100`), not a
  percentage-POINTS difference -- contrast with `auditor/demotion.py`'s win
  rate divergence rule, which the spec explicitly phrases in percentage
  points instead.
- Gate 3's "ไม่มี circuit breaker ระดับหนักถูก trigger" = only a
  `drawdown_halt` (`risk/circuit_breaker.py`'s heaviest, manual-restart-only
  tier) counts as "heavy" -- `daily_loss`/`consecutive_loss` triggers do not
  block this gate. The caller (`scripts/run_auditor.py`) is responsible for
  turning a day's `AnomalyEventRecord`s into the `heavy_cb_triggered` bool
  this module consumes (see that script for the substring heuristic used
  to distinguish which tier fired, since `AnomalyEventRecord.event_type` is
  generically `circuit_breaker_trigger` for all tiers).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autotrade.auditor.backtest_results import BacktestReportEnvelope
from autotrade.auditor.metrics import TradeMetrics
from autotrade.backtest.report import BacktestReport


@dataclass(frozen=True)
class CriterionResult:
    name: str
    passed: bool | None
    actual: Any
    threshold: str
    note: str | None = None


@dataclass(frozen=True)
class GateResult:
    passed: bool
    criteria: list[CriterionResult]
    recommendation: str | None = None


@dataclass(frozen=True)
class PromotionThresholds:
    """Every §5.2 number, populated from `config/base.yaml`'s `auditor:`
    block by the caller -- defaults here mirror the spec's own conservative
    starting values, used only if a caller constructs this without
    overriding (e.g. in tests)."""

    # Gate 1: Backtest -> Paper
    backtest_min_profit_factor: float = 1.3
    backtest_max_drawdown_pct: float = 15.0
    backtest_min_trade_count: int = 200
    backtest_min_profit_factor_excluding_top_5: float = 1.0

    # Gate 2: Paper -> Live ramp
    paper_min_trade_count: int = 100
    paper_min_weeks_fast_track: float = 16.0
    paper_min_weeks_floor: float = 8.0
    paper_min_profit_factor: float = 1.2
    paper_max_drawdown_pct: float = 12.0
    paper_max_deviation_pct: float = 30.0

    # Gate 3: Live ramp -> Full size
    live_ramp_min_months: float = 3.0
    live_ramp_min_profit_factor: float = 1.2
    live_ramp_min_avg_net_r: float = 0.0


def _at_least(name: str, actual: float | None, threshold: float, note: str | None = None) -> CriterionResult:
    if actual is None:
        return CriterionResult(name, None, actual, f">= {threshold}", note or "insufficient data")
    return CriterionResult(name, actual >= threshold, actual, f">= {threshold}", note)


def _at_most(name: str, actual: float | None, threshold: float, note: str | None = None) -> CriterionResult:
    if actual is None:
        return CriterionResult(name, None, actual, f"<= {threshold}", note or "insufficient data")
    return CriterionResult(name, actual <= threshold, actual, f"<= {threshold}", note)


def _greater_than(name: str, actual: float | None, threshold: float, note: str | None = None) -> CriterionResult:
    if actual is None:
        return CriterionResult(name, None, actual, f"> {threshold}", note or "insufficient data")
    return CriterionResult(name, actual > threshold, actual, f"> {threshold}", note)


def evaluate_backtest_to_paper_gate(
    report_envelope: BacktestReportEnvelope, thresholds: PromotionThresholds,
) -> GateResult:
    """Gate 1. Per Appendix A §5.2's "Out-of-sample: ... backtest ที่ไม่มี
    cost model = ไม่นับ": if `report_envelope.is_out_of_sample` is `False` OR
    `report_envelope.cost_model_complete` is `False`, the whole gate fails
    outright and every other criterion is skipped -- an in-sample backtest
    (possibly overfit) or one without real costs modeled tells us nothing
    about promotion-worthiness. `is_out_of_sample` is a human-set flag on the
    envelope (`scripts/run_backtest.py`'s `--out-of-sample`, default
    `False`) -- this hard-fail is what makes forgetting that flag fail
    loudly instead of silently passing an in-sample run."""
    hard_fail_criteria = []
    if not report_envelope.is_out_of_sample:
        hard_fail_criteria.append(CriterionResult(
            name="is_out_of_sample", passed=False, actual=False, threshold="True",
            note=(
                "This gate only applies to an out-of-sample backtest run (Appendix A §5.2) -- "
                "an in-sample result cannot rule out overfitting. Re-run scripts/run_backtest.py "
                "with --out-of-sample once the data is genuinely held-out."
            ),
        ))
    if not report_envelope.cost_model_complete:
        hard_fail_criteria.append(CriterionResult(
            name="cost_model_complete", passed=False, actual=False, threshold="True",
            note=(
                "Backtest without a complete cost model (real commission + minimum-1-spread "
                "slippage + overnight swap/rollover, since 2026-07-25) does not count toward any "
                "promotion decision (Appendix A §5.2) -- every other criterion is skipped. Re-run "
                "scripts/run_backtest.py with --swap-long-per-lot/--swap-short-per-lot if this run "
                "omitted them."
            ),
        ))
    if not report_envelope.risk_voice_modeled:
        hard_fail_criteria.append(CriterionResult(
            name="risk_voice_modeled", passed=False, actual=False, threshold="True",
            note=(
                "Backtest without Risk Voice modeled only exercised Bull/Bear scoring + the "
                "Decision Matrix, not the full veto gate live trading applies -- its trade "
                "count/profit factor are not representative. Re-run scripts/run_backtest.py "
                "(risk_voice_cfg is now wired in by default) before this gate can be evaluated."
            ),
        ))
    if not report_envelope.watchman_exits_modeled:
        hard_fail_criteria.append(CriterionResult(
            name="watchman_exits_modeled", passed=False, actual=False, threshold="True",
            note=(
                "Backtest without Watchman's exit management modeled only ever exercised fixed "
                "SL/TP/end-of-data exits, never breakeven/ATR-trailing/structure-invalidation/"
                "time-stop (EXP-002, experiments/experiments_log.md) -- its trade count/profit "
                "factor are not representative. Re-run scripts/run_backtest.py (watchman_cfg is "
                "now wired in by default) before this gate can be evaluated."
            ),
        ))
    if not report_envelope.shield_modeled:
        hard_fail_criteria.append(CriterionResult(
            name="shield_modeled", passed=False, actual=False, threshold="True",
            note=(
                "Backtest without Shield modeled never exercised the duplicate-signal cooldown "
                "(rule 6), the one Shield rule with real effect in this single-position engine "
                "(experiments/experiments_log.md's 2026-07-22 Shield NOTE) -- its trade count is "
                "not representative. Re-run scripts/run_backtest.py (shield_cfg is now wired in "
                "by default) before this gate can be evaluated."
            ),
        ))
    if hard_fail_criteria:
        return GateResult(
            passed=False, criteria=hard_fail_criteria,
            recommendation=(
                "Re-run the backtest out-of-sample with a complete cost model before this gate "
                "can be evaluated."
            ),
        )

    report = report_envelope.report
    criteria = [
        _at_least("profit_factor", report.profit_factor, thresholds.backtest_min_profit_factor),
        _at_most("max_drawdown_pct", report.max_drawdown_pct, thresholds.backtest_max_drawdown_pct),
        _at_least("trade_count", report.trade_count, thresholds.backtest_min_trade_count),
        _greater_than(
            "profit_factor_excluding_top_5", report.profit_factor_excluding_top_5,
            thresholds.backtest_min_profit_factor_excluding_top_5,
            note="Profit factor with the 5 largest-winning trades removed -- must still be "
                 "profitable without relying on them (Appendix A §5.2).",
        ),
    ]
    passed = all(c.passed for c in criteria)
    recommendation = None if passed else "Backtest does not yet meet the Backtest -> Paper gate; see failing/insufficient criteria."
    return GateResult(passed=passed, criteria=criteria, recommendation=recommendation)


def _relative_deviation_pct(paper_value: float | None, backtest_value: float | None) -> float | None:
    if paper_value is None or backtest_value is None:
        return None
    if backtest_value == 0:
        return 0.0 if paper_value == 0 else float("inf")
    return abs(paper_value - backtest_value) / abs(backtest_value) * 100


def _sample_size_criterion(
    trade_count: int, weeks_elapsed: float, thresholds: PromotionThresholds,
) -> tuple[CriterionResult, str | None]:
    floor_met = weeks_elapsed >= thresholds.paper_min_weeks_floor
    fast_track_met = (
        trade_count >= thresholds.paper_min_trade_count or weeks_elapsed >= thresholds.paper_min_weeks_fast_track
    )
    passed = floor_met and fast_track_met

    note = None
    recommendation = None
    if passed and trade_count < thresholds.paper_min_trade_count:
        note = (
            "Reached via the weeks-elapsed branch with fewer than "
            f"{thresholds.paper_min_trade_count} trades -- Appendix A §5.2's guidance for this "
            "low-frequency system: accept the small sample rather than fail this criterion, but "
            "extend live-ramp caution instead (keep risk at the lower live-ramp percentage longer)."
        )
        recommendation = (
            "Accept small paper sample; extend live ramp (keep 0.25% risk longer) rather than "
            "treat this as a full pass."
        )

    criterion = CriterionResult(
        name="sample_size", passed=passed,
        actual=f"{trade_count} trades, {weeks_elapsed} weeks elapsed",
        threshold=(
            f">= {thresholds.paper_min_trade_count} trades OR >= {thresholds.paper_min_weeks_fast_track} weeks "
            f"(always requiring >= {thresholds.paper_min_weeks_floor} weeks)"
        ),
        note=note,
    )
    return criterion, recommendation


def evaluate_paper_to_live_gate(
    paper_metrics: TradeMetrics,
    backtest_report: BacktestReport,
    weeks_elapsed: int,
    trade_count: int,
    thresholds: PromotionThresholds,
) -> GateResult:
    """Gate 2. `weeks_elapsed`/`trade_count` are passed explicitly (rather
    than read off `paper_metrics.trade_count`) so the caller can account for
    the paper period independent of exactly which metrics object shape is
    on hand."""
    sample_criterion, sample_recommendation = _sample_size_criterion(trade_count, weeks_elapsed, thresholds)
    criteria = [
        sample_criterion,
        _at_least("profit_factor", paper_metrics.profit_factor, thresholds.paper_min_profit_factor),
        _at_most("max_drawdown_pct", paper_metrics.max_drawdown_pct, thresholds.paper_max_drawdown_pct),
        _at_most(
            "win_rate_deviation_pct",
            _relative_deviation_pct(paper_metrics.win_rate, backtest_report.win_rate),
            thresholds.paper_max_deviation_pct,
            note="Relative deviation of paper's win rate from the backtest's (Appendix A §5.2).",
        ),
        _at_most(
            "avg_r_deviation_pct",
            _relative_deviation_pct(paper_metrics.avg_r_multiple, backtest_report.avg_r_multiple),
            thresholds.paper_max_deviation_pct,
            note="Relative deviation of paper's average R multiple from the backtest's (Appendix A §5.2).",
        ),
    ]
    passed = all(c.passed for c in criteria)
    recommendation = sample_recommendation
    if not passed and recommendation is None:
        recommendation = "Paper trading does not yet meet the Paper -> Live ramp gate; see failing/insufficient criteria."
    return GateResult(passed=passed, criteria=criteria, recommendation=recommendation)


def evaluate_live_ramp_to_full_gate(
    live_metrics: TradeMetrics,
    months_elapsed: int,
    heavy_cb_triggered: bool,
    thresholds: PromotionThresholds,
) -> GateResult:
    """Gate 3. `heavy_cb_triggered` = only a `drawdown_halt` tier fired (see
    module docstring's "Resolved interpretations" note) -- the caller is
    responsible for that classification."""
    criteria = [
        _at_least("months_elapsed", months_elapsed, thresholds.live_ramp_min_months),
        _at_least("profit_factor", live_metrics.profit_factor, thresholds.live_ramp_min_profit_factor),
        CriterionResult(
            name="heavy_circuit_breaker_triggered", passed=not heavy_cb_triggered, actual=heavy_cb_triggered,
            threshold="False",
            note="Only a drawdown_halt (the heaviest circuit-breaker tier) counts here -- "
                 "daily_loss/consecutive_loss triggers do not block this gate (Appendix A §5.2, "
                 "resolved interpretation).",
        ),
        _greater_than(
            "avg_net_r", live_metrics.avg_r_multiple, thresholds.live_ramp_min_avg_net_r,
            note="Average realized R multiple across live trades -- slippage must not have made "
                 "expectancy negative.",
        ),
    ]
    passed = all(c.passed for c in criteria)
    recommendation = None if passed else "Live ramp does not yet meet the Live ramp -> Full size gate; see failing/insufficient criteria."
    return GateResult(passed=passed, criteria=criteria, recommendation=recommendation)
