"""Tests for postflight checks (Definition of Done) — Issue #476."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.postflight import (
    PostflightCheckItem,
    PostflightChecker,
    PostflightResult,
    ensure_branch_pushed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_input():
    return {
        'issue_title': 'Test issue',
        'issue_body': 'Implement feature X',
        'repo_path': '/tmp/fake-repo',
        'branch_name': 'feat/test',
        'issue_number': '123',
        'repo_url': 'https://github.com/owner/repo',
        'test_command': 'pytest tests/ -x',
    }


# ---------------------------------------------------------------------------
# PostflightResult
# ---------------------------------------------------------------------------

class TestPostflightResult:
    def test_starts_passed(self):
        r = PostflightResult()
        assert r.passed is True
        assert r.warnings == []

    def test_add_warning(self):
        r = PostflightResult()
        r.add_check(PostflightCheckItem(name="test", passed=False, message="warn"))
        assert r.passed is True  # postflight is advisory
        assert len(r.warnings) == 1

    def test_summary(self):
        r = PostflightResult()
        r.add_check(PostflightCheckItem(name="a", passed=True, message="good"))
        r.add_check(PostflightCheckItem(name="b", passed=False, message="meh"))
        s = r.summary()
        assert "✓ a" in s
        assert "⚠ b" in s


# ---------------------------------------------------------------------------
# Phase completeness
# ---------------------------------------------------------------------------

class TestPhaseCompleteness:
    def test_all_phases_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
                completed_phases=['spec', 'implement', 'review', 'test'],
            )
            result = PostflightResult()
            checker._check_phase_completeness(result)
            assert all(c.passed for c in result.checks)

    def test_missing_phases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
                completed_phases=['spec', 'implement'],
            )
            result = PostflightResult()
            checker._check_phase_completeness(result)
            failing = [c for c in result.checks if not c.passed]
            assert len(failing) == 1
            assert "review" in failing[0].message


# ---------------------------------------------------------------------------
# Test regression
# ---------------------------------------------------------------------------

class TestTestRegression:
    def test_no_output_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
            )
            result = PostflightResult()
            checker._check_test_regression(result)
            # Should pass — no evidence of new code
            regression_checks = [c for c in result.checks if c.name == 'test_regression']
            assert len(regression_checks) == 1
            assert regression_checks[0].passed is True

    def test_with_test_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write implement.md with test references
            (Path(tmpdir) / 'implement.md').write_text(
                "Created tests/test_feature.py with 5 test cases"
            )
            (Path(tmpdir) / 'test.json').write_text(json.dumps({
                'result': '47 passed, 0 failed'
            }))
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
            )
            result = PostflightResult()
            checker._check_test_regression(result)
            # Should pass — tests found
            regression_checks = [c for c in result.checks if c.name == 'test_regression']
            assert regression_checks[0].passed is True
            # Should also have test_count
            count_checks = [c for c in result.checks if c.name == 'test_count']
            assert len(count_checks) == 1
            assert "47" in count_checks[0].message


# ---------------------------------------------------------------------------
# GitHub comment
# ---------------------------------------------------------------------------

class TestGitHubComment:
    def test_builds_comment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="abc12345",
                output_dir=Path(tmpdir),
                scoring_passed=True,
                scoring_score=0.95,
                completed_phases=['spec', 'implement', 'review', 'test'],
                elapsed_seconds=185.0,
            )
            result = PostflightResult()
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                checker._build_github_comment(result)

            assert result.github_comment is not None
            assert "abc12345" in result.github_comment
            assert "0.950" in result.github_comment
            assert "PASSED" in result.github_comment
            assert "185s" in result.github_comment

    def test_no_issue_number_skips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data = _valid_input()
            data['issue_number'] = ''
            checker = PostflightChecker(
                input_data=data,
                run_id="abc12345",
                output_dir=Path(tmpdir),
            )
            result = PostflightResult()
            checker._build_github_comment(result)
            assert result.github_comment is None

    def test_gh_failure_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="abc12345",
                output_dir=Path(tmpdir),
                scoring_passed=False,
                scoring_score=0.5,
            )
            result = PostflightResult()
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1, stdout='', stderr='auth error'
                )
                checker._build_github_comment(result)
            # Comment built but post failed
            assert result.github_comment is not None
            assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# Full run_all
# ---------------------------------------------------------------------------

class TestRunAll:
    def test_successful_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
                scoring_passed=True,
                scoring_score=0.97,
                completed_phases=['spec', 'implement', 'review', 'test'],
                elapsed_seconds=200.0,
            )
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                result = checker.run_all()
            assert result.passed is True
            assert result.github_comment is not None

    def test_missing_phases_warns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checker = PostflightChecker(
                input_data=_valid_input(),
                run_id="test123",
                output_dir=Path(tmpdir),
                completed_phases=['spec'],
            )
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                result = checker.run_all()
            # Advisory warnings, still passes
            assert result.passed is True
            assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# TestEnsureBranchPushed — Issue #487
# ---------------------------------------------------------------------------

class TestEnsureBranchPushed:
    """Unit tests for ensure_branch_pushed()."""

    def test_branch_already_on_remote_returns_true(self):
        """ls-remote reports branch exists → no push, returns True."""
        with patch("subprocess.run") as mock_run:
            # Simulate ls-remote finding the branch
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123\trefs/heads/feat/my-branch\n",
                stderr="",
            )
            result = ensure_branch_pushed("/fake/repo", "feat/my-branch")
        assert result is True
        # Only one subprocess call expected (ls-remote, no push)
        assert mock_run.call_count == 1

    def test_branch_not_on_remote_pushes_and_returns_true(self):
        """ls-remote returns empty → push triggered → returns True."""
        ls_result = MagicMock(returncode=0, stdout="", stderr="")
        push_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=[ls_result, push_result]) as mock_run:
            result = ensure_branch_pushed("/fake/repo", "feat/new-branch")

        assert result is True
        assert mock_run.call_count == 2
        # Verify the push command was invoked with --set-upstream
        push_call_args = mock_run.call_args_list[1]
        assert "--set-upstream" in push_call_args[0][0]
        assert "feat/new-branch" in push_call_args[0][0]

    def test_push_failure_returns_false(self):
        """Push exits non-zero → returns False."""
        ls_result = MagicMock(returncode=0, stdout="", stderr="")
        push_result = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: remote rejected",
        )

        with patch("subprocess.run", side_effect=[ls_result, push_result]):
            result = ensure_branch_pushed("/fake/repo", "feat/bad-branch")

        assert result is False

    def test_ls_remote_timeout_returns_false(self):
        """ls-remote timeout → returns False immediately (no push)."""
        import subprocess as _sp

        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="git", timeout=30)):
            result = ensure_branch_pushed("/fake/repo", "feat/my-branch")

        assert result is False

    def test_push_timeout_returns_false(self):
        """Push timeout → returns False."""
        import subprocess as _sp

        ls_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "subprocess.run",
            side_effect=[ls_result, _sp.TimeoutExpired(cmd="git", timeout=60)],
        ):
            result = ensure_branch_pushed("/fake/repo", "feat/my-branch")

        assert result is False

    def test_ls_remote_nonzero_exit_triggers_push(self):
        """ls-remote non-zero exit (no remote at all) → still attempts push."""
        ls_result = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repo")
        push_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=[ls_result, push_result]):
            result = ensure_branch_pushed("/fake/repo", "feat/my-branch")

        assert result is True

    def test_accepts_path_object(self):
        """repo_path may be a pathlib.Path; function converts internally."""
        ls_result = MagicMock(returncode=0, stdout="feat/my-branch\n", stderr="")

        with patch("subprocess.run", return_value=ls_result):
            result = ensure_branch_pushed(Path("/fake/repo"), "feat/my-branch")

        assert result is True
