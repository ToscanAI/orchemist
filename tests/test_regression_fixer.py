"""Tests for RegressionFixer.

Issue: #3.3b.1 — RegressionFixer — spawn fix pipeline

Coverage:
- TestBuildFixInput:  task description content, branch naming, required fields,
                      empty affected files, diagnosis inclusion.
- TestParseRunId:     standard output, no match, empty string, extra whitespace,
                      multiple lines.
- TestSpawnFix:       happy path, DB status update, run_id storage, attempt count
                      increment, nonzero return, timeout, file not found,
                      missing run_id in stdout, db_path forwarding, no db_path,
                      tempfile cleanup, DB update failure graceful.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.regression import (
    Regression,
    RegressionFixer,
    RegressionStatus,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_regression(
    commit_sha: str = "abcdef1234567890",
    failure_type: str = "test_failure",
    ci_run_url: str = "https://github.com/org/repo/actions/runs/42",
    affected_files: list | None = None,
    diagnosis: str | None = None,
    fix_attempt_count: int = 0,
) -> Regression:
    r = Regression(
        commit_sha=commit_sha,
        ci_run_url=ci_run_url,
        failure_type=failure_type,
        affected_files=affected_files if affected_files is not None else ["src/foo.py"],
        diagnosis=diagnosis,
        fix_attempt_count=fix_attempt_count,
    )
    return r


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


@pytest.fixture
def fixer(tmp_path):
    return RegressionFixer(
        repo_path=tmp_path,
        repo_url="https://github.com/org/repo",
        repo_slug="org/repo",
    )


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.update_regression.return_value = True
    return db


# ---------------------------------------------------------------------------
# TestBuildFixInput
# ---------------------------------------------------------------------------


class TestBuildFixInput:
    """Tests for RegressionFixer._build_fix_input."""

    def test_task_description_contains_commit_sha(self, fixer):
        regression = _make_regression(commit_sha="deadbeef12345678")
        result = fixer._build_fix_input(regression)
        assert "deadbeef12345678" in result["task_description"]

    def test_task_description_contains_failure_type(self, fixer):
        regression = _make_regression(failure_type="build_error")
        result = fixer._build_fix_input(regression)
        assert "build_error" in result["task_description"]

    def test_task_description_contains_ci_run_url(self, fixer):
        url = "https://github.com/org/repo/actions/runs/999"
        regression = _make_regression(ci_run_url=url)
        result = fixer._build_fix_input(regression)
        assert url in result["task_description"]

    def test_task_description_contains_affected_files(self, fixer):
        regression = _make_regression(affected_files=["src/engine.py", "tests/test_engine.py"])
        result = fixer._build_fix_input(regression)
        assert "src/engine.py" in result["task_description"]
        assert "tests/test_engine.py" in result["task_description"]

    def test_branch_naming_format(self, fixer):
        """Branch should be fix/regression-{sha[:8]}-{id[:8]}."""
        regression = _make_regression(commit_sha="abcdef1234567890")
        result = fixer._build_fix_input(regression)
        expected_branch = f"fix/regression-abcdef12-{regression.id[:8]}"
        assert result["branch_name"] == expected_branch

    def test_required_fields_present(self, fixer):
        """All fields required by coding-pipeline-v1 must be present."""
        regression = _make_regression()
        result = fixer._build_fix_input(regression)
        for field in ("task_description", "branch_name", "repo_url", "repo_path",
                      "regression_id", "affected_files"):
            assert field in result, f"Missing required field: {field}"

    def test_empty_affected_files_graceful(self, fixer):
        """Empty affected_files list should not raise; task_description still valid."""
        regression = _make_regression(affected_files=[])
        result = fixer._build_fix_input(regression)
        assert isinstance(result["task_description"], str)
        assert len(result["task_description"]) > 0
        assert result["affected_files"] == []

    def test_diagnosis_included_when_present(self, fixer):
        """If regression has a diagnosis, it should appear in task_description."""
        regression = _make_regression(diagnosis="NullPointerException in scorer.py:42")
        result = fixer._build_fix_input(regression)
        assert "NullPointerException in scorer.py:42" in result["task_description"]

    def test_no_diagnosis_no_extra_section(self, fixer):
        """When diagnosis is None, the description should not mention it."""
        regression = _make_regression(diagnosis=None)
        result = fixer._build_fix_input(regression)
        # Should not blow up and should still be a valid string
        assert isinstance(result["task_description"], str)

    def test_repo_url_and_path_forwarded(self, fixer, tmp_path):
        regression = _make_regression()
        result = fixer._build_fix_input(regression)
        assert result["repo_url"] == "https://github.com/org/repo"
        assert result["repo_path"] == str(tmp_path)

    def test_regression_id_forwarded(self, fixer):
        regression = _make_regression()
        result = fixer._build_fix_input(regression)
        assert result["regression_id"] == regression.id


# ---------------------------------------------------------------------------
# TestParseRunId
# ---------------------------------------------------------------------------


class TestParseRunId:
    """Tests for RegressionFixer._parse_run_id (static method)."""

    def test_standard_output_extracts_run_id(self):
        stdout = (
            "✓ Pipeline launched in background\n"
            "  Run ID:  abc12345\n"
            "  Status:  orch status abc12345\n"
        )
        assert RegressionFixer._parse_run_id(stdout) == "abc12345"

    def test_no_match_returns_none(self):
        stdout = "Error: template not found\naborting"
        assert RegressionFixer._parse_run_id(stdout) is None

    def test_empty_string_returns_none(self):
        assert RegressionFixer._parse_run_id("") is None

    def test_extra_whitespace_stripped(self):
        stdout = "  Run ID:   xyz99999   \n"
        assert RegressionFixer._parse_run_id(stdout) == "xyz99999"

    def test_run_id_in_multiple_line_output(self):
        stdout = "\n".join([
            "Launching pipeline...",
            "Template: coding-pipeline-v1",
            "  Run ID:  run-7f3a",
            "  Logs:    orch logs run-7f3a",
        ])
        assert RegressionFixer._parse_run_id(stdout) == "run-7f3a"


# ---------------------------------------------------------------------------
# TestSpawnFix
# ---------------------------------------------------------------------------


class TestSpawnFix:
    """Tests for RegressionFixer.spawn_fix."""

    def _good_stdout(self, run_id: str = "run-abc12345") -> str:
        return (
            f"✓ Pipeline launched in background\n"
            f"  Run ID:  {run_id}\n"
            f"  Status:  orch status {run_id}\n"
        )

    def test_happy_path_returns_run_id(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout("run-xyz"))):
            result = fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        assert result == "run-xyz"

    def test_db_status_updated_to_fixing(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout("run-abc"))):
            fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        mock_db.update_regression.assert_called_once()
        call_kwargs = mock_db.update_regression.call_args
        assert call_kwargs.kwargs.get("status") == RegressionStatus.FIXING.value

    def test_run_id_stored_in_db(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout("run-store-me"))):
            fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        call_kwargs = mock_db.update_regression.call_args
        assert call_kwargs.kwargs.get("fix_run_id") == "run-store-me"

    def test_attempt_count_incremented(self, fixer, mock_db, tmp_path):
        regression = _make_regression(fix_attempt_count=2)
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout())):
            fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        call_kwargs = mock_db.update_regression.call_args
        assert call_kwargs.kwargs.get("fix_attempt_count") == 3

    def test_nonzero_returncode_returns_none(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(returncode=1, stderr="template not found")):
            result = fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        assert result is None
        mock_db.update_regression.assert_not_called()

    def test_timeout_returns_none(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="python", timeout=60)):
            result = fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        assert result is None
        mock_db.update_regression.assert_not_called()

    def test_file_not_found_returns_none(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   side_effect=FileNotFoundError("python not found")):
            result = fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        assert result is None
        mock_db.update_regression.assert_not_called()

    def test_missing_run_id_in_stdout_returns_none(self, fixer, mock_db, tmp_path):
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout="Pipeline output with no run id")):
            result = fixer.spawn_fix(regression, mock_db, tmp_path / "test.db")
        assert result is None
        mock_db.update_regression.assert_not_called()

    def test_db_path_forwarded_to_cli(self, fixer, mock_db, tmp_path):
        """When db_path is given, --db-path should appear in the subprocess call."""
        regression = _make_regression()
        db_file = tmp_path / "pipeline.db"
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout())) as mock_run:
            fixer.spawn_fix(regression, mock_db, db_file)
        cmd = mock_run.call_args[0][0]
        assert "--db-path" in cmd
        assert str(db_file) in cmd

    def test_no_db_path_no_flag(self, fixer, mock_db):
        """When db_path is None, --db-path must NOT appear in the subprocess call."""
        regression = _make_regression()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout())) as mock_run:
            fixer.spawn_fix(regression, mock_db, None)
        cmd = mock_run.call_args[0][0]
        assert "--db-path" not in cmd

    def test_tempfile_cleaned_up_on_success(self, fixer, mock_db, tmp_path):
        """Temp file should be deleted after a successful launch."""
        regression = _make_regression()
        created_tmp = []

        original_ntf = __import__("tempfile").NamedTemporaryFile

        import tempfile as _tempfile

        class _TrackingNTF:
            """Context manager that records the temp file path."""
            def __init__(self, **kwargs):
                self._ntf = original_ntf(**kwargs)
                created_tmp.append(self._ntf.name)

            def __enter__(self):
                return self._ntf.__enter__()

            def __exit__(self, *args):
                return self._ntf.__exit__(*args)

        with patch("orchestration_engine.regression.tempfile.NamedTemporaryFile",
                   side_effect=_TrackingNTF):
            with patch("orchestration_engine.regression.subprocess.run",
                       return_value=_make_completed(stdout=self._good_stdout())):
                fixer.spawn_fix(regression, mock_db, None)

        import os
        for path in created_tmp:
            assert not os.path.exists(path), f"Temp file not cleaned up: {path}"

    def test_tempfile_cleaned_up_on_failure(self, fixer, mock_db):
        """Temp file should also be deleted on subprocess error."""
        regression = _make_regression()
        created_tmp = []

        original_ntf = __import__("tempfile").NamedTemporaryFile

        class _TrackingNTF:
            def __init__(self, **kwargs):
                self._ntf = original_ntf(**kwargs)
                created_tmp.append(self._ntf.name)

            def __enter__(self):
                return self._ntf.__enter__()

            def __exit__(self, *args):
                return self._ntf.__exit__(*args)

        with patch("orchestration_engine.regression.tempfile.NamedTemporaryFile",
                   side_effect=_TrackingNTF):
            with patch("orchestration_engine.regression.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="python", timeout=60)):
                fixer.spawn_fix(regression, mock_db, None)

        import os
        for path in created_tmp:
            assert not os.path.exists(path), f"Temp file not cleaned up: {path}"

    def test_db_update_failure_returns_none_gracefully(self, fixer, tmp_path):
        """When DB update raises, spawn_fix should return None without re-raising."""
        regression = _make_regression()
        bad_db = MagicMock()
        bad_db.update_regression.side_effect = RuntimeError("DB connection lost")

        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(stdout=self._good_stdout("run-x"))):
            result = fixer.spawn_fix(regression, bad_db, None)

        assert result is None


# ---------------------------------------------------------------------------
# TestHandleFixCompletion
# ---------------------------------------------------------------------------


class TestHandleFixCompletion:
    """Tests for RegressionFixer.handle_fix_completion.

    Issue: #3.3b.2 — Confidence-Gated Auto-Merge for Fixes

    Coverage:
    - Happy path: gate passes + merge succeeds → "fixed"
    - Gate passes + merge fails → "needs_review"
    - Score exactly at threshold (0.95) → gate passes
    - Score just below threshold (0.949) → gate not met → "needs_review"
    - Score = 1.0 (maximum) → gate passes
    - scoring_status != "passed" even with high score → "needs_review"
    - scoring_status is None → "needs_review"
    - scoring_score is None → "needs_review"
    - Both score and status missing → "needs_review"
    - No branch_name in fix_run → merge not attempted → "needs_review"
    - DB update called with correct status after fixed
    - DB update called with correct status after needs_review
    - DB update exception handled gracefully (no re-raise)
    - _merge_pr invoked with correct branch name
    - _merge_pr invoked with correct repo_slug
    - scoring_status = "failed" with high score → gate not met
    - Return value is a plain string (not RegressionStatus enum)
    """

    def _make_passing_run(
        self,
        score: float = 0.97,
        scoring_status: str = "passed",
        branch: str = "fix/regression-abcdef12-12345678",
    ) -> dict:
        """Return a fix_run dict that passes the confidence gate."""
        return {
            "scoring_score": score,
            "scoring_status": scoring_status,
            "branch_name": branch,
        }

    def test_gate_passed_merge_succeeds_returns_fixed(self, fixer, mock_db):
        """Score >= 0.95 + status 'passed' + successful merge → 'fixed'."""
        fix_run = self._make_passing_run()
        with patch.object(fixer, "_merge_pr", return_value=True):
            result = fixer.handle_fix_completion("reg-001", fix_run, mock_db)
        assert result == RegressionStatus.FIXED.value

    def test_gate_passed_merge_fails_returns_needs_review(self, fixer, mock_db):
        """Confidence gate passed but merge fails → 'needs_review'."""
        fix_run = self._make_passing_run()
        with patch.object(fixer, "_merge_pr", return_value=False):
            result = fixer.handle_fix_completion("reg-001", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value

    def test_score_exactly_at_threshold_passes_gate(self, fixer, mock_db):
        """Score == 0.95 (boundary) should pass the gate."""
        fix_run = self._make_passing_run(score=0.95)
        with patch.object(fixer, "_merge_pr", return_value=True):
            result = fixer.handle_fix_completion("reg-002", fix_run, mock_db)
        assert result == RegressionStatus.FIXED.value

    def test_score_just_below_threshold_fails_gate(self, fixer, mock_db):
        """Score == 0.949 (just below threshold) → 'needs_review'."""
        fix_run = self._make_passing_run(score=0.949)
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-003", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_score_maximum_passes_gate(self, fixer, mock_db):
        """Score == 1.0 (max) should pass the gate."""
        fix_run = self._make_passing_run(score=1.0)
        with patch.object(fixer, "_merge_pr", return_value=True):
            result = fixer.handle_fix_completion("reg-004", fix_run, mock_db)
        assert result == RegressionStatus.FIXED.value

    def test_scoring_status_failed_with_high_score_fails_gate(self, fixer, mock_db):
        """High score but scoring_status='failed' → gate not met → 'needs_review'."""
        fix_run = self._make_passing_run(score=0.99, scoring_status="failed")
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-005", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_scoring_status_none_fails_gate(self, fixer, mock_db):
        """scoring_status=None → gate not met → 'needs_review'."""
        fix_run = {"scoring_score": 0.99, "scoring_status": None, "branch_name": "fix/x"}
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-006", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_scoring_score_none_fails_gate(self, fixer, mock_db):
        """scoring_score=None → gate not met → 'needs_review'."""
        fix_run = {"scoring_score": None, "scoring_status": "passed", "branch_name": "fix/x"}
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-007", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_both_score_and_status_missing_fails_gate(self, fixer, mock_db):
        """Empty fix_run (no score or status) → 'needs_review'."""
        fix_run = {}
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-008", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_no_branch_name_skips_merge_returns_needs_review(self, fixer, mock_db):
        """Gate passes but branch_name missing → merge skipped → 'needs_review'."""
        fix_run = {"scoring_score": 0.97, "scoring_status": "passed"}
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            result = fixer.handle_fix_completion("reg-009", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value
        mock_merge.assert_not_called()

    def test_db_updated_with_fixed_status(self, fixer, mock_db):
        """DB update must be called with status='fixed' on successful merge."""
        fix_run = self._make_passing_run()
        with patch.object(fixer, "_merge_pr", return_value=True):
            fixer.handle_fix_completion("reg-010", fix_run, mock_db)
        mock_db.update_regression.assert_called_once_with(
            "reg-010", status=RegressionStatus.FIXED.value
        )

    def test_db_updated_with_needs_review_status(self, fixer, mock_db):
        """DB update must be called with status='needs_review' when gate not met."""
        fix_run = {"scoring_score": 0.5, "scoring_status": "passed", "branch_name": "fix/x"}
        with patch.object(fixer, "_merge_pr", return_value=True):
            fixer.handle_fix_completion("reg-011", fix_run, mock_db)
        mock_db.update_regression.assert_called_once_with(
            "reg-011", status=RegressionStatus.NEEDS_REVIEW.value
        )

    def test_db_update_exception_does_not_propagate(self, fixer):
        """DB update failures must be swallowed (log + continue)."""
        bad_db = MagicMock()
        bad_db.update_regression.side_effect = RuntimeError("DB gone")
        fix_run = self._make_passing_run()
        with patch.object(fixer, "_merge_pr", return_value=True):
            # Should not raise.
            result = fixer.handle_fix_completion("reg-012", fix_run, bad_db)
        # Return value is still the decided status.
        assert result == RegressionStatus.FIXED.value

    def test_merge_pr_called_with_correct_branch(self, fixer, mock_db):
        """_merge_pr must be called with the branch_name from fix_run."""
        branch = "fix/regression-deadbeef-cafef00d"
        fix_run = self._make_passing_run(branch=branch)
        with patch.object(fixer, "_merge_pr", return_value=True) as mock_merge:
            fixer.handle_fix_completion("reg-013", fix_run, mock_db)
        mock_merge.assert_called_once_with(branch)

    def test_return_value_is_string(self, fixer, mock_db):
        """Return value must be a plain str, not a RegressionStatus enum instance."""
        fix_run = self._make_passing_run()
        with patch.object(fixer, "_merge_pr", return_value=True):
            result = fixer.handle_fix_completion("reg-014", fix_run, mock_db)
        assert isinstance(result, str)
        assert not isinstance(result, RegressionStatus)

    def test_needs_review_return_value_is_string(self, fixer, mock_db):
        """Return value is a plain str for needs_review path too."""
        fix_run = {"scoring_score": 0.3, "scoring_status": "failed"}
        result = fixer.handle_fix_completion("reg-015", fix_run, mock_db)
        assert isinstance(result, str)
        assert not isinstance(result, RegressionStatus)

    def test_merge_pr_subprocess_called_with_correct_repo_slug(self, fixer, mock_db):
        """_merge_pr must invoke gh with the correct --repo slug."""
        fix_run = self._make_passing_run(branch="fix/regression-aabbccdd-11223344")
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed()) as mock_run:
            fixer.handle_fix_completion("reg-016", fix_run, mock_db)
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        assert "org/repo" in cmd

    def test_merge_pr_returns_false_on_nonzero_returncode(self, fixer, mock_db):
        """When gh returns non-zero, _merge_pr returns False → needs_review."""
        fix_run = self._make_passing_run()
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=_make_completed(returncode=1, stderr="PR not found")):
            result = fixer.handle_fix_completion("reg-017", fix_run, mock_db)
        assert result == RegressionStatus.NEEDS_REVIEW.value


# ---------------------------------------------------------------------------
# TestHandleFixCompletion
# ---------------------------------------------------------------------------


class TestHandleFixCompletion:
    """Tests for RegressionFixer.handle_fix_completion.

    Issue: #3.3b.2 — confidence-gated auto-merge for regression fixes
    """

    REGRESSION_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
    COMMIT_SHA = "deadbeef12345678"

    def _make_db(self, commit_sha: str = COMMIT_SHA, get_raises: bool = False,
                 get_returns_none: bool = False, update_raises: bool = False):
        """Return a mock DB with get_regression pre-configured."""
        db = MagicMock()
        if get_raises:
            db.get_regression.side_effect = RuntimeError("DB error")
        elif get_returns_none:
            db.get_regression.return_value = None
        else:
            db.get_regression.return_value = {"commit_sha": commit_sha, "id": self.REGRESSION_ID}
        if update_raises:
            db.update_regression.side_effect = RuntimeError("update failed")
        return db

    def _merge_ok(self):
        return _make_completed(stdout="", stderr="", returncode=0)

    def _merge_fail(self):
        return _make_completed(stdout="", stderr="PR not found", returncode=1)

    # --- Gate passes ---

    def test_gate_passes_returns_fixed(self, fixer):
        """score=0.95, status='passed', merge succeeds → returns 'fixed'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()):
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "fixed"

    def test_gate_passes_updates_db_to_fixed(self, fixer):
        """When gate passes and merge succeeds, DB is updated to 'fixed'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()):
            fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        db.update_regression.assert_called_once_with(self.REGRESSION_ID, status="fixed")

    def test_gate_passes_merges_pr(self, fixer):
        """When gate passes, _merge_pr is invoked with correct gh args."""
        db = self._make_db(commit_sha=self.COMMIT_SHA)
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()) as mock_run:
            fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "pr" in cmd
        assert "merge" in cmd
        expected_branch = f"fix/regression-{self.COMMIT_SHA[:8]}-{self.REGRESSION_ID[:8]}"
        assert expected_branch in cmd
        assert "--squash" in cmd
        assert "--repo" in cmd
        assert "org/repo" in cmd
        assert "--yes" in cmd

    def test_score_above_threshold(self, fixer):
        """score=0.99 (above threshold) should also pass gate → 'fixed'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.99, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()):
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "fixed"

    def test_score_exactly_threshold_passes(self, fixer):
        """score == CONFIDENCE_THRESHOLD should pass the gate."""
        db = self._make_db()
        fix_run = {"scoring_score": RegressionFixer.CONFIDENCE_THRESHOLD,
                   "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()):
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "fixed"

    # --- Gate fails ---

    def test_score_below_threshold_returns_needs_review(self, fixer):
        """score=0.94 (below threshold) → 'needs_review', no merge."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.94, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    def test_status_not_passed_returns_needs_review(self, fixer):
        """scoring_status='failed', score above threshold → 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.99, "scoring_status": "failed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    def test_scoring_status_none_returns_needs_review(self, fixer):
        """scoring_status=None → gate fails → 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.99, "scoring_status": None}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    def test_score_none_returns_needs_review(self, fixer):
        """scoring_score=None → gate fails → 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": None, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    def test_both_none_returns_needs_review(self, fixer):
        """Both None → gate fails → 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": None, "scoring_status": None}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    # --- Merge failure ---

    def test_merge_failure_falls_back_to_needs_review(self, fixer):
        """Gate passes but merge fails → falls back to 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_fail()):
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"

    def test_merge_failure_updates_db_needs_review(self, fixer):
        """On merge failure, DB is updated to 'needs_review'."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_fail()):
            fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        db.update_regression.assert_called_once_with(self.REGRESSION_ID, status="needs_review")

    # --- Branch reconstruction ---

    def test_branch_name_reconstructed_correctly(self, fixer):
        """Branch name must be fix/regression-{sha[:8]}-{reg_id[:8]}."""
        db = self._make_db(commit_sha="deadbeef12345678")
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        reg_id = "12345678-aaaa-bbbb-cccc-dddddddddddd"
        with patch("orchestration_engine.regression.subprocess.run",
                   return_value=self._merge_ok()) as mock_run:
            fixer.handle_fix_completion(reg_id, fix_run, db)
        cmd = mock_run.call_args[0][0]
        assert "fix/regression-deadbeef-12345678" in cmd

    # --- DB edge cases ---

    def test_regression_not_in_db_graceful(self, fixer):
        """get_regression returns None → gracefully returns 'needs_review'."""
        db = self._make_db(get_returns_none=True)
        fix_run = {"scoring_score": 0.95, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"
        mock_run.assert_not_called()

    def test_db_update_failure_does_not_raise(self, fixer):
        """DB update_regression raising should not propagate."""
        db = self._make_db(update_raises=True)
        fix_run = {"scoring_score": 0.94, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run"):
            # Must not raise
            result = fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        assert result == "needs_review"

    def test_no_merge_called_below_threshold(self, fixer):
        """Subprocess must not be called when score is below threshold."""
        db = self._make_db()
        fix_run = {"scoring_score": 0.50, "scoring_status": "passed"}
        with patch("orchestration_engine.regression.subprocess.run") as mock_run:
            fixer.handle_fix_completion(self.REGRESSION_ID, fix_run, db)
        mock_run.assert_not_called()
