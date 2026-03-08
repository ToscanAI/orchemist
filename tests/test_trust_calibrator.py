"""tests/test_trust_calibrator.py — Tests for Issue #4.2.2: TrustCalibrator.

Covers all acceptance criteria and edge cases for TrustCalibrator:
- Constructor validation (alpha, conservative, aggressive, bootstrap_threshold)
- compute_threshold: bootstrap lock, post-bootstrap interpolation, clamping
- update_after_run: outcome validation, profile creation, EMA computation,
  counter increments, threshold derivation, DB persistence, adjustment log
- Module exports via __init__.py
- OUTCOME_SCORES and VALID_OUTCOMES constants

Test classes:
    TestOutcomeConstants            — OUTCOME_SCORES / VALID_OUTCOMES shape
    TestTrustCalibratorConstructor  — parameter validation, attribute storage
    TestComputeThreshold            — bootstrap lock, interpolation, clamping
    TestUpdateAfterRun              — EMA, counters, DB writes, return dict
    TestUpdateAfterRunEdgeCases     — clamping at 0/1, unknown outcome
    TestModuleExports               — __init__.py exports TrustCalibrator
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.trust import (
    OUTCOME_SCORES,
    VALID_OUTCOMES,
    TrustCalibrator,
    TrustProfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return a fresh in-memory Database with all migrations applied."""
    return Database(":memory:")


def _make_calibrator(
    repo: str = "owner/repo",
    template_id: str = "coding-pipeline-v1",
    task_type: str = "bugfix",
    alpha: float = 0.1,
    conservative: float = 0.98,
    aggressive: float = 0.7,
    bootstrap_threshold: int = 10,
) -> TrustCalibrator:
    return TrustCalibrator(
        repo=repo,
        template_id=template_id,
        task_type=task_type,
        alpha=alpha,
        conservative=conservative,
        aggressive=aggressive,
        bootstrap_threshold=bootstrap_threshold,
    )


def _profile_data(
    repo: str = "owner/repo",
    template_id: str = "coding-pipeline-v1",
    task_type: str = "bugfix",
    **overrides: Any,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "repo": repo,
        "template_id": template_id,
        "task_type": task_type,
        "auto_merge_threshold": 0.85,
        "human_review_threshold": 0.70,
        "trust_score": 0.5,
        "total_runs": 0,
        "successful_merges": 0,
        "regressions": 0,
        "reverted_prs": 0,
        "last_run_at": None,
    }
    base.update(overrides)
    return base


# ===========================================================================
# TestOutcomeConstants
# ===========================================================================


class TestOutcomeConstants:
    """OUTCOME_SCORES and VALID_OUTCOMES sanity checks."""

    def test_outcome_scores_has_four_entries(self) -> None:
        assert len(OUTCOME_SCORES) == 4

    def test_run_success_score_is_positive(self) -> None:
        assert OUTCOME_SCORES["run_success"] > 0

    def test_regression_score_is_negative(self) -> None:
        assert OUTCOME_SCORES["regression"] < 0

    def test_revert_score_is_negative(self) -> None:
        assert OUTCOME_SCORES["revert"] < 0

    def test_human_override_reject_score_is_negative(self) -> None:
        assert OUTCOME_SCORES["human_override_reject"] < 0

    def test_regression_is_more_severe_than_human_override(self) -> None:
        # Regression penalty should be harsher than human override reject
        assert OUTCOME_SCORES["regression"] < OUTCOME_SCORES["human_override_reject"]

    def test_valid_outcomes_matches_outcome_scores_keys(self) -> None:
        assert VALID_OUTCOMES == frozenset(OUTCOME_SCORES.keys())

    def test_all_four_outcomes_in_valid_outcomes(self) -> None:
        expected = {"run_success", "regression", "revert", "human_override_reject"}
        assert expected == VALID_OUTCOMES


# ===========================================================================
# TestTrustCalibratorConstructor
# ===========================================================================


class TestTrustCalibratorConstructor:
    """Constructor stores parameters and validates constraints."""

    def test_stores_repo(self) -> None:
        c = _make_calibrator(repo="my/repo")
        assert c.repo == "my/repo"

    def test_stores_template_id(self) -> None:
        c = _make_calibrator(template_id="pipeline-v2")
        assert c.template_id == "pipeline-v2"

    def test_stores_task_type(self) -> None:
        c = _make_calibrator(task_type="feature")
        assert c.task_type == "feature"

    def test_stores_alpha(self) -> None:
        c = _make_calibrator(alpha=0.2)
        assert c.alpha == pytest.approx(0.2)

    def test_stores_conservative(self) -> None:
        c = _make_calibrator(conservative=0.95)
        assert c.conservative == pytest.approx(0.95)

    def test_stores_aggressive(self) -> None:
        c = _make_calibrator(aggressive=0.8)
        assert c.aggressive == pytest.approx(0.8)

    def test_stores_bootstrap_threshold(self) -> None:
        c = _make_calibrator(bootstrap_threshold=5)
        assert c.bootstrap_threshold == 5

    # --- default values ---

    def test_default_alpha(self) -> None:
        c = TrustCalibrator("r", "t", "x")
        assert c.alpha == pytest.approx(0.1)

    def test_default_conservative(self) -> None:
        c = TrustCalibrator("r", "t", "x")
        assert c.conservative == pytest.approx(0.98)

    def test_default_aggressive(self) -> None:
        c = TrustCalibrator("r", "t", "x")
        assert c.aggressive == pytest.approx(0.7)

    def test_default_bootstrap_threshold(self) -> None:
        c = TrustCalibrator("r", "t", "x")
        assert c.bootstrap_threshold == 10

    # --- validation: alpha ---

    def test_alpha_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            TrustCalibrator("r", "t", "x", alpha=0.0)

    def test_alpha_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            TrustCalibrator("r", "t", "x", alpha=-0.1)

    def test_alpha_one_accepted(self) -> None:
        c = TrustCalibrator("r", "t", "x", alpha=1.0)
        assert c.alpha == pytest.approx(1.0)

    def test_alpha_greater_than_one_raises(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            TrustCalibrator("r", "t", "x", alpha=1.1)

    # --- validation: conservative / aggressive ---

    def test_aggressive_equal_conservative_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustCalibrator("r", "t", "x", conservative=0.8, aggressive=0.8)

    def test_aggressive_greater_than_conservative_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustCalibrator("r", "t", "x", conservative=0.7, aggressive=0.9)

    def test_conservative_greater_than_one_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustCalibrator("r", "t", "x", conservative=1.1, aggressive=0.7)

    def test_aggressive_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustCalibrator("r", "t", "x", conservative=0.98, aggressive=-0.1)

    # --- validation: bootstrap_threshold ---

    def test_bootstrap_threshold_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="bootstrap_threshold"):
            TrustCalibrator("r", "t", "x", bootstrap_threshold=-1)

    def test_bootstrap_threshold_zero_accepted(self) -> None:
        c = TrustCalibrator("r", "t", "x", bootstrap_threshold=0)
        assert c.bootstrap_threshold == 0


# ===========================================================================
# TestComputeThreshold
# ===========================================================================


class TestComputeThreshold:
    """compute_threshold: bootstrap lock, interpolation, and clamping."""

    def test_bootstrap_locked_at_conservative(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        # Only 9 successful merges — still in bootstrap
        threshold = c.compute_threshold(trust_score=1.0, successful_merges=9)
        assert threshold == pytest.approx(0.98)

    def test_bootstrap_locked_at_zero_merges(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        threshold = c.compute_threshold(trust_score=0.5, successful_merges=0)
        assert threshold == pytest.approx(0.98)

    def test_post_bootstrap_zero_trust_equals_conservative(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        # trust_score=0 → threshold = conservative - 0*(diff) = conservative
        threshold = c.compute_threshold(trust_score=0.0, successful_merges=10)
        assert threshold == pytest.approx(0.98)

    def test_post_bootstrap_full_trust_equals_aggressive(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        # trust_score=1 → threshold = conservative - 1*(conservative - aggressive) = aggressive
        threshold = c.compute_threshold(trust_score=1.0, successful_merges=10)
        assert threshold == pytest.approx(0.7)

    def test_post_bootstrap_half_trust_interpolated(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        # trust_score=0.5 → threshold = 0.98 - 0.5*(0.98-0.7) = 0.98 - 0.14 = 0.84
        threshold = c.compute_threshold(trust_score=0.5, successful_merges=10)
        assert threshold == pytest.approx(0.84)

    def test_threshold_exactly_at_bootstrap_boundary(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        # Exactly 10 successful merges — no longer in bootstrap
        threshold = c.compute_threshold(trust_score=1.0, successful_merges=10)
        assert threshold == pytest.approx(0.7)  # not 0.98

    def test_threshold_clamped_to_zero_minimum(self) -> None:
        # Pathological: conservative=0, aggressive=0 → threshold=0
        c = TrustCalibrator("r", "t", "x", conservative=0.5, aggressive=0.0,
                            bootstrap_threshold=0)
        # trust_score=2.0 would make raw negative — clamped to 0
        # trust_score is clamped in update_after_run, but compute_threshold is pure
        threshold = c.compute_threshold(trust_score=2.0, successful_merges=10)
        assert threshold >= 0.0

    def test_threshold_clamped_to_one_maximum(self) -> None:
        c = TrustCalibrator("r", "t", "x", conservative=0.98, aggressive=0.7,
                            bootstrap_threshold=0)
        # trust_score < 0 could produce > 1; clamp ensures ≤ 1
        threshold = c.compute_threshold(trust_score=-1.0, successful_merges=10)
        assert threshold <= 1.0

    def test_bootstrap_threshold_zero_never_locked(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=0)
        # Even with 0 successful merges, bootstrap_threshold=0 means no lock
        threshold = c.compute_threshold(trust_score=1.0, successful_merges=0)
        assert threshold == pytest.approx(0.7)

    def test_pure_function_no_db_required(self) -> None:
        """compute_threshold must not require a DB — callable without one."""
        c = _make_calibrator()
        # Should not raise or require any DB argument
        result = c.compute_threshold(trust_score=0.8, successful_merges=5)
        assert isinstance(result, float)


# ===========================================================================
# TestUpdateAfterRun
# ===========================================================================


class TestUpdateAfterRun:
    """update_after_run EMA logic, counters, DB writes, and return dict."""

    def test_unknown_outcome_raises_value_error(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        with pytest.raises(ValueError, match="outcome"):
            c.update_after_run("run-1", "unknown_outcome", db)

    def test_returns_dict_with_required_keys(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        required = {
            "profile_id", "adjustment_id", "run_id", "outcome",
            "old_score", "new_score", "delta", "threshold",
            "total_runs", "successful_merges", "regressions", "reverted_prs",
        }
        assert required <= set(result.keys())

    def test_run_id_preserved_in_return(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("my-run-abc", "run_success", db)
        assert result["run_id"] == "my-run-abc"

    def test_outcome_preserved_in_return(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "regression", db)
        assert result["outcome"] == "regression"

    # --- EMA formula ---

    def test_ema_run_success_increases_score(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        result = c.update_after_run("run-1", "run_success", db)
        # new = 0.1*1.0 + 0.9*0.5 = 0.1 + 0.45 = 0.55
        assert result["new_score"] == pytest.approx(0.55)

    def test_ema_regression_decreases_score(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        result = c.update_after_run("run-1", "regression", db)
        # new = 0.1*(-3.0) + 0.9*0.5 = -0.3 + 0.45 = 0.15
        assert result["new_score"] == pytest.approx(0.15)

    def test_ema_revert_decreases_score(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        result = c.update_after_run("run-1", "revert", db)
        # new = 0.1*(-2.0) + 0.9*0.5 = -0.2 + 0.45 = 0.25
        assert result["new_score"] == pytest.approx(0.25)

    def test_ema_human_override_reject_decreases_score(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        result = c.update_after_run("run-1", "human_override_reject", db)
        # new = 0.1*(-1.0) + 0.9*0.5 = -0.1 + 0.45 = 0.35
        assert result["new_score"] == pytest.approx(0.35)

    def test_old_score_matches_pre_update_score(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.6))
        result = c.update_after_run("run-1", "run_success", db)
        assert result["old_score"] == pytest.approx(0.6)

    def test_delta_equals_new_minus_old(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        assert result["delta"] == pytest.approx(result["new_score"] - result["old_score"])

    # --- Score clamping ---

    def test_score_clamped_at_zero_on_severe_penalty(self) -> None:
        c = _make_calibrator(alpha=1.0)  # immediate response
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.0))
        result = c.update_after_run("run-1", "regression", db)
        # raw = 1.0*(-3.0) + 0.0*0.0 = -3.0 → clamped to 0.0
        assert result["new_score"] == pytest.approx(0.0)
        assert result["new_score"] >= 0.0

    def test_score_clamped_at_one_on_large_positive(self) -> None:
        c = _make_calibrator(alpha=1.0)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.9))
        result = c.update_after_run("run-1", "run_success", db)
        # raw = 1.0*1.0 → 1.0, which is already ≤ 1.0
        assert result["new_score"] == pytest.approx(1.0)
        assert result["new_score"] <= 1.0

    # --- Counter increments ---

    def test_total_runs_incremented(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(total_runs=3))
        result = c.update_after_run("run-1", "run_success", db)
        assert result["total_runs"] == 4

    def test_successful_merges_incremented_on_run_success(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(successful_merges=2))
        result = c.update_after_run("run-1", "run_success", db)
        assert result["successful_merges"] == 3

    def test_successful_merges_not_incremented_on_regression(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(successful_merges=2))
        result = c.update_after_run("run-1", "regression", db)
        assert result["successful_merges"] == 2

    def test_regressions_incremented_on_regression(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(regressions=1))
        result = c.update_after_run("run-1", "regression", db)
        assert result["regressions"] == 2

    def test_regressions_not_incremented_on_run_success(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(regressions=1))
        result = c.update_after_run("run-1", "run_success", db)
        assert result["regressions"] == 1

    def test_reverted_prs_incremented_on_revert(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(reverted_prs=0))
        result = c.update_after_run("run-1", "revert", db)
        assert result["reverted_prs"] == 1

    def test_no_dedicated_counter_for_human_override_reject(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        db.upsert_trust_profile(_profile_data(
            successful_merges=0, regressions=0, reverted_prs=0
        ))
        result = c.update_after_run("run-1", "human_override_reject", db)
        assert result["successful_merges"] == 0
        assert result["regressions"] == 0
        assert result["reverted_prs"] == 0
        assert result["total_runs"] == 1  # only total_runs increments

    # --- Profile auto-creation ---

    def test_creates_profile_if_not_exists(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        # No profile pre-created
        result = c.update_after_run("run-1", "run_success", db)
        assert result["profile_id"] is not None
        assert isinstance(result["profile_id"], int)
        # Profile should now exist
        profile = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert profile is not None

    def test_auto_created_profile_starts_at_default_score(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        # TrustProfile default is 0.5; after one run_success:
        # 0.1*1.0 + 0.9*0.5 = 0.55
        assert result["old_score"] == pytest.approx(0.5)
        assert result["new_score"] == pytest.approx(0.55)

    # --- DB persistence ---

    def test_profile_score_persisted_in_db(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        c.update_after_run("run-1", "run_success", db)
        profile = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert profile is not None
        assert profile["trust_score"] == pytest.approx(0.55)

    def test_adjustment_logged_in_db(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        adjustments = db.list_trust_adjustments(result["profile_id"])
        assert len(adjustments) == 1

    def test_adjustment_contains_correct_fields(self) -> None:
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        result = c.update_after_run("run-1", "run_success", db)
        adj = db.list_trust_adjustments(result["profile_id"])[0]
        assert adj["run_id"] == "run-1"
        assert adj["score_before"] == pytest.approx(0.5)
        assert adj["score_after"] == pytest.approx(0.55)
        assert adj["delta"] == pytest.approx(0.05)
        assert adj["reason"] == "run_success"

    def test_adjustment_id_in_return_dict(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        assert isinstance(result["adjustment_id"], int)
        assert result["adjustment_id"] > 0

    def test_multiple_runs_accumulate_adjustments(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        r1 = c.update_after_run("run-1", "run_success", db)
        r2 = c.update_after_run("run-2", "regression", db)
        adjustments = db.list_trust_adjustments(r1["profile_id"])
        assert len(adjustments) == 2

    def test_consecutive_runs_chain_ema(self) -> None:
        """Each run's EMA uses the score written by the previous run."""
        c = _make_calibrator(alpha=0.1)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        r1 = c.update_after_run("run-1", "run_success", db)
        # After run 1: 0.1*1 + 0.9*0.5 = 0.55
        assert r1["new_score"] == pytest.approx(0.55)
        r2 = c.update_after_run("run-2", "run_success", db)
        # After run 2: 0.1*1 + 0.9*0.55 = 0.595
        assert r2["old_score"] == pytest.approx(0.55)
        assert r2["new_score"] == pytest.approx(0.595)

    # --- Threshold in return dict ---

    def test_threshold_in_return_is_float(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        assert isinstance(result["threshold"], float)

    def test_threshold_locked_during_bootstrap(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        db = _make_db()
        # Seed with 9 successful merges (still in bootstrap)
        db.upsert_trust_profile(_profile_data(successful_merges=9))
        result = c.update_after_run("run-1", "run_success", db)
        # Now successful_merges=10 — just crossed bootstrap, should unlock
        assert result["successful_merges"] == 10
        # At trust ~0.55, post-bootstrap threshold should be below 0.98
        assert result["threshold"] < 0.98

    def test_threshold_at_conservative_for_low_merges(self) -> None:
        c = _make_calibrator(conservative=0.98, aggressive=0.7, bootstrap_threshold=10)
        db = _make_db()
        db.upsert_trust_profile(_profile_data(successful_merges=5))
        result = c.update_after_run("run-1", "regression", db)
        # After regression, successful_merges stays at 5 < 10 → bootstrap
        assert result["threshold"] == pytest.approx(0.98)

    def test_auto_merge_threshold_persisted_in_profile(self) -> None:
        c = _make_calibrator(bootstrap_threshold=0)  # no bootstrap
        db = _make_db()
        result = c.update_after_run("run-1", "run_success", db)
        profile = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert profile is not None
        assert profile["auto_merge_threshold"] == pytest.approx(result["threshold"])


# ===========================================================================
# TestUpdateAfterRunEdgeCases
# ===========================================================================


class TestUpdateAfterRunEdgeCases:
    """Edge cases: boundary scores, all outcomes, profile isolation."""

    def test_all_valid_outcomes_accepted(self) -> None:
        c = _make_calibrator()
        for outcome in VALID_OUTCOMES:
            db = _make_db()
            result = c.update_after_run(f"run-{outcome}", outcome, db)
            assert result["outcome"] == outcome

    def test_profile_isolation_between_repos(self) -> None:
        db = _make_db()
        c_a = _make_calibrator(repo="repo-a")
        c_b = _make_calibrator(repo="repo-b")
        db.upsert_trust_profile(_profile_data(repo="repo-a", trust_score=0.3))
        db.upsert_trust_profile(_profile_data(repo="repo-b", trust_score=0.9))
        result_a = c_a.update_after_run("run-1", "run_success", db)
        result_b = c_b.update_after_run("run-1", "regression", db)
        # Ensure each calibrator read its own profile's old_score
        assert result_a["old_score"] == pytest.approx(0.3)
        assert result_b["old_score"] == pytest.approx(0.9)

    def test_profile_isolation_between_task_types(self) -> None:
        db = _make_db()
        c_bug = _make_calibrator(task_type="bugfix")
        c_feat = _make_calibrator(task_type="feature")
        db.upsert_trust_profile(_profile_data(task_type="bugfix", trust_score=0.4))
        db.upsert_trust_profile(_profile_data(task_type="feature", trust_score=0.8))
        r_bug = c_bug.update_after_run("run-1", "run_success", db)
        r_feat = c_feat.update_after_run("run-1", "run_success", db)
        assert r_bug["old_score"] == pytest.approx(0.4)
        assert r_feat["old_score"] == pytest.approx(0.8)

    def test_high_alpha_reacts_faster(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        c_low = _make_calibrator(alpha=0.1)
        c_high = TrustCalibrator("owner/repo", "coding-pipeline-v1", "bugfix",
                                 alpha=0.9)
        # Separate DBs to avoid cross-contamination
        db_low = _make_db()
        db_high = _make_db()
        db_low.upsert_trust_profile(_profile_data(trust_score=0.5))
        db_high.upsert_trust_profile(_profile_data(trust_score=0.5))
        r_low = c_low.update_after_run("run-1", "run_success", db_low)
        r_high = c_high.update_after_run("run-1", "run_success", db_high)
        # High alpha should react more strongly to the positive outcome
        assert r_high["new_score"] > r_low["new_score"]

    def test_empty_run_id_accepted(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        result = c.update_after_run("", "run_success", db)
        assert result["run_id"] == ""

    def test_last_run_at_updated_in_db(self) -> None:
        c = _make_calibrator()
        db = _make_db()
        c.update_after_run("run-1", "run_success", db)
        profile = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert profile is not None
        assert profile["last_run_at"] is not None


# ===========================================================================
# TestModuleExports
# ===========================================================================


class TestModuleExports:
    """Verify __init__.py exports TrustCalibrator, OUTCOME_SCORES, VALID_OUTCOMES."""

    def test_trust_calibrator_in_all(self) -> None:
        import orchestration_engine as oe
        assert "TrustCalibrator" in oe.__all__

    def test_outcome_scores_in_all(self) -> None:
        import orchestration_engine as oe
        assert "OUTCOME_SCORES" in oe.__all__

    def test_valid_outcomes_in_all(self) -> None:
        import orchestration_engine as oe
        assert "VALID_OUTCOMES" in oe.__all__

    def test_trust_calibrator_importable_from_package(self) -> None:
        from orchestration_engine import TrustCalibrator  # noqa: F401
        assert TrustCalibrator is not None

    def test_outcome_scores_importable_from_package(self) -> None:
        from orchestration_engine import OUTCOME_SCORES  # noqa: F401
        assert OUTCOME_SCORES is not None

    def test_valid_outcomes_importable_from_package(self) -> None:
        from orchestration_engine import VALID_OUTCOMES  # noqa: F401
        assert VALID_OUTCOMES is not None

    def test_trust_calibrator_importable_from_module(self) -> None:
        from orchestration_engine.trust import TrustCalibrator  # noqa: F401
        assert TrustCalibrator is not None

    def test_trust_profile_still_exported(self) -> None:
        """Regression guard: TrustProfile must still be in __all__."""
        import orchestration_engine as oe
        assert "TrustProfile" in oe.__all__

    def test_trust_config_still_exported(self) -> None:
        """Regression guard: TrustConfig must still be in __all__."""
        import orchestration_engine as oe
        assert "TrustConfig" in oe.__all__
