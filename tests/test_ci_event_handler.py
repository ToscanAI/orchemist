"""Tests for Issue #512: CI Event Handler — check_suite PR association + auto-fix.

Coverage:
- RegressionWebhookHandler._extract_failed_check_names_from_inline
- RegressionWebhookHandler._extract_pr_number
- RegressionWebhookHandler._build_pr_comment
- RegressionWebhookHandler._post_pr_comment
- RegressionWebhookHandler._fetch_check_run_details
- RegressionWebhookHandler.handle_ci_failure (auto-fix dispatch, PR comment)
- Integration: full failure path with PR number extraction and comment posting
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.db import Database
from orchestration_engine.git_integration import GitConfig, GitContext
from orchestration_engine.regression import (
    Regression,
    RegressionDetector,
    RegressionFixer,
    RegressionStatus,
    RegressionWebhookHandler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _make_regression(
    commit_sha: str = "abc1234",
    ci_run_url: str = "https://ci.example.com/1",
    affected_files: list | None = None,
    fix_attempt_count: int = 0,
) -> Regression:
    """Build a minimal Regression instance for testing."""
    return Regression(
        commit_sha=commit_sha,
        ci_run_url=ci_run_url,
        failure_type="ci_failure",
        affected_files=affected_files if affected_files is not None else ["src/engine.py"],
        fix_attempt_count=fix_attempt_count,
    )


def _failure_payload(
    head_sha: str = "failsha123",
    url: str = "https://ci.example.com/99",
    check_runs: list | None = None,
    pull_requests: list | None = None,
    check_suite_id: int | None = None,
) -> dict:
    """Build a minimal check_suite.completed failure payload."""
    suite: dict = {
        "conclusion": "failure",
        "head_sha": head_sha,
        "url": url,
    }
    if check_runs is not None:
        suite["check_runs"] = check_runs
    if pull_requests is not None:
        suite["pull_requests"] = pull_requests
    if check_suite_id is not None:
        suite["id"] = check_suite_id
    return {"check_suite": suite}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def git_ctx():
    cfg = GitConfig(enabled=False)
    return GitContext(config=cfg, pipeline_id="test-pipe", run_id="run-001")


@pytest.fixture
def detector(db, git_ctx):
    return RegressionDetector(db=db, git_context=git_ctx)


@pytest.fixture
def handler_notify(db, git_ctx, detector, tmp_path):
    return RegressionWebhookHandler(
        db=db,
        git_context=git_ctx,
        detector=detector,
        repo_path=tmp_path,
        repo_slug="org/repo",
        auto_fix_mode="notify-only",
    )


@pytest.fixture
def mock_fixer():
    f = MagicMock(spec=RegressionFixer)
    f.spawn_fix.return_value = "run-abc123"
    return f


@pytest.fixture
def handler_autofix(db, git_ctx, detector, tmp_path, mock_fixer):
    return RegressionWebhookHandler(
        db=db,
        git_context=git_ctx,
        detector=detector,
        repo_path=tmp_path,
        repo_slug="org/repo",
        auto_fix_mode="auto-fix",
        fixer=mock_fixer,
    )


# ---------------------------------------------------------------------------
# TestExtractFailedCheckNames
# ---------------------------------------------------------------------------

class TestExtractFailedCheckNames:
    """Unit tests for _extract_failed_check_names_from_inline (static method)."""

    def test_returns_names_of_failed_runs(self):
        """Payload with one failure and one success → only failure name returned."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"name": "pytest", "conclusion": "failure"},
                    {"name": "build", "conclusion": "success"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["pytest"]

    def test_empty_when_all_pass(self):
        """All runs with conclusion=success → empty list."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"name": "pytest", "conclusion": "success"},
                    {"name": "mypy", "conclusion": "success"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == []

    def test_empty_payload_returns_empty(self):
        """Empty payload → empty list (no crash)."""
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline({})
        assert result == []

    def test_skips_runs_without_name(self):
        """Run dicts missing the 'name' key are excluded."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"conclusion": "failure"},  # no name
                    {"name": "lint", "conclusion": "failure"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["lint"]

    def test_multiple_failures_all_returned(self):
        """Three failed checks → list of three names."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"name": "pytest", "conclusion": "failure"},
                    {"name": "mypy", "conclusion": "failure"},
                    {"name": "lint", "conclusion": "failure"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["pytest", "mypy", "lint"]

    def test_ignores_neutral_conclusion(self):
        """conclusion=neutral is excluded (only 'failure' is counted)."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"name": "ci", "conclusion": "neutral"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == []

    def test_includes_timed_out_conclusion(self):
        """conclusion=timed_out is included alongside failure."""
        payload = {
            "check_suite": {
                "check_runs": [
                    {"name": "slow-test", "conclusion": "timed_out"},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == ["slow-test"]

    def test_empty_check_runs_returns_empty(self):
        """check_runs present but empty → empty list."""
        payload = {"check_suite": {"check_runs": []}}
        result = RegressionWebhookHandler._extract_failed_check_names_from_inline(payload)
        assert result == []


# ---------------------------------------------------------------------------
# TestExtractPrNumber
# ---------------------------------------------------------------------------

class TestExtractPrNumber:
    """Unit tests for _extract_pr_number (static method)."""

    def test_returns_first_pr_number(self):
        """pull_requests with one entry → returns its number."""
        payload = {
            "check_suite": {
                "pull_requests": [{"number": 42, "head": {"sha": "abc"}}]
            }
        }
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result == 42

    def test_returns_none_when_no_pull_requests(self):
        """pull_requests empty list → None."""
        payload = {"check_suite": {"pull_requests": []}}
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result is None

    def test_returns_none_when_key_absent(self):
        """No pull_requests key → None."""
        payload = {"check_suite": {"conclusion": "failure"}}
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result is None

    def test_returns_none_on_type_error(self):
        """pull_requests with non-integer 'number' → None (no crash)."""
        payload = {
            "check_suite": {
                "pull_requests": [{"number": "not-an-int"}]
            }
        }
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result is None

    def test_returns_first_when_multiple_prs(self):
        """Multiple PRs → only the first one's number is returned."""
        payload = {
            "check_suite": {
                "pull_requests": [
                    {"number": 7},
                    {"number": 8},
                ]
            }
        }
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result == 7

    def test_returns_none_on_empty_payload(self):
        """Completely empty payload → None (no crash)."""
        result = RegressionWebhookHandler._extract_pr_number({})
        assert result is None

    def test_int_conversion(self):
        """Number provided as an integer string that can be cast → returns int."""
        payload = {
            "check_suite": {
                "pull_requests": [{"number": 99}]
            }
        }
        result = RegressionWebhookHandler._extract_pr_number(payload)
        assert result == 99
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# TestBuildPrComment
# ---------------------------------------------------------------------------

class TestBuildPrComment:
    """Unit tests for _build_pr_comment (static method)."""

    def test_contains_regression_id(self):
        """Output contains the regression's id."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, ["pytest"], "notify-only")
        assert reg.id in body

    def test_contains_commit_sha(self):
        """Output contains the culprit commit SHA."""
        reg = _make_regression(commit_sha="deadbeef")
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert "deadbeef" in body

    def test_contains_failed_check_names(self):
        """Failed check names appear as bullet points in the output."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(
            reg, ["pytest", "mypy"], "notify-only"
        )
        assert "pytest" in body
        assert "mypy" in body

    def test_shows_notify_only_label(self):
        """auto_fix_mode='notify-only' → body contains 'notify-only'."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert "notify-only" in body

    def test_shows_auto_fix_label(self):
        """auto_fix_mode='auto-fix' → body contains 'auto-fix'."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "auto-fix")
        assert "auto-fix" in body

    def test_empty_checks_shows_none_extracted(self):
        """failed_check_names=[] → body contains '_none extracted_'."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert "_none extracted_" in body

    def test_empty_affected_files_shows_unknown(self):
        """regression.affected_files=[] → body contains '_unknown_'."""
        reg = _make_regression(affected_files=[])
        body = RegressionWebhookHandler._build_pr_comment(reg, ["lint"], "notify-only")
        assert "_unknown_" in body

    def test_affected_files_listed(self):
        """Affected files appear in the body."""
        reg = _make_regression(affected_files=["src/foo.py", "src/bar.py"])
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert "src/foo.py" in body
        assert "src/bar.py" in body

    def test_returns_string(self):
        """Return type is str."""
        reg = _make_regression()
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert isinstance(body, str)

    def test_contains_ci_run_url(self):
        """CI run URL appears in the body."""
        reg = _make_regression(ci_run_url="https://github.com/org/repo/actions/runs/42")
        body = RegressionWebhookHandler._build_pr_comment(reg, [], "notify-only")
        assert "https://github.com/org/repo/actions/runs/42" in body


# ---------------------------------------------------------------------------
# TestPostPrComment
# ---------------------------------------------------------------------------

class TestPostPrComment:
    """Unit tests for _post_pr_comment (instance method)."""

    def test_happy_path_returns_true(self, handler_notify):
        """subprocess.run rc=0 → returns True."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(returncode=0),
        ) as mock_run:
            result = handler_notify._post_pr_comment(42, "body text")
        assert result is True

    def test_returns_true_on_success(self, handler_notify):
        """Alias: returns True on success (explicit assertion)."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(returncode=0),
        ):
            assert handler_notify._post_pr_comment(7, "body") is True

    def test_returns_false_on_nonzero_rc(self, handler_notify):
        """rc=1 → returns False, no exception raised."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(returncode=1, stderr="auth error"),
        ):
            result = handler_notify._post_pr_comment(42, "body")
        assert result is False

    def test_gh_not_found_returns_false(self, handler_notify):
        """FileNotFoundError (gh not installed) → returns False, no raise."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = handler_notify._post_pr_comment(42, "body")
        assert result is False

    def test_timeout_returns_false(self, handler_notify):
        """TimeoutExpired → returns False, no raise."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            result = handler_notify._post_pr_comment(42, "body")
        assert result is False

    def test_calls_correct_repo(self, handler_notify):
        """Command includes --repo with the handler's repo_slug."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            handler_notify._post_pr_comment(42, "body")
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        assert "org/repo" in cmd

    def test_calls_correct_pr_number(self, handler_notify):
        """Command includes the PR number as a string argument."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            handler_notify._post_pr_comment(42, "body")
        cmd = mock_run.call_args[0][0]
        assert "42" in cmd

    def test_command_includes_gh_pr_comment(self, handler_notify):
        """Command starts with 'gh' and includes 'pr' and 'comment'."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            handler_notify._post_pr_comment(42, "body text")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "pr" in cmd
        assert "comment" in cmd


# ---------------------------------------------------------------------------
# TestFetchCheckRunDetails
# ---------------------------------------------------------------------------

class TestFetchCheckRunDetails:
    """Unit tests for _fetch_check_run_details (instance method)."""

    def test_returns_check_runs_list_on_success(self, handler_notify):
        """gh api rc=0 with valid JSON → check_runs list returned."""
        api_response = {
            "check_runs": [
                {"id": 1, "name": "pytest", "conclusion": "failure"},
                {"id": 2, "name": "lint", "conclusion": "success"},
            ]
        }
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(stdout=json.dumps(api_response)),
        ):
            result = handler_notify._fetch_check_run_details(12345)
        assert result is not None
        assert len(result) == 2
        assert result[0]["name"] == "pytest"

    def test_returns_none_on_gh_failure(self, handler_notify):
        """gh api non-zero rc → returns None."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(returncode=1, stderr="not found"),
        ):
            result = handler_notify._fetch_check_run_details(12345)
        assert result is None

    def test_returns_none_on_invalid_json(self, handler_notify):
        """gh api returns unparseable JSON → returns None."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(stdout="not-json", returncode=0),
        ):
            result = handler_notify._fetch_check_run_details(12345)
        assert result is None

    def test_returns_none_when_gh_not_found(self, handler_notify):
        """FileNotFoundError (gh not installed) → returns None."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = handler_notify._fetch_check_run_details(12345)
        assert result is None

    def test_returns_none_on_timeout(self, handler_notify):
        """TimeoutExpired → returns None."""
        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            result = handler_notify._fetch_check_run_details(12345)
        assert result is None


# ---------------------------------------------------------------------------
# TestAutoFixMode
# ---------------------------------------------------------------------------

class TestAutoFixMode:
    """Tests for auto-fix dispatch via handle_ci_failure."""

    def _setup_detector(self, detector, tmp_path):
        """Wire a detector that always finds a culprit commit."""
        detector._git.get_commit_range = lambda lg, h, p: ["culprit_sha"]
        detector._git.get_commit_files = lambda sha, p: ["src/engine.py"]

    def test_notify_only_is_default(self, db, git_ctx, detector, tmp_path):
        """Handler constructed without auto_fix_mode param → defaults to 'notify-only'."""
        h = RegressionWebhookHandler(
            db=db,
            git_context=git_ctx,
            detector=detector,
            repo_path=tmp_path,
            repo_slug="org/repo",
        )
        assert h._auto_fix_mode == "notify-only"

    def test_notify_only_does_not_call_spawn_fix(self, handler_notify, db, detector, mock_fixer, tmp_path):
        """notify-only mode: fixer.spawn_fix is never called."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector, tmp_path)
        handler_notify._fixer = mock_fixer  # inject fixer but keep notify-only mode

        payload = _failure_payload()
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ):
            handler_notify.handle_ci_failure(payload)

        mock_fixer.spawn_fix.assert_not_called()

    def test_auto_fix_calls_spawn_fix_when_allowed(
        self, handler_autofix, db, detector, mock_fixer, tmp_path
    ):
        """auto-fix mode + SafetyGuard allows → mock_fixer.spawn_fix called once."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector, tmp_path)

        payload = _failure_payload()
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ):
            result = handler_autofix.handle_ci_failure(payload)

        assert result is not None
        mock_fixer.spawn_fix.assert_called_once()

    def test_auto_fix_respects_safety_guard_max_attempts(
        self, handler_autofix, db, detector, mock_fixer, tmp_path
    ):
        """Regression with fix_attempt_count >= 3 → spawn_fix NOT called (SafetyGuard blocks)."""
        db.store_green_sha("org/repo", "greensha")
        detector._git.get_commit_range = lambda lg, h, p: ["culprit_sha"]
        detector._git.get_commit_files = lambda sha, p: ["src/engine.py"]

        # Patch detect() to return a regression with fix_attempt_count=3 (at max)
        high_attempt_regression = _make_regression(fix_attempt_count=3)
        with patch.object(detector, "detect", return_value=high_attempt_regression):
            payload = _failure_payload()
            with patch(
                "orchestration_engine.regression.subprocess.run",
                return_value=_make_proc(),
            ):
                result = handler_autofix.handle_ci_failure(payload)

        assert result is not None
        mock_fixer.spawn_fix.assert_not_called()

    def test_auto_fix_with_no_fixer_logs_warning(
        self, db, git_ctx, detector, tmp_path
    ):
        """auto_fix_mode='auto-fix', fixer=None → no crash, returns Regression."""
        db.store_green_sha("org/repo", "greensha")
        detector._git.get_commit_range = lambda lg, h, p: ["culprit_sha"]
        detector._git.get_commit_files = lambda sha, p: ["src/engine.py"]

        h = RegressionWebhookHandler(
            db=db,
            git_context=git_ctx,
            detector=detector,
            repo_path=tmp_path,
            repo_slug="org/repo",
            auto_fix_mode="auto-fix",
            fixer=None,  # no fixer provided
        )
        payload = _failure_payload()
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ):
            result = h.handle_ci_failure(payload)

        assert result is not None
        assert isinstance(result, Regression)

    def test_auto_fix_mode_stored_correctly(self, db, git_ctx, detector, tmp_path, mock_fixer):
        """auto_fix_mode param stored as _auto_fix_mode attribute."""
        h = RegressionWebhookHandler(
            db=db,
            git_context=git_ctx,
            detector=detector,
            repo_path=tmp_path,
            repo_slug="org/repo",
            auto_fix_mode="auto-fix",
            fixer=mock_fixer,
        )
        assert h._auto_fix_mode == "auto-fix"
        assert h._fixer is mock_fixer


# ---------------------------------------------------------------------------
# TestPrAssociationIntegration
# ---------------------------------------------------------------------------

class TestPrAssociationIntegration:
    """Integration tests for the full failure path with PR association."""

    def _setup_detector(self, detector):
        detector._git.get_commit_range = lambda lg, h, p: ["culprit_sha"]
        detector._git.get_commit_files = lambda sha, p: ["src/engine.py"]

    def test_pr_comment_posted_when_pr_associated(self, handler_notify, db, detector):
        """Full failure flow with pull_requests → gh pr comment <number> called."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        payload = _failure_payload(pull_requests=[{"number": 7}])
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            result = handler_notify.handle_ci_failure(payload)

        assert result is not None
        # Find the gh pr comment call among all subprocess.run calls
        pr_comment_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0] == "gh" and "pr" in c[0][0] and "comment" in c[0][0]
        ]
        assert len(pr_comment_calls) >= 1
        cmd = pr_comment_calls[0][0][0]
        assert "7" in cmd

    def test_no_pr_comment_when_no_pr_associated(self, handler_notify, db, detector):
        """pull_requests=[] → gh pr comment NOT called."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        payload = _failure_payload(pull_requests=[])
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            handler_notify.handle_ci_failure(payload)

        pr_comment_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0] == "gh" and "pr" in c[0][0] and "comment" in c[0][0]
        ]
        assert len(pr_comment_calls) == 0

    def test_pr_comment_body_contains_check_names(self, handler_notify, db, detector):
        """Inline check_runs with failures → PR comment body includes their names."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        check_runs = [
            {"name": "pytest", "conclusion": "failure"},
            {"name": "build", "conclusion": "success"},
        ]
        payload = _failure_payload(
            check_runs=check_runs,
            pull_requests=[{"number": 3}],
        )
        posted_bodies = []
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ) as mock_run:
            handler_notify.handle_ci_failure(payload)

        # Collect --body args from gh pr comment calls
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if cmd[0] == "gh" and "pr" in cmd and "comment" in cmd:
                for i, arg in enumerate(cmd):
                    if arg == "--body" and i + 1 < len(cmd):
                        posted_bodies.append(cmd[i + 1])

        assert any("pytest" in b for b in posted_bodies)

    def test_pr_comment_soft_fails_do_not_block_regression(
        self, handler_notify, db, detector
    ):
        """gh pr comment raises FileNotFoundError → Regression still returned."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        payload = _failure_payload(pull_requests=[{"number": 5}])

        def _side_effect(cmd, **kwargs):
            if "pr" in cmd and "comment" in cmd:
                raise FileNotFoundError("gh not found")
            return _make_proc(stdout="https://github.com/org/repo/issues/1")

        with patch("orchestration_engine.regression.subprocess.run", side_effect=_side_effect):
            result = handler_notify.handle_ci_failure(payload)

        assert result is not None
        assert isinstance(result, Regression)

    def test_github_issue_still_created_alongside_pr_comment(
        self, handler_notify, db, detector
    ):
        """Both gh issue create and gh pr comment are called for a PR-associated failure."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        payload = _failure_payload(pull_requests=[{"number": 12}])
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(stdout="https://github.com/org/repo/issues/99"),
        ) as mock_run:
            result = handler_notify.handle_ci_failure(payload)

        assert result is not None
        issue_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0] == "gh" and "issue" in c[0][0] and "create" in c[0][0]
        ]
        pr_comment_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0] == "gh" and "pr" in c[0][0] and "comment" in c[0][0]
        ]
        assert len(issue_calls) >= 1, "gh issue create not called"
        assert len(pr_comment_calls) >= 1, "gh pr comment not called"

    def test_regression_returned_when_no_pr_but_check_suite_id(
        self, handler_notify, db, detector
    ):
        """No PR in payload but check_suite_id present → no PR comment, regression returned."""
        db.store_green_sha("org/repo", "greensha")
        self._setup_detector(detector)

        # check_suite with id but empty pull_requests
        payload = _failure_payload(check_suite_id=99999, pull_requests=[])
        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_proc(),
        ):
            result = handler_notify.handle_ci_failure(payload)

        assert result is not None
