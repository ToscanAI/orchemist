"""Tests for preflight checks (Definition of Ready) — Issue #476."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.preflight import (
    REQUIRED_INPUT_FIELDS,
    CheckItem,
    PreflightChecker,
    PreflightResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_input():
    """Return a valid input dict with all required fields."""
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
# PreflightResult
# ---------------------------------------------------------------------------

class TestPreflightResult:
    def test_starts_passed(self):
        r = PreflightResult()
        assert r.passed is True
        assert r.errors == []
        assert r.warnings == []

    def test_add_passing_check(self):
        r = PreflightResult()
        r.add_check(CheckItem(name="test", passed=True, message="ok"))
        assert r.passed is True
        assert len(r.checks) == 1

    def test_add_failing_error_check(self):
        r = PreflightResult()
        r.add_check(CheckItem(name="test", passed=False, message="bad", severity="error"))
        assert r.passed is False
        assert len(r.errors) == 1

    def test_add_failing_warning_check(self):
        r = PreflightResult()
        r.add_check(CheckItem(name="test", passed=False, message="meh", severity="warning"))
        assert r.passed is True  # warnings don't fail
        assert len(r.warnings) == 1

    def test_summary(self):
        r = PreflightResult()
        r.add_check(CheckItem(name="a", passed=True, message="good"))
        r.add_check(CheckItem(name="b", passed=False, message="bad", severity="error"))
        s = r.summary()
        assert "✓ a" in s
        assert "✗ b" in s


# ---------------------------------------------------------------------------
# Input field checks
# ---------------------------------------------------------------------------

class TestInputFields:
    def test_all_fields_present(self):
        checker = PreflightChecker(_valid_input())
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is True
        assert any(c.name == "input_fields_present" and c.passed for c in result.checks)

    def test_missing_field(self):
        data = _valid_input()
        del data['test_command']
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is False
        assert "test_command" in result.errors[0]

    def test_empty_field(self):
        data = _valid_input()
        data['issue_title'] = ''
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is False
        assert "issue_title" in result.errors[0]

    def test_multiple_missing(self):
        data = _valid_input()
        del data['test_command']
        del data['repo_url']
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is False
        assert "test_command" in result.errors[0]
        assert "repo_url" in result.errors[0]

    def test_custom_required_fields(self):
        data = {'topic': 'AI', 'author': 'Test'}
        checker = PreflightChecker(data, required_fields=['topic', 'author'])
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is True

    def test_empty_required_fields_skips_check(self):
        checker = PreflightChecker({}, required_fields=[])
        result = PreflightResult()
        checker._check_input_fields(result)
        assert result.passed is True


# ---------------------------------------------------------------------------
# Missing placeholder checks
# ---------------------------------------------------------------------------

class TestMissingPlaceholders:
    def test_no_placeholders(self):
        checker = PreflightChecker(_valid_input())
        result = PreflightResult()
        checker._check_missing_placeholders(result)
        assert result.passed is True

    def test_has_placeholder(self):
        data = _valid_input()
        data['test_command'] = '<MISSING:test_command>'
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_missing_placeholders(result)
        assert result.passed is False
        assert "MISSING" in result.errors[0]

    def test_placeholder_in_body(self):
        data = _valid_input()
        data['issue_body'] = 'Run <MISSING:script> first'
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_missing_placeholders(result)
        assert result.passed is False


# ---------------------------------------------------------------------------
# Git readiness checks
# ---------------------------------------------------------------------------

class TestGitReadiness:
    def test_no_repo_path(self):
        data = _valid_input()
        data['repo_path'] = ''
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_git_readiness(result)
        # Should pass with a warning-level skip
        assert result.passed is True

    def test_nonexistent_repo(self):
        data = _valid_input()
        data['repo_path'] = '/tmp/nonexistent-repo-xyz-456'
        checker = PreflightChecker(data)
        result = PreflightResult()
        checker._check_git_readiness(result)
        assert result.passed is False

    def test_real_git_repo_clean(self):
        """Test with a real temporary git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True)
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'],
                           cwd=tmpdir, capture_output=True,
                           env={**os.environ, 'GIT_AUTHOR_NAME': 'Test',
                                'GIT_AUTHOR_EMAIL': 'test@test.com',
                                'GIT_COMMITTER_NAME': 'Test',
                                'GIT_COMMITTER_EMAIL': 'test@test.com'})
            data = _valid_input()
            data['repo_path'] = tmpdir
            checker = PreflightChecker(data)
            result = PreflightResult()
            checker._check_git_readiness(result)
            # git_clean should pass
            clean_checks = [c for c in result.checks if c.name == 'git_clean']
            assert len(clean_checks) == 1
            assert clean_checks[0].passed is True

    def test_real_git_repo_dirty(self):
        """Test dirty working tree detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True)
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'],
                           cwd=tmpdir, capture_output=True,
                           env={**os.environ, 'GIT_AUTHOR_NAME': 'Test',
                                'GIT_AUTHOR_EMAIL': 'test@test.com',
                                'GIT_COMMITTER_NAME': 'Test',
                                'GIT_COMMITTER_EMAIL': 'test@test.com'})
            # Create uncommitted file
            (Path(tmpdir) / 'dirty.txt').write_text('dirty')
            data = _valid_input()
            data['repo_path'] = tmpdir
            checker = PreflightChecker(data)
            result = PreflightResult()
            checker._check_git_readiness(result)
            clean_checks = [c for c in result.checks if c.name == 'git_clean']
            assert len(clean_checks) == 1
            assert clean_checks[0].passed is False


# ---------------------------------------------------------------------------
# Dedup checks
# ---------------------------------------------------------------------------

class TestDedup:
    def test_no_db(self):
        checker = PreflightChecker(_valid_input(), db=None)
        result = PreflightResult()
        checker._check_dedup(result)
        assert result.passed is True

    def test_no_active_runs(self):
        mock_db = MagicMock()
        mock_db.list_pipeline_runs.return_value = []
        checker = PreflightChecker(_valid_input(), db=mock_db)
        result = PreflightResult()
        checker._check_dedup(result)
        assert result.passed is True

    def test_duplicate_detected(self):
        existing_run = {
            'run_id': 'abc12345-full-id',
            'input_json': json.dumps({
                'issue_number': '123',
                'repo_url': 'https://github.com/owner/repo',
            }),
        }
        mock_db = MagicMock()
        mock_db.list_pipeline_runs.return_value = [existing_run]
        checker = PreflightChecker(_valid_input(), db=mock_db)
        result = PreflightResult()
        checker._check_dedup(result)
        # Dedup is a warning, not error
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "abc12345" in result.warnings[0]

    def test_different_issue_no_dup(self):
        existing_run = {
            'run_id': 'abc12345-full-id',
            'input_json': json.dumps({
                'issue_number': '999',
                'repo_url': 'https://github.com/owner/repo',
            }),
        }
        mock_db = MagicMock()
        mock_db.list_pipeline_runs.return_value = [existing_run]
        checker = PreflightChecker(_valid_input(), db=mock_db)
        result = PreflightResult()
        checker._check_dedup(result)
        assert result.passed is True
        assert len(result.warnings) == 0


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_no_dependencies(self):
        checker = PreflightChecker(_valid_input())
        result = PreflightResult()
        checker._check_dependencies(result)
        assert result.passed is True

    def test_depends_on_pattern_detected(self):
        data = _valid_input()
        data['issue_body'] = 'This depends on #452 and requires #453'
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='CLOSED\n', stderr=''
            )
            checker = PreflightChecker(data)
            result = PreflightResult()
            checker._check_dependencies(result)
            assert result.passed is True
            # Should have called gh for both deps
            assert mock_run.call_count == 2

    def test_unresolved_dependency(self):
        data = _valid_input()
        data['issue_body'] = 'Depends on #999'
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='OPEN\n', stderr=''
            )
            checker = PreflightChecker(data)
            result = PreflightResult()
            checker._check_dependencies(result)
            # Warning, not error
            assert result.passed is True
            assert len(result.warnings) == 1
            assert "#999" in result.warnings[0]


# ---------------------------------------------------------------------------
# Full run_all integration
# ---------------------------------------------------------------------------

class TestRunAll:
    def test_valid_input_passes(self):
        """Full preflight with valid input (real temp git repo)."""
        import tempfile, subprocess, os
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True)
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'init'],
                           cwd=tmpdir, capture_output=True,
                           env={**os.environ, 'GIT_AUTHOR_NAME': 'Test',
                                'GIT_AUTHOR_EMAIL': 'test@test.com',
                                'GIT_COMMITTER_NAME': 'Test',
                                'GIT_COMMITTER_EMAIL': 'test@test.com'})
            data = _valid_input()
            data['repo_path'] = tmpdir
            checker = PreflightChecker(data, db=None)
            result = checker.run_all()
            assert result.passed is True
            assert len(result.errors) == 0

    def test_missing_field_fails(self):
        data = _valid_input()
        del data['test_command']
        data['repo_path'] = ''
        checker = PreflightChecker(data, db=None)
        result = checker.run_all()
        assert result.passed is False
        assert any("test_command" in e for e in result.errors)

    def test_placeholder_fails(self):
        data = _valid_input()
        data['test_command'] = '<MISSING:test_command>'
        data['repo_path'] = ''
        checker = PreflightChecker(data, db=None)
        result = checker.run_all()
        assert result.passed is False
