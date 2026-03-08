"""E2E integration test for the self-healing chain.

Simulates the full flow:
  CI failure webhook → RegressionDetector → DiagnosisEngine
    → SafetyGuard → RegressionFixer → handle_fix_completion (auto-merge gate)
    → TrustCalibrator.update_after_run

Uses:
  - Real Database (in-memory SQLite via tmp_path)
  - Real RegressionDetector, RegressionFixer, SafetyGuard, TrustCalibrator
  - Mock GitContext (no real git operations)
  - Mock executor (returns canned DiagnosisResult JSON)
  - Mock subprocess (no real gh CLI, no real orch launch)

Issue: #429.4
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.db import Database
from orchestration_engine.regression import (
    Regression,
    RegressionDetector,
    RegressionFixer,
    RegressionStatus,
    RegressionWebhookHandler,
    SafetyGuard,
)
from orchestration_engine.trust import TrustCalibrator
from orchestration_engine.daemon import dispatch_regression_fix_safely


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_db(tmp_path):
    """Real Database instance backed by a temp SQLite file.

    The Database class auto-runs migrations on connect, so all tables
    (regressions, ci_green_shas, trust_profiles, trust_adjustments, etc.)
    are available immediately.
    """
    return Database(str(tmp_path / "e2e.db"))


@pytest.fixture
def mock_git():
    """Mock GitContext that returns predictable commit data without git I/O."""
    git = MagicMock()
    git.get_commit_range.return_value = [
        "aabbccdd11223344",
        "11223344aabbccdd",
    ]
    git.get_commit_files.return_value = [
        "src/scorer.py",
        "tests/test_scorer.py",
    ]
    return git


@pytest.fixture
def mock_executor():
    """Mock LLM executor that returns a canned DiagnosisResult JSON response."""
    executor = MagicMock()
    diagnosis_json = json.dumps({
        "failure_class": "bad_prompt",
        "remediation": "retry_same",
        "confidence": 0.88,
        "explanation": "The spec phase prompt was ambiguous.",
    })
    result = MagicMock()
    result.result = {"text": diagnosis_json}
    executor.execute.return_value = result
    return executor


@pytest.fixture
def regression_record():
    """A clean Regression instance representing a detected CI failure."""
    return Regression(
        commit_sha="aabbccdd11223344",
        ci_run_url="https://github.com/org/repo/actions/runs/1001",
        failure_type="test_failure",
        affected_files=["src/scorer.py", "tests/test_scorer.py"],
    )


@pytest.fixture
def fixer(tmp_path):
    """RegressionFixer wired to a fake repo."""
    return RegressionFixer(
        repo_path=tmp_path / "repo",
        repo_url="https://github.com/org/repo",
        repo_slug="org/repo",
    )


def _orch_launch_stdout(run_id: str = "run-fix-001") -> str:
    """Return fake orch launch stdout containing a parseable run_id line."""
    return (
        f"✓ Pipeline launched in background\n"
        f"  Run ID:  {run_id}\n"
        f"  Logs:    orch logs {run_id}\n"
    )


def _subprocess_success(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock CompletedProcess with the given values."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# Test class 1: Happy Path — full chain succeeds
# ---------------------------------------------------------------------------


class TestHappyPathChain:
    """End-to-end happy path: every stage succeeds and the chain completes."""

    def test_regression_detected_persisted_to_db(
        self, real_db, mock_git, tmp_path
    ):
        """RegressionDetector.detect() persists a Regression record to the real DB."""
        real_db.store_green_sha("org/repo", "deadbeef00000000")
        detector = RegressionDetector(db=real_db, git_context=mock_git)

        regression = detector.detect(
            last_green_sha="deadbeef00000000",
            head_sha="aabbccdd11223344",
            ci_error_log="FAILED: tests/test_scorer.py::test_compute_score",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            repo_path=tmp_path / "repo",
        )

        assert regression is not None
        assert regression.commit_sha == "aabbccdd11223344"
        assert regression.status == RegressionStatus.DETECTED

        # Verify it was persisted to the real DB
        record = real_db.get_regression(regression.id)
        assert record is not None
        assert record["commit_sha"] == "aabbccdd11223344"
        assert record["failure_type"] == "test_failure"

    def test_safety_guard_allows_new_regression(
        self, real_db, regression_record
    ):
        """SafetyGuard allows a fresh regression (0 attempts, standard failure type)."""
        guard = SafetyGuard()
        allowed, reason = guard.should_attempt_fix(regression_record, real_db)
        assert allowed is True
        assert "safe to attempt fix" in reason

    def test_dispatch_spawns_fix_after_guard_pass(
        self, real_db, regression_record, fixer, tmp_path
    ):
        """dispatch_regression_fix_safely: guard passes → spawn_fix called → run_id returned."""
        real_db.insert_regression(regression_record.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                stdout=_orch_launch_stdout("run-fix-001")
            ),
        ):
            run_id = dispatch_regression_fix_safely(
                regression=regression_record,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id == "run-fix-001"

    def test_dispatch_updates_regression_status_to_fixing(
        self, real_db, regression_record, fixer, tmp_path
    ):
        """After dispatch_regression_fix_safely, regression DB record is FIXING."""
        real_db.insert_regression(regression_record.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                stdout=_orch_launch_stdout("run-fix-001")
            ),
        ):
            dispatch_regression_fix_safely(
                regression=regression_record,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        record = real_db.get_regression(regression_record.id)
        assert record["status"] == RegressionStatus.FIXING.value
        assert record["fix_run_id"] == "run-fix-001"

    def test_handle_fix_completion_high_score_returns_fixed(
        self, real_db, regression_record, fixer
    ):
        """handle_fix_completion: score ≥ threshold + status passed → FIXED."""
        real_db.insert_regression(regression_record.to_dict())

        fix_run = {"scoring_score": 0.97, "scoring_status": "passed"}
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(returncode=0),
        ):
            result = fixer.handle_fix_completion(
                regression_record.id, fix_run, real_db
            )

        assert result == RegressionStatus.FIXED.value
        record = real_db.get_regression(regression_record.id)
        assert record["status"] == RegressionStatus.FIXED.value

    def test_trust_calibration_updated_after_success(self, real_db):
        """TrustCalibrator.update_after_run updates the trust profile in the real DB."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )

        result = calibrator.update_after_run(
            run_id="run-fix-001",
            outcome="run_success",
            db=real_db,
        )

        assert result["outcome"] == "run_success"
        assert result["new_score"] is not None
        assert 0.0 <= result["new_score"] <= 1.0

        # Verify trust profile persisted to real DB
        profile = real_db.get_trust_profile(
            "org/repo", "coding-pipeline-v1", "test_failure"
        )
        assert profile is not None
        assert profile["total_runs"] == 1
        assert profile["successful_merges"] == 1

    def test_full_chain_from_webhook_to_fix_dispatch(
        self, real_db, mock_git, fixer, tmp_path
    ):
        """Integration: webhook event → detect → dispatch → fix spawned.

        Wires RegressionWebhookHandler.handle_ci_failure into the
        dispatch_regression_fix_safely path to verify the complete
        chain produces a run_id and leaves the DB in a consistent state.
        """
        # Store a green SHA baseline
        real_db.store_green_sha("org/repo", "deadbeef00000000")

        # Suppress gh issue create
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            mock_run.return_value = _subprocess_success(
                stdout="https://github.com/org/repo/issues/42"
            )

            detector = RegressionDetector(db=real_db, git_context=mock_git)
            handler = RegressionWebhookHandler(
                db=real_db,
                git_context=mock_git,
                detector=detector,
                repo_path=tmp_path / "repo",
                repo_slug="org/repo",
                template_id="coding-pipeline-v1",
            )

            payload = {
                "check_suite": {
                    "conclusion": "failure",
                    "head_sha": "aabbccdd11223344",
                    "url": "https://github.com/org/repo/actions/runs/1001",
                    "check_runs": [
                        {
                            "output": {
                                "title": "Tests failed",
                                "summary": "FAILED tests/test_scorer.py",
                                "text": "",
                            }
                        }
                    ],
                }
            }
            regression = handler.handle_ci_failure(payload)

        assert regression is not None
        assert regression.status == RegressionStatus.DETECTED

        # Now dispatch the fix through the safety-gated path
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                stdout=_orch_launch_stdout("run-fix-e2e")
            ),
        ):
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id == "run-fix-e2e"
        record = real_db.get_regression(regression.id)
        assert record["status"] == RegressionStatus.FIXING.value
        assert record["fix_run_id"] == "run-fix-e2e"


# ---------------------------------------------------------------------------
# Test class 2: SafetyGuard blocking — chain stops early
# ---------------------------------------------------------------------------


class TestSafetyGuardBlocking:
    """Safety guard blocks fix attempts; verify regression is ESCALATED."""

    def test_max_attempts_blocks_dispatch(
        self, real_db, fixer, tmp_path
    ):
        """dispatch_regression_fix_safely: max attempts reached → returns None."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            fix_attempt_count=3,  # at the limit
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        mock_run.assert_not_called()

    def test_max_attempts_escalates_db_record(
        self, real_db, fixer, tmp_path
    ):
        """When guard blocks due to max attempts, regression is ESCALATED in DB."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            fix_attempt_count=3,
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run"):
            dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        record = real_db.get_regression(regression.id)
        assert record["status"] == RegressionStatus.ESCALATED.value

    def test_excluded_failure_type_blocks_dispatch(
        self, real_db, fixer, tmp_path
    ):
        """Infrastructure failures are excluded → guard blocks → returns None."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="infra_failure",
            fix_attempt_count=0,
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        mock_run.assert_not_called()

    def test_excluded_type_escalates_db_record(
        self, real_db, fixer, tmp_path
    ):
        """Excluded failure type → ESCALATED status in DB."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="network_timeout",
            fix_attempt_count=0,
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run"):
            dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        record = real_db.get_regression(regression.id)
        assert record["status"] == RegressionStatus.ESCALATED.value

    def test_flaky_keyword_blocks_dispatch(
        self, real_db, fixer, tmp_path
    ):
        """Flaky test keyword in failure_type → guard blocks → returns None."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="flaky_test",
            fix_attempt_count=0,
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        mock_run.assert_not_called()

    def test_oscillation_pattern_blocks_dispatch(
        self, real_db, fixer, tmp_path
    ):
        """DB oscillation: same commit self-healed previously → guard blocks."""
        # Insert a prior regression for the same commit that was FIXED with no fix_run_id
        prior = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/999",
            failure_type="test_failure",
            status=RegressionStatus.FIXED,
            fix_run_id=None,
        )
        real_db.insert_regression(prior.to_dict())

        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            fix_attempt_count=0,
        )
        real_db.insert_regression(regression.to_dict())

        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test class 3: Mid-chain failure — graceful degradation
# ---------------------------------------------------------------------------


class TestMidChainFailureDegradation:
    """Verify the chain degrades gracefully when individual stages fail."""

    def test_spawn_fix_subprocess_failure_returns_none(
        self, real_db, regression_record, fixer, tmp_path
    ):
        """When orch launch fails (non-zero rc), dispatch returns None without raising."""
        real_db.insert_regression(regression_record.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                returncode=1,
                stderr="template not found",
            ),
        ):
            run_id = dispatch_regression_fix_safely(
                regression=regression_record,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        # Regression should remain in DETECTED (not FIXING) on launch failure
        record = real_db.get_regression(regression_record.id)
        assert record["status"] == RegressionStatus.DETECTED.value

    def test_spawn_fix_timeout_returns_none(
        self, real_db, regression_record, fixer, tmp_path
    ):
        """When orch launch times out, dispatch returns None without raising."""
        real_db.insert_regression(regression_record.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=60),
        ):
            run_id = dispatch_regression_fix_safely(
                regression=regression_record,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None

    def test_handle_fix_completion_low_score_returns_needs_review(
        self, real_db, regression_record, fixer
    ):
        """handle_fix_completion: score below threshold → NEEDS_REVIEW, no merge."""
        real_db.insert_regression(regression_record.to_dict())

        fix_run = {"scoring_score": 0.85, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(
                regression_record.id, fix_run, real_db
            )

        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_run.assert_not_called()
        record = real_db.get_regression(regression_record.id)
        assert record["status"] == RegressionStatus.NEEDS_REVIEW.value

    def test_handle_fix_completion_merge_failure_degrades(
        self, real_db, regression_record, fixer
    ):
        """handle_fix_completion: score passes gate but merge fails → NEEDS_REVIEW."""
        real_db.insert_regression(regression_record.to_dict())

        fix_run = {"scoring_score": 0.97, "scoring_status": "passed"}
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(returncode=1, stderr="PR not found"),
        ):
            result = fixer.handle_fix_completion(
                regression_record.id, fix_run, real_db
            )

        assert result == RegressionStatus.NEEDS_REVIEW.value

    def test_detector_no_commits_in_range_returns_none(
        self, real_db, tmp_path
    ):
        """RegressionDetector: empty commit range → returns None without raising."""
        git = MagicMock()
        git.get_commit_range.return_value = []  # no commits found

        real_db.store_green_sha("org/repo", "deadbeef00000000")
        detector = RegressionDetector(db=real_db, git_context=git)

        regression = detector.detect(
            last_green_sha="deadbeef00000000",
            head_sha="aabbccdd11223344",
            ci_error_log="FAILED: tests/test_scorer.py",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            repo_path=tmp_path / "repo",
        )

        assert regression is None

    def test_webhook_handler_no_green_sha_returns_none(
        self, real_db, mock_git, tmp_path
    ):
        """RegressionWebhookHandler: no baseline SHA → returns None without raising."""
        detector = RegressionDetector(db=real_db, git_context=mock_git)
        handler = RegressionWebhookHandler(
            db=real_db,
            git_context=mock_git,
            detector=detector,
            repo_path=tmp_path / "repo",
            repo_slug="org/repo",
        )

        payload = {
            "check_suite": {
                "conclusion": "failure",
                "head_sha": "aabbccdd11223344",
                "url": "https://github.com/org/repo/actions/runs/1001",
            }
        }
        # No green SHA stored → should return None gracefully
        regression = handler.handle_ci_failure(payload)
        assert regression is None

    def test_db_update_failure_in_guard_block_does_not_raise(
        self, fixer, tmp_path
    ):
        """If DB update fails when escalating, dispatch_regression_fix_safely swallows it."""
        broken_db = MagicMock()
        broken_db.list_regressions.return_value = []
        broken_db.update_regression.side_effect = RuntimeError("DB connection lost")

        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="infra_failure",  # excluded → guard blocks
            fix_attempt_count=0,
        )

        # Must not raise even though DB update fails
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            run_id = dispatch_regression_fix_safely(
                regression=regression,
                db=broken_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        assert run_id is None
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test class 4: Trust score side-effects across the chain
# ---------------------------------------------------------------------------


class TestTrustCalibrationSideEffects:
    """Verify TrustCalibrator correctly updates trust scores after chain outcomes."""

    def test_regression_penalty_applied_after_ci_failure(self, real_db):
        """TrustCalibrator: 'regression' outcome decreases trust score from 0.5."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )

        result = calibrator.update_after_run(
            run_id="regression-001",
            outcome="regression",
            db=real_db,
        )

        # Initial score is 0.5; regression should move it downward
        assert result["new_score"] < 0.5
        assert result["delta"] < 0
        assert result["regressions"] == 1
        assert result["successful_merges"] == 0

    def test_success_delta_applied_after_fix(self, real_db):
        """TrustCalibrator: 'run_success' outcome increases trust score."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )

        result = calibrator.update_after_run(
            run_id="run-fix-success",
            outcome="run_success",
            db=real_db,
        )

        # Initial score is 0.5; success should move it upward
        assert result["new_score"] > 0.5
        assert result["delta"] > 0
        assert result["successful_merges"] == 1

    def test_multiple_regression_updates_compound(self, real_db):
        """Multiple regression events should compound to drive trust score lower."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )

        r1 = calibrator.update_after_run("reg-001", "regression", real_db)
        r2 = calibrator.update_after_run("reg-002", "regression", real_db)
        r3 = calibrator.update_after_run("reg-003", "regression", real_db)

        # Each regression should reduce the score further (or reach the floor at 0.0)
        assert r2["new_score"] <= r1["new_score"]
        assert r3["new_score"] <= r2["new_score"]
        # At least one step must have actually decreased (not all clamped to 0.0 from start)
        assert r3["new_score"] < 0.5  # well below neutral

        profile = real_db.get_trust_profile(
            "org/repo", "coding-pipeline-v1", "test_failure"
        )
        assert profile["regressions"] == 3
        assert profile["total_runs"] == 3

    def test_webhook_handler_applies_trust_penalty_on_ci_failure(
        self, real_db, mock_git, tmp_path
    ):
        """RegressionWebhookHandler: CI failure triggers trust penalty in real DB."""
        real_db.store_green_sha("org/repo", "deadbeef00000000")

        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_subprocess_success(
                       stdout="https://github.com/org/repo/issues/1"
                   )):
            detector = RegressionDetector(db=real_db, git_context=mock_git)
            handler = RegressionWebhookHandler(
                db=real_db,
                git_context=mock_git,
                detector=detector,
                repo_path=tmp_path / "repo",
                repo_slug="org/repo",
                template_id="coding-pipeline-v1",
            )

            payload = {
                "check_suite": {
                    "conclusion": "failure",
                    "head_sha": "aabbccdd11223344",
                    "url": "https://github.com/org/repo/actions/runs/1001",
                }
            }
            regression = handler.handle_ci_failure(payload)

        assert regression is not None

        # Trust penalty should have been applied to the real DB.
        # handle_ci_failure uses regression.failure_type ("ci_failure") as task_type.
        profile = real_db.get_trust_profile(
            "org/repo", "coding-pipeline-v1", "ci_failure"
        )
        assert profile is not None
        assert profile["regressions"] == 1
        assert profile["trust_score"] < 0.5  # below neutral after penalty

    def test_trust_score_bounded_between_zero_and_one(self, real_db):
        """TrustCalibrator: score never goes below 0.0 or above 1.0."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )

        # Apply many regression penalties to attempt to go below zero
        for i in range(50):
            result = calibrator.update_after_run(
                run_id=f"reg-{i:03d}",
                outcome="regression",
                db=real_db,
            )
            assert result["new_score"] >= 0.0, (
                f"Trust score went below 0.0 at iteration {i}: "
                f"{result['new_score']}"
            )

        # Apply many successes to attempt to go above one
        calibrator2 = TrustCalibrator(
            repo="org2/repo2",
            template_id="coding-pipeline-v1",
            task_type="test_failure",
        )
        for i in range(50):
            result = calibrator2.update_after_run(
                run_id=f"run-{i:03d}",
                outcome="run_success",
                db=real_db,
            )
            assert result["new_score"] <= 1.0, (
                f"Trust score exceeded 1.0 at iteration {i}: "
                f"{result['new_score']}"
            )


# ---------------------------------------------------------------------------
# Test class 5: Data contract verification — inter-component handoffs
# ---------------------------------------------------------------------------


class TestInterComponentDataContracts:
    """Verify that data flows correctly across component boundaries."""

    def test_regression_to_dict_round_trips_through_db(
        self, real_db, regression_record
    ):
        """Regression.to_dict() → DB insert → DB get_regression round-trips cleanly."""
        real_db.insert_regression(regression_record.to_dict())
        record = real_db.get_regression(regression_record.id)

        assert record["id"] == regression_record.id
        assert record["commit_sha"] == regression_record.commit_sha
        assert record["failure_type"] == regression_record.failure_type
        assert record["status"] == RegressionStatus.DETECTED.value
        # affected_files should round-trip (stored as JSON string)
        stored_files = (
            json.loads(record["affected_files"])
            if isinstance(record["affected_files"], str)
            else record["affected_files"]
        )
        assert stored_files == regression_record.affected_files

    def test_regression_id_field_in_fix_input(
        self, fixer, regression_record
    ):
        """RegressionFixer._build_fix_input produces all fields required for the pipeline."""
        fix_input = fixer._build_fix_input(regression_record)

        assert fix_input["regression_id"] == regression_record.id
        assert fix_input["repo_url"] == "https://github.com/org/repo"
        assert fix_input["affected_files"] == regression_record.affected_files
        assert regression_record.commit_sha in fix_input["task_description"]

    def test_fix_run_id_stored_in_regression_after_dispatch(
        self, real_db, regression_record, fixer, tmp_path
    ):
        """After dispatch, fix_run_id is stored in the regression DB record."""
        real_db.insert_regression(regression_record.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                stdout=_orch_launch_stdout("run-contract-check")
            ),
        ):
            dispatch_regression_fix_safely(
                regression=regression_record,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        record = real_db.get_regression(regression_record.id)
        assert record["fix_run_id"] == "run-contract-check"

    def test_ci_success_updates_green_sha(
        self, real_db, mock_git, tmp_path
    ):
        """Webhook handler: CI success conclusion updates the green SHA baseline."""
        real_db.store_green_sha("org/repo", "oldgreenshaaabbbb")

        detector = RegressionDetector(db=real_db, git_context=mock_git)
        handler = RegressionWebhookHandler(
            db=real_db,
            git_context=mock_git,
            detector=detector,
            repo_path=tmp_path / "repo",
            repo_slug="org/repo",
        )

        payload = {
            "check_suite": {
                "conclusion": "success",
                "head_sha": "newgreenshaffffffff",
            }
        }
        result = handler.handle_ci_failure(payload)

        assert result is None  # success → no regression
        stored = real_db.get_last_green_sha("org/repo")
        assert stored == "newgreenshaffffffff"

    def test_attempt_count_incremented_in_regression_record(
        self, real_db, fixer, tmp_path
    ):
        """After dispatch, fix_attempt_count is incremented in DB."""
        regression = Regression(
            commit_sha="aabbccdd11223344",
            ci_run_url="https://github.com/org/repo/actions/runs/1001",
            failure_type="test_failure",
            fix_attempt_count=1,  # already attempted once
        )
        real_db.insert_regression(regression.to_dict())

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_subprocess_success(
                stdout=_orch_launch_stdout("run-attempt-2")
            ),
        ):
            dispatch_regression_fix_safely(
                regression=regression,
                db=real_db,
                db_path=tmp_path / "e2e.db",
                fixer=fixer,
            )

        record = real_db.get_regression(regression.id)
        assert record["fix_attempt_count"] == 2

    def test_trust_adjustment_audit_trail_created(self, real_db):
        """TrustCalibrator.update_after_run creates an audit row in trust_adjustments."""
        calibrator = TrustCalibrator(
            repo="org/repo",
            template_id="coding-pipeline-v1",
            task_type="audit_trail_test",
        )

        result = calibrator.update_after_run(
            run_id="run-audit-001",
            outcome="run_success",
            db=real_db,
        )

        adjustment_id = result.get("adjustment_id")
        assert adjustment_id is not None

        adjustments = real_db.list_trust_adjustments(
            profile_id=result["profile_id"]
        )
        assert len(adjustments) >= 1
        last = adjustments[-1]
        # trust_adjustments stores outcome in the `reason` field
        assert last["run_id"] == "run-audit-001"
        assert last["reason"] == "run_success"
