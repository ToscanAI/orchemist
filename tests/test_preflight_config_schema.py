"""Tests for Issue #576: Preflight should use template config_schema.required
and category, not hardcoded coding fields.

Covers:
- Template-aware required field validation
- Empty required list early-exit
- Null/whitespace repo_path skip behavior
- Category-aware non-git directory severity (error vs warning)
- Git subprocess failure as warning (non-blocking)
- Backward compatibility (no config_schema → fallback to 7 coding fields)
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.preflight import REQUIRED_INPUT_FIELDS, PreflightChecker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_checker(input_data, required_fields=None, category=""):
    """Construct a PreflightChecker with optional required_fields and category."""
    return PreflightChecker(
        input_data,
        required_fields=required_fields,
        category=category,
    )


def _get_check(result, name):
    """Return the first CheckItem with the given name, or None."""
    for check in result.checks:
        if check.name == name:
            return check
    return None


# ---------------------------------------------------------------------------
# Field Validation — Template-Aware Required Fields
# ---------------------------------------------------------------------------


class TestTemplateAwareFieldValidation:

    def test_custom_required_fields_all_present_passes(self):
        """All declared required fields present and non-empty → passes."""
        input_data = {'topic': 'AI', 'author_name': 'Nate'}
        checker = _make_checker(input_data, required_fields=['topic', 'author_name'])
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is True
        assert result.passed is True

    def test_custom_required_field_missing_fails_with_field_name(self):
        """Missing declared required field → fails, field name in message."""
        input_data = {'topic': 'AI'}  # 'author_name' absent
        checker = _make_checker(input_data, required_fields=['topic', 'author_name'])
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert result.passed is False
        assert 'author_name' in check.message

    def test_custom_required_field_whitespace_only_fails_with_field_name(self):
        """Whitespace-only value for declared required field → fails."""
        input_data = {'topic': 'AI', 'author_name': '   '}
        checker = _make_checker(input_data, required_fields=['topic', 'author_name'])
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert result.passed is False
        assert 'author_name' in check.message

    def test_empty_required_list_passes_with_no_required_fields_message(self):
        """required_fields=[] → passes immediately with 'No required fields declared'."""
        input_data = {}  # would fail default 7-field check
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is True
        assert 'No required fields declared' in check.message
        assert result.passed is True

    def test_no_required_fields_schema_falls_back_to_default_coding_fields(self, tmp_path):
        """required_fields=None → falls back to REQUIRED_INPUT_FIELDS. All 7 present → passes."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        input_data = {
            'issue_title': 'Fix bug',
            'issue_body': 'Details here',
            'repo_path': str(git_dir),
            'branch_name': 'fix/123',
            'issue_number': '123',
            'repo_url': 'https://github.com/org/repo',
            'test_command': 'pytest',
        }
        checker = _make_checker(input_data, required_fields=None)

        with patch('subprocess.run') as mock_run:
            m = MagicMock()
            m.stdout = ''
            m.returncode = 0
            mock_run.return_value = m
            result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is True

    def test_fallback_coding_fields_missing_field_fails_naming_it(self):
        """required_fields=None (fallback active), missing 'test_command' → fails with name."""
        input_data = {
            'issue_title': 'Fix bug',
            'issue_body': 'Details here',
            'repo_path': '',  # skip git check
            'branch_name': 'fix/123',
            'issue_number': '123',
            'repo_url': 'https://github.com/org/repo',
            # 'test_command' intentionally missing
        }
        checker = _make_checker(input_data, required_fields=None)
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert result.passed is False
        assert 'test_command' in check.message

    def test_non_list_required_falls_back_to_coding_fields(self):
        """Non-list config_schema.required → daemon passes required_fields=None → fallback active."""
        # Simulates daemon behavior: non-list required → pass None to PreflightChecker
        input_data = {'topic': 'AI'}  # only custom field, no coding fields
        checker = _make_checker(input_data, required_fields=None)
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert result.passed is False
        default_fields = set(REQUIRED_INPUT_FIELDS)
        assert any(f in check.message for f in default_fields), (
            f"Expected a default coding field name in error '{check.message}'"
        )

    def test_author_name_missing_error_contains_author_name(self):
        """Missing 'author_name' from declared required fields → message contains 'author_name'."""
        input_data = {'topic': 'AI Research'}
        checker = _make_checker(input_data, required_fields=['topic', 'author_name'])
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert 'author_name' in check.message


# ---------------------------------------------------------------------------
# Git Repository Checks — Category-Aware Behavior
# ---------------------------------------------------------------------------


class TestGitRepositoryChecks:

    def test_missing_repo_path_key_skips_git_check_with_warning(self):
        """No repo_path key → git check skipped, git_readiness passed=True, severity=warning."""
        input_data = {}
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is True
        assert check.severity == 'warning'
        assert 'skipping git checks' in check.message.lower()
        assert result.passed is True
        git_errors = [e for e in result.errors if 'git' in e.lower()]
        assert len(git_errors) == 0

    def test_null_repo_path_skips_git_check_with_warning(self):
        """repo_path=None → git check skipped identically to missing key."""
        input_data = {'repo_path': None}
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is True
        assert check.severity == 'warning'
        assert 'skipping git checks' in check.message.lower()
        assert result.passed is True

    def test_empty_string_repo_path_skips_git_check_with_warning(self):
        """repo_path='' → git check skipped, passed=True, severity=warning."""
        input_data = {'repo_path': ''}
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is True
        assert check.severity == 'warning'
        assert result.passed is True

    def test_whitespace_only_repo_path_skips_git_check_with_warning(self):
        """repo_path='   ' (whitespace) → git check skipped, passed=True, severity=warning."""
        input_data = {'repo_path': '   '}
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is True
        assert check.severity == 'warning'
        assert result.passed is True

    def test_nonexistent_repo_path_fails_with_error(self):
        """Non-existent repo_path → git_readiness passed=False, severity=error."""
        input_data = {'repo_path': '/tmp/does-not-exist-for-issue-576-test'}
        checker = _make_checker(input_data, required_fields=[])
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is False
        assert check.severity == 'error'
        assert result.passed is False

    def test_non_git_dir_code_category_produces_error(self, tmp_path):
        """Non-git dir + category='code' → git_readiness error, result.passed=False."""
        non_git_dir = tmp_path / 'project'
        non_git_dir.mkdir()

        input_data = {'repo_path': str(non_git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='code')
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is False
        assert check.severity == 'error'
        assert result.passed is False

    def test_non_git_dir_absent_category_produces_error(self, tmp_path):
        """Non-git dir + category='' (absent) → behaves like code: error, passed=False."""
        non_git_dir = tmp_path / 'project'
        non_git_dir.mkdir()

        input_data = {'repo_path': str(non_git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='')
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.passed is False
        assert check.severity == 'error'
        assert result.passed is False

    def test_non_git_dir_content_category_produces_warning_not_error(self, tmp_path):
        """Non-git dir + category='content' → warning only, result.passed=True."""
        non_git_dir = tmp_path / 'output'
        non_git_dir.mkdir()

        input_data = {'repo_path': str(non_git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='content')
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.severity == 'warning'
        assert result.passed is True
        git_errors = [e for e in result.errors if 'git_readiness' in e]
        assert len(git_errors) == 0

    def test_non_git_dir_research_category_produces_warning_not_error(self, tmp_path):
        """Non-git dir + category='research' → warning only, result.passed=True."""
        non_git_dir = tmp_path / 'output'
        non_git_dir.mkdir()

        input_data = {'repo_path': str(non_git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='research')
        result = checker.run_all()

        check = _get_check(result, 'git_readiness')
        assert check is not None
        assert check.severity == 'warning'
        assert result.passed is True
        git_errors = [e for e in result.errors if 'git_readiness' in e]
        assert len(git_errors) == 0

    def test_valid_git_repo_clean_tree_passes(self, tmp_path):
        """Valid git repo + clean working tree → git_clean passed=True, result.passed=True."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        input_data = {'repo_path': str(git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='code')

        with patch('subprocess.run') as mock_run:
            def side_effect(cmd, **kwargs):
                m = MagicMock()
                m.stdout = ''
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect
            result = checker.run_all()

        check = _get_check(result, 'git_clean')
        assert check is not None
        assert check.passed is True
        assert result.passed is True

    def test_valid_git_repo_dirty_tree_fails(self, tmp_path):
        """Valid git repo + uncommitted changes → git_clean passed=False, result.passed=False."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        input_data = {'repo_path': str(git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='code')

        with patch('subprocess.run') as mock_run:
            def side_effect(cmd, **kwargs):
                m = MagicMock()
                if 'status' in cmd:
                    m.stdout = 'M  modified_file.py\n'
                    m.returncode = 0
                else:
                    m.stdout = '0'
                    m.returncode = 0
                return m
            mock_run.side_effect = side_effect
            result = checker.run_all()

        check = _get_check(result, 'git_clean')
        assert check is not None
        assert check.passed is False
        assert result.passed is False

    def test_git_subprocess_failure_records_warning_does_not_block_launch(self, tmp_path):
        """Git subprocess failure → git_clean severity=warning, result.passed=True (non-blocking)."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        input_data = {'repo_path': str(git_dir)}
        checker = _make_checker(input_data, required_fields=[], category='code')

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError('git: command not found')
            result = checker.run_all()

        check = _get_check(result, 'git_clean')
        assert check is not None
        assert check.passed is False
        assert check.severity == 'warning'
        assert result.passed is True
        git_clean_errors = [e for e in result.errors if 'git_clean' in e]
        assert len(git_clean_errors) == 0


# ---------------------------------------------------------------------------
# Backward Compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:

    def test_explicit_seven_coding_fields_in_schema_passes(self, tmp_path):
        """All 7 standard fields in required_fields + all present in input → passes."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        all_seven = {
            'issue_title': 'Fix bug',
            'issue_body': 'Description',
            'repo_path': str(git_dir),
            'branch_name': 'fix/123',
            'issue_number': '123',
            'repo_url': 'https://github.com/org/repo',
            'test_command': 'pytest',
        }
        checker = _make_checker(all_seven, required_fields=list(REQUIRED_INPUT_FIELDS))

        with patch('subprocess.run') as mock_run:
            m = MagicMock()
            m.stdout = ''
            m.returncode = 0
            mock_run.return_value = m
            result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is True
        assert result.passed is True

    def test_no_config_schema_behaves_as_before_all_seven_present(self, tmp_path):
        """required_fields=None (no config_schema) + all 7 fields present → passes (pre-#576 behavior)."""
        git_dir = tmp_path / 'repo'
        git_dir.mkdir()
        (git_dir / '.git').mkdir()

        all_seven = {
            'issue_title': 'Add feature',
            'issue_body': 'As a user...',
            'repo_path': str(git_dir),
            'branch_name': 'feat/new',
            'issue_number': '42',
            'repo_url': 'https://github.com/org/project',
            'test_command': 'make test',
        }
        checker = _make_checker(all_seven, required_fields=None)

        with patch('subprocess.run') as mock_run:
            m = MagicMock()
            m.stdout = ''
            m.returncode = 0
            mock_run.return_value = m
            result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is True
        assert result.passed is True

    def test_no_config_schema_behaves_as_before_field_missing_fails(self):
        """required_fields=None (fallback) + 'issue_body' missing → fails with field name."""
        six_fields = {
            'issue_title': 'Add feature',
            'repo_path': 'fake-repo',  # non-existent, but field check fails first
            'branch_name': 'feat/new',
            'issue_number': '42',
            'repo_url': 'https://github.com/org/project',
            'test_command': 'make test',
            # 'issue_body' intentionally missing
        }
        checker = _make_checker(six_fields, required_fields=None)
        result = checker.run_all()

        check = _get_check(result, 'input_fields_present')
        assert check is not None
        assert check.passed is False
        assert result.passed is False
        assert 'issue_body' in check.message
