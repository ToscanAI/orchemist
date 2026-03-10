"""Tests for CI event handler — check_suite triggers regression detection.

Issue #512: Covers the new methods added to RegressionWebhookHandler:
  - _extract_failed_check_names_from_inline
  - _extract_pr_number
  - _fetch_check_run_details
  - _fetch_and_extract_failed_check_names
  - _build_pr_comment
  - _post_pr_comment
  - Extended __init__ (auto_fix_mode, fixer)
  - Extended handle_ci_failure integration
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.regression import (
    Regression,
    RegressionFixer,
    RegressionStatus,
    RegressionWebhookHandler,
    SafetyGuard,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_handler(
    *,
    auto_fix_mode: bool = False,
    fixer=None,
    repo_slug: str = "owner/repo",
) -> RegressionWebhookHandler:
    db = MagicMock()
    git_context = MagicMock()
    detector = MagicMock()
    return RegressionWebhookHandler(
        db=db,
        git_context=git_context,
        detector=detector,
        repo_path=Path("/tmp/repo"),
        repo_slug=repo_slug,
        auto_fix_mode=auto_fix_mode,
        fixer=fixer,
    )


def _make_regression(**kwargs) -> Regression:
    defaults = dict(
        commit_sha="abc123def456",
        ci_run_url="https://github.com/owner/repo/actions/runs/1",
        failure_type="ci_failure",
        affected_files=["src/foo.py", "src/bar.py"],
    )
    defaults.update(kwargs)
    return Regression(**defaults)


def _make_payload(
    conclusion: str = "failure",
    head_sha: str = "abc123",
    check_suite_id: int = 42,
    pull_requests=None,
    check_runs=None,
) -> dict:
    payload: dict = {
        "check_suite": {
            "conclusion": conclusion,
            "head_sha": head_sha,
            "url": "https://api.github.com/repos/owner/repo/check-suites/42",
            "id": check_suite_id,
            "pull_requests": pull_requests or [],
            "check_runs": check_runs or [],
        }
    }
    return payload


# ---------------------------------------------------------------------------
# Class 1: TestRegressionWebhookHandlerInit
# ---------------------------------------------------------------------------


class TestRegressionWebhookHandlerInit:
    def test_default_auto_fix_mode_is_false(self):
        handler = _make_handler()
        assert handler._auto_fix_mode is False

    def test_default_fixer_is_none(self):
        handler = _make_handler()
        assert handler._fixer is None

    def test_auto_fix_mode_true(self):
        fixer = MagicMock()
        handler = _make_handler(auto_fix_mode=True, fixer=fixer)
        assert handler._auto_fix_mode is True
        assert handler._fixer is fixer

    def test_existing_params_still_work(self):
        handler = _make_handler(repo_slug="acme/engine")
        assert handler._repo_slug == "acme/engine"

    def test_auto_fix_mode_keyword_only(self):
        """auto_fix_mode must be passed as keyword argument."""
        db = MagicMock()
        git = MagicMock()
        detector = MagicMock()
        # positional args up to template_id, then keyword-only
        handler = RegressionWebhookHandler(
            db, git, detector, Path("/tmp"), "owner/repo", "ci",
            auto_fix_mode=True,
        )
        assert handler._auto_fix_mode is True


# ---------------------------------------------------------------------------
# Class 2: TestExtractFailedCheckNamesFromInline
# ---------------------------------------------------------------------------


class TestExtractFailedCheckNamesFromInline:
    def test_returns_empty_when_no_check_runs(self):
        payload = _make_payload(check_runs=[])
        assert RegressionWebhookHandler._extract_failed_check_names_from_inline(payload) == []

    def test_returns_only_failed_check_names(self):
        check_runs = [
            {"name": "tests", "conclusion": "failure"},
            {"name": "lint", "conclusion": "success"},
            {"name": "build", "conclusion": "failure"},
        ]
        payload = _make_payload(check_runs=check_runs)
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["tests", "build"]

    def test_includes_timed_out(self):
        check_runs = [
            {"name": "integration", "conclusion": "timed_out"},
        ]
        payload = _make_payload(check_runs=check_runs)
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["integration"]

    def test_skips_runs_without_name(self):
        check_runs = [
            {"name": "", "conclusion": "failure"},
            {"conclusion": "failure"},
        ]
        payload = _make_payload(check_runs=check_runs)
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == []

    def test_null_check_runs_returns_empty(self):
        payload = {"check_suite": {"check_runs": None}}
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == []

    def test_missing_check_suite_returns_empty(self):
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline({})
        assert result == []


# ---------------------------------------------------------------------------
# Class 3: TestExtractPrNumber
# ---------------------------------------------------------------------------


class TestExtractPrNumber:
    def test_returns_pr_number_from_first_pr(self):
        payload = _make_payload(pull_requests=[{"number": 99}, {"number": 100}])
        assert RegressionWebhookHandler._extract_pr_number(payload) == 99

    def test_returns_none_when_no_pull_requests(self):
        payload = _make_payload(pull_requests=[])
        assert RegressionWebhookHandler._extract_pr_number(payload) is None

    def test_returns_none_when_pull_requests_is_null(self):
        payload = {"check_suite": {"pull_requests": None}}
        assert RegressionWebhookHandler._extract_pr_number(payload) is None

    def test_returns_none_when_no_check_suite(self):
        assert RegressionWebhookHandler._extract_pr_number({}) is None

    def test_converts_to_int(self):
        payload = _make_payload(pull_requests=[{"number": "42"}])
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result == 42
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Class 4: TestFetchCheckRunDetails
# ---------------------------------------------------------------------------


class TestFetchCheckRunDetails:
    def test_returns_check_runs_on_success(self):
        handler = _make_handler()
        api_response = {"check_runs": [{"name": "tests", "conclusion": "failure"}]}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(api_response)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = handler._fetch_check_run_details(42)
        assert result == [{"name": "tests", "conclusion": "failure"}]
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "api" in cmd
        assert "repos/owner/repo/check-suites/42/check-runs" in " ".join(cmd)

    def test_returns_empty_on_nonzero_returncode(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Not Found"
        with patch("subprocess.run", return_value=mock_result):
            result = handler._fetch_check_run_details(999)
        assert result == []

    def test_soft_fails_on_timeout(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            result = handler._fetch_check_run_details(42)
        assert result == []

    def test_soft_fails_on_file_not_found(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = handler._fetch_check_run_details(42)
        assert result == []

    def test_soft_fails_on_json_decode_error(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-json"
        with patch("subprocess.run", return_value=mock_result):
            result = handler._fetch_check_run_details(42)
        assert result == []

    def test_returns_empty_list_when_no_check_runs_key(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"total_count": 0})
        with patch("subprocess.run", return_value=mock_result):
            result = handler._fetch_check_run_details(42)
        assert result == []


# ---------------------------------------------------------------------------
# Class 5: TestFetchAndExtractFailedCheckNames
# ---------------------------------------------------------------------------


class TestFetchAndExtractFailedCheckNames:
    def test_uses_inline_when_available(self):
        handler = _make_handler()
        check_runs = [{"name": "tests", "conclusion": "failure"}]
        payload = _make_payload(check_runs=check_runs)
        with patch.object(handler, "_fetch_check_run_details") as mock_fetch:
            result = handler._fetch_and_extract_failed_check_names(payload)
        assert result == ["tests"]
        mock_fetch.assert_not_called()

    def test_falls_back_to_api_when_inline_empty(self):
        handler = _make_handler()
        payload = _make_payload(check_suite_id=77, check_runs=[])
        api_runs = [{"name": "build", "conclusion": "failure"}]
        with patch.object(handler, "_fetch_check_run_details", return_value=api_runs):
            result = handler._fetch_and_extract_failed_check_names(payload)
        assert result == ["build"]

    def test_api_fallback_filters_only_failures(self):
        handler = _make_handler()
        payload = _make_payload(check_suite_id=10, check_runs=[])
        api_runs = [
            {"name": "lint", "conclusion": "success"},
            {"name": "tests", "conclusion": "failure"},
        ]
        with patch.object(handler, "_fetch_check_run_details", return_value=api_runs):
            result = handler._fetch_and_extract_failed_check_names(payload)
        assert result == ["tests"]

    def test_returns_empty_when_no_suite_id_and_no_inline(self):
        handler = _make_handler()
        payload = {"check_suite": {"check_runs": []}}  # no id
        with patch.object(handler, "_fetch_check_run_details") as mock_fetch:
            result = handler._fetch_and_extract_failed_check_names(payload)
        assert result == []
        mock_fetch.assert_not_called()

    def test_passes_suite_id_to_fetch(self):
        handler = _make_handler()
        payload = _make_payload(check_suite_id=55, check_runs=[])
        with patch.object(handler, "_fetch_check_run_details", return_value=[]) as mock_fetch:
            handler._fetch_and_extract_failed_check_names(payload)
        mock_fetch.assert_called_once_with(55)


# ---------------------------------------------------------------------------
# Class 6: TestBuildPrComment
# ---------------------------------------------------------------------------


class TestBuildPrComment:
    def test_contains_regression_id(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert reg.id in body

    def test_contains_commit_sha(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert reg.commit_sha in body

    def test_contains_ci_run_url(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert reg.ci_run_url in body

    def test_lists_failed_checks(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, ["tests", "lint"], False)
        assert "tests" in body
        assert "lint" in body

    def test_no_failed_checks_fallback_message(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert "No check run details available" in body

    def test_auto_fix_enabled_message(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], True)
        assert "Auto-fix is enabled" in body or "auto-fix" in body.lower()

    def test_auto_fix_disabled_message(self):
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert "Auto-fix is disabled" in body or "manual review" in body.lower()

    def test_lists_affected_files(self):
        reg = _make_regression(affected_files=["src/main.py"])
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert "src/main.py" in body

    def test_empty_affected_files_fallback(self):
        reg = _make_regression(affected_files=[])
        body = RegressionWebhookHandler._build_pr_comment(reg, [], False)
        assert "No affected files identified" in body


# ---------------------------------------------------------------------------
# Class 7: TestPostPrComment
# ---------------------------------------------------------------------------


class TestPostPrComment:
    def test_calls_gh_pr_comment(self):
        handler = _make_handler(repo_slug="owner/repo")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            handler._post_pr_comment(42, "Hello PR!")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "pr" in cmd
        assert "comment" in cmd
        assert "42" in cmd
        assert "--repo" in cmd
        assert "owner/repo" in cmd
        assert "--body" in cmd
        assert "Hello PR!" in cmd

    def test_uses_timeout_30(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            handler._post_pr_comment(1, "body")
        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 30

    def test_soft_fails_on_nonzero_returncode(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("subprocess.run", return_value=mock_result):
            # Should not raise
            handler._post_pr_comment(1, "body")

    def test_soft_fails_on_timeout_expired(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            handler._post_pr_comment(1, "body")  # must not raise

    def test_soft_fails_on_file_not_found(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            handler._post_pr_comment(1, "body")  # must not raise

    def test_soft_fails_on_os_error(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=OSError("broken")):
            handler._post_pr_comment(1, "body")  # must not raise


# ---------------------------------------------------------------------------
# Class 8: TestHandleCiFailureIntegration
# ---------------------------------------------------------------------------


class TestHandleCiFailureIntegration:
    def _make_full_handler(self, auto_fix_mode=False, fixer=None):
        db = MagicMock()
        db.get_last_green_sha.return_value = "green123"
        git_context = MagicMock()
        detector = MagicMock()
        regression = _make_regression()
        detector.detect.return_value = regression
        handler = RegressionWebhookHandler(
            db=db,
            git_context=git_context,
            detector=detector,
            repo_path=Path("/tmp/repo"),
            repo_slug="owner/repo",
            auto_fix_mode=auto_fix_mode,
            fixer=fixer,
        )
        return handler, regression

    def test_posts_pr_comment_when_pr_associated(self):
        handler, regression = self._make_full_handler()
        payload = _make_payload(
            pull_requests=[{"number": 7}],
            check_runs=[{"name": "tests", "conclusion": "failure"}],
        )
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment") as mock_comment, \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            handler.handle_ci_failure(payload)
        mock_comment.assert_called_once()
        pr_num, body = mock_comment.call_args[0]
        assert pr_num == 7
        assert regression.id in body

    def test_skips_pr_comment_when_no_pr(self):
        handler, regression = self._make_full_handler()
        payload = _make_payload(pull_requests=[])
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment") as mock_comment, \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            handler.handle_ci_failure(payload)
        mock_comment.assert_not_called()

    def test_auto_fix_spawned_when_enabled_and_allowed(self):
        fixer = MagicMock()
        fixer.spawn_fix.return_value = "run-abc"
        handler, regression = self._make_full_handler(auto_fix_mode=True, fixer=fixer)
        payload = _make_payload(pull_requests=[])
        guard_mock = MagicMock()
        guard_mock.should_attempt_fix.return_value = (True, "safe to attempt fix")
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment"), \
             patch("orchestration_engine.regression.SafetyGuard", return_value=guard_mock), \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            result = handler.handle_ci_failure(payload)
        fixer.spawn_fix.assert_called_once_with(regression, handler._db, None)
        assert result is regression

    def test_auto_fix_blocked_by_safety_guard(self):
        fixer = MagicMock()
        handler, regression = self._make_full_handler(auto_fix_mode=True, fixer=fixer)
        payload = _make_payload(pull_requests=[])
        guard_mock = MagicMock()
        guard_mock.should_attempt_fix.return_value = (False, "max fix attempts reached (3)")
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment"), \
             patch("orchestration_engine.regression.SafetyGuard", return_value=guard_mock), \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            handler.handle_ci_failure(payload)
        fixer.spawn_fix.assert_not_called()

    def test_auto_fix_not_attempted_when_disabled(self):
        fixer = MagicMock()
        handler, regression = self._make_full_handler(auto_fix_mode=False, fixer=fixer)
        payload = _make_payload(pull_requests=[])
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment"), \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            handler.handle_ci_failure(payload)
        fixer.spawn_fix.assert_not_called()

    def test_returns_regression_on_failure(self):
        handler, regression = self._make_full_handler()
        payload = _make_payload(pull_requests=[])
        with patch.object(handler, "_open_github_issue", return_value=None), \
             patch.object(handler, "_post_pr_comment"), \
             patch("orchestration_engine.regression.TrustCalibrator", create=True):
            result = handler.handle_ci_failure(payload)
        assert result is regression

    def test_returns_none_on_success(self):
        handler, _ = self._make_full_handler()
        payload = _make_payload(conclusion="success")
        result = handler.handle_ci_failure(payload)
        assert result is None
        handler._db.store_green_sha.assert_called_once()

    def test_returns_none_on_cancelled(self):
        handler, _ = self._make_full_handler()
        payload = _make_payload(conclusion="cancelled")
        result = handler.handle_ci_failure(payload)
        assert result is None
