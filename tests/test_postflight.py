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
