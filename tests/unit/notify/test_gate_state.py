"""Unit tests for notify/gate_state.py -- promotion/demotion change-detection
logic used by scripts/run_auditor.py's --notify flag. Every test uses a
tmp_path-based state file, never the real default path.

check_promotion_gate_changed()/check_demotion_changed() are read-only (do
NOT persist) -- record_promotion_gate()/record_demotion() persist. This
split exists so run_auditor.py can persist only after a Telegram send
actually succeeds (see gate_state.py's module/function docstrings)."""
from __future__ import annotations

from autotrade.notify import gate_state


# --- promotion gates ------------------------------------------------------


def test_promotion_gate_first_eval_always_changed(tmp_path):
    path = tmp_path / "gate_state.json"

    changed, description = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    assert changed is True
    assert "backtest" in description


def test_promotion_gate_check_alone_does_not_persist(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    changed, _ = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    assert changed is True  # still "changed" -- the first check() never recorded anything


def test_promotion_gate_same_result_twice_is_silent_after_recording(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.check_promotion_gate_changed("backtest", True, state_path=path)
    gate_state.record_promotion_gate("backtest", True, state_path=path)

    changed, _ = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    assert changed is False


def test_promotion_gate_null_to_true_counts_as_changed(tmp_path):
    path = tmp_path / "gate_state.json"
    path.write_text('{"promotion": {"paper": null}}', encoding="utf-8")

    changed, description = gate_state.check_promotion_gate_changed("paper", True, state_path=path)

    assert changed is True
    assert "PENDING" in description
    assert "PASSED" in description


def test_promotion_gate_false_to_true_counts_as_changed(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.record_promotion_gate("live", False, state_path=path)

    changed, description = gate_state.check_promotion_gate_changed("live", True, state_path=path)

    assert changed is True
    assert "FAILED -> PASSED" in description


def test_promotion_gate_missing_state_file_treated_as_no_prior_state(tmp_path):
    path = tmp_path / "does_not_exist.json"

    changed, _ = gate_state.check_promotion_gate_changed("backtest", False, state_path=path)

    assert changed is True


def test_promotion_gate_corrupt_state_file_treated_as_no_prior_state(tmp_path):
    path = tmp_path / "gate_state.json"
    path.write_text("{not valid json", encoding="utf-8")

    changed, _ = gate_state.check_promotion_gate_changed("backtest", False, state_path=path)

    assert changed is True


def test_promotion_gate_independent_gates_tracked_separately(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.record_promotion_gate("backtest", True, state_path=path)

    changed, _ = gate_state.check_promotion_gate_changed("paper", True, state_path=path)

    assert changed is True  # a different gate name has never been recorded before


def test_promotion_gate_persists_across_separate_record_calls(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.record_promotion_gate("backtest", True, state_path=path)
    gate_state.record_promotion_gate("paper", False, state_path=path)

    changed, _ = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    assert changed is False  # backtest's True was persisted despite the intervening paper record


def test_record_promotion_gate_not_reflected_until_recorded(tmp_path):
    # Guards the exact reason this split exists: a check() that observed
    # "changed" must NOT itself make the change stick -- only record_*()
    # does, and only run_auditor.py calls that after a confirmed send.
    path = tmp_path / "gate_state.json"
    gate_state.check_promotion_gate_changed("backtest", True, state_path=path)  # observed only

    changed, _ = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)

    assert changed is True  # never recorded, so it's still "first-ever eval" every time


# --- demotion action -------------------------------------------------------


def test_demotion_first_eval_always_changed_even_for_none_action(tmp_path):
    path = tmp_path / "gate_state.json"

    changed, description = gate_state.check_demotion_changed("none", state_path=path)

    assert changed is True
    assert "Demotion" in description


def test_demotion_same_action_twice_is_silent_after_recording(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.check_demotion_changed("none", state_path=path)
    gate_state.record_demotion("none", state_path=path)

    changed, _ = gate_state.check_demotion_changed("none", state_path=path)

    assert changed is False


def test_demotion_none_to_revert_to_paper_counts_as_changed(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.record_demotion("none", state_path=path)

    changed, description = gate_state.check_demotion_changed("revert_to_paper", state_path=path)

    assert changed is True
    assert "none -> revert_to_paper" in description


def test_demotion_missing_state_file_treated_as_no_prior_state(tmp_path):
    path = tmp_path / "does_not_exist.json"

    changed, _ = gate_state.check_demotion_changed("none", state_path=path)

    assert changed is True


def test_demotion_corrupt_state_file_treated_as_no_prior_state(tmp_path):
    path = tmp_path / "gate_state.json"
    path.write_text("not json at all", encoding="utf-8")

    changed, _ = gate_state.check_demotion_changed("halt_and_investigate", state_path=path)

    assert changed is True


def test_promotion_and_demotion_state_coexist_in_same_file(tmp_path):
    path = tmp_path / "gate_state.json"
    gate_state.record_promotion_gate("backtest", True, state_path=path)
    gate_state.record_demotion("none", state_path=path)

    changed_promotion, _ = gate_state.check_promotion_gate_changed("backtest", True, state_path=path)
    changed_demotion, _ = gate_state.check_demotion_changed("none", state_path=path)

    assert changed_promotion is False
    assert changed_demotion is False
