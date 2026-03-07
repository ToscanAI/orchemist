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
