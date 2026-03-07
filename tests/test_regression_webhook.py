"""Tests for RegressionWebhookHandler and register_regression_trigger.

Issue: #3.3a.3 — Webhook Wiring + GitHub Issue Creation

Coverage:
- Database.store_green_sha / get_last_green_sha: upsert, retrieval, missing key
- RegressionWebhookHandler.handle_ci_failure:
    - failure conclusion → detect + open issue + return Regression
    - success conclusion → update green SHA, return None
    - neutral/other conclusions → no-op, return None
    - missing head_sha → no-op, return None
    - no baseline (no last-green SHA) → no-op, return None
    - detector returns None → return None
    - gh CLI failure → regression still returned (issue URL None)
    - unexpected exception → return None (soft failure)
- RegressionWebhookHandler._extract_ci_error_log: extracts from check_runs, body
- RegressionWebhookHandler._open_github_issue: happy path, gh not found, timeout
- register_regression_trigger: creates trigger with correct fields, persists to DB,
    validation error propagated, integrity error propagated
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.db import Database
from orchestration_engine.git_integration import GitConfig, GitContext
from orchestration_engine.regression import (
    Regression,
    RegressionDetector,
    RegressionStatus,
    RegressionWebhookHandler,
    register_regression_trigger,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


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
def handler(db, git_ctx, detector, tmp_path):
    return RegressionWebhookHandler(
        db=db,
        git_context=git_ctx,
        detector=detector,
        repo_path=tmp_path,
        repo_slug="org/repo",
    )


def _ci_payload(conclusion: str, head_sha: str = "abc1234def5678", url: str = "https://ci.example.com/1") -> dict:
    """Build a minimal check_suite.completed payload."""
    return {
        "check_suite": {
            "conclusion": conclusion,
            "head_sha": head_sha,
            "url": url,
        }
    }


# ---------------------------------------------------------------------------
# TestGreenShaDB
# ---------------------------------------------------------------------------


class TestGreenShaDB:
    """Tests for Database.store_green_sha and Database.get_last_green_sha."""

    def test_store_and_retrieve_green_sha(self, db):
        """Basic store + retrieve round-trip."""
        db.store_green_sha("org/repo", "abc123")
        result = db.get_last_green_sha("org/repo")
        assert result == "abc123"

    def test_get_last_green_sha_missing_returns_none(self, db):
        """Returns None when no record exists for the repo slug."""
        result = db.get_last_green_sha("org/nonexistent")
        assert result is None

    def test_store_green_sha_upserts(self, db):
        """Calling store_green_sha twice updates the SHA."""
        db.store_green_sha("org/repo", "old_sha")
        db.store_green_sha("org/repo", "new_sha")
        result = db.get_last_green_sha("org/repo")
        assert result == "new_sha"

    def test_different_repos_are_independent(self, db):
        """Different repo slugs maintain separate SHA records."""
        db.store_green_sha("org/repo-a", "sha_a")
        db.store_green_sha("org/repo-b", "sha_b")
        assert db.get_last_green_sha("org/repo-a") == "sha_a"
        assert db.get_last_green_sha("org/repo-b") == "sha_b"

    def test_store_green_sha_updates_timestamp(self, db):
        """Upserting does not duplicate rows (PRIMARY KEY enforcement)."""
        db.store_green_sha("org/repo", "sha1")
        db.store_green_sha("org/repo", "sha2")
        # Only one row should exist
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute("SELECT COUNT(*) FROM ci_green_shas WHERE repo_slug = 'org/repo'")
            count = cursor.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# TestRegressionWebhookHandlerSuccess
# ---------------------------------------------------------------------------


class TestRegressionWebhookHandlerSuccess:
    """Tests for the 'success' CI conclusion path."""

    def test_success_stores_green_sha(self, handler, db):
        """CI pass → green SHA is stored in the DB."""
        payload = _ci_payload("success", head_sha="greensha123")
        result = handler.handle_ci_failure(payload)
        assert result is None
        assert db.get_last_green_sha("org/repo") == "greensha123"

    def test_success_with_no_head_sha_is_noop(self, handler, db):
        """Success payload without a head_sha is silently skipped."""
        payload = {"check_suite": {"conclusion": "success", "head_sha": None}}
        result = handler.handle_ci_failure(payload)
        assert result is None
        assert db.get_last_green_sha("org/repo") is None


# ---------------------------------------------------------------------------
# TestRegressionWebhookHandlerNoop
# ---------------------------------------------------------------------------


class TestRegressionWebhookHandlerNoop:
    """Tests for no-op conclusions (non-failure, non-success)."""

    @pytest.mark.parametrize("conclusion", ["cancelled", "neutral", "skipped", "stale", "timed_out"])
    def test_noop_conclusions_return_none(self, handler, conclusion):
        """Non-failure/success conclusions are silently ignored."""
        payload = _ci_payload(conclusion)
        result = handler.handle_ci_failure(payload)
        assert result is None

    def test_empty_check_suite_is_noop(self, handler):
        """Payload with no check_suite key → None (no crash)."""
        result = handler.handle_ci_failure({})
        assert result is None


# ---------------------------------------------------------------------------
# TestRegressionWebhookHandlerFailure
# ---------------------------------------------------------------------------


class TestRegressionWebhookHandlerFailure:
    """Tests for the 'failure' CI conclusion path."""

    def test_failure_without_baseline_returns_none(self, handler, db):
        """No last-green SHA in DB → can't detect regression, returns None."""
        payload = _ci_payload("failure", head_sha="failsha")
        result = handler.handle_ci_failure(payload)
        assert result is None

    def test_failure_without_head_sha_returns_none(self, handler, db):
        """Failure payload with no head_sha → returns None."""
        db.store_green_sha("org/repo", "greensha")
        payload = {"check_suite": {"conclusion": "failure", "head_sha": None, "url": ""}}
        result = handler.handle_ci_failure(payload)
        assert result is None

    def test_failure_triggers_detection_and_returns_regression(
        self, handler, db, detector, tmp_path
    ):
        """Happy path: failure conclusion → Regression returned and persisted."""
        # Set a baseline
        db.store_green_sha("org/repo", "last_green_sha")

        # Wire detector with fake git methods
        detector._git.get_commit_range = lambda lg, h, p: ["culprit_sha"]
        detector._git.get_commit_files = lambda sha, p: ["src/engine.py"]

        payload = _ci_payload("failure", head_sha="failing_sha", url="https://ci.example.com/999")
        # Suppress gh CLI call
        with patch("orchestration_engine.regression.subprocess.run", return_value=_make_completed(stdout="https://github.com/org/repo/issues/42")):
            result = handler.handle_ci_failure(payload)

        assert result is not None
        assert isinstance(result, Regression)
        assert result.commit_sha == "culprit_sha"
        assert result.status == RegressionStatus.DETECTED

        # Persisted to DB
        row = db.get_regression(result.id)
        assert row is not None
        assert row["commit_sha"] == "culprit_sha"

    def test_failure_calls_gh_issue_create(self, handler, db, detector, tmp_path):
        """gh issue create is called with the correct arguments."""
        db.store_green_sha("org/repo", "green_sha")
        detector._git.get_commit_range = lambda lg, h, p: ["abc1234"]
        detector._git.get_commit_files = lambda sha, p: ["src/runner.py"]

        payload = _ci_payload("failure", head_sha="head_sha", url="https://ci.example.com/2")

        with patch("orchestration_engine.regression.subprocess.run", return_value=_make_completed(stdout="https://github.com/org/repo/issues/7")) as mock_run:
            handler.handle_ci_failure(payload)

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]  # positional cmd list
        assert call_args[0] == "gh"
        assert "issue" in call_args
        assert "create" in call_args
        assert "--repo" in call_args
        assert "org/repo" in call_args

    def test_failure_gh_cli_not_found_still_returns_regression(
        self, handler, db, detector, tmp_path
    ):
        """When gh CLI is not installed, regression is still returned (soft fail)."""
        db.store_green_sha("org/repo", "green_sha")
        detector._git.get_commit_range = lambda lg, h, p: ["abc1234"]
        detector._git.get_commit_files = lambda sha, p: []

        payload = _ci_payload("failure", head_sha="head_sha")

        with patch("orchestration_engine.regression.subprocess.run", side_effect=FileNotFoundError):
            result = handler.handle_ci_failure(payload)

        assert result is not None
        assert isinstance(result, Regression)

    def test_failure_gh_cli_timeout_still_returns_regression(
        self, handler, db, detector, tmp_path
    ):
        """gh CLI timeout is handled gracefully — regression still returned."""
        db.store_green_sha("org/repo", "green_sha")
        detector._git.get_commit_range = lambda lg, h, p: ["sha1"]
        detector._git.get_commit_files = lambda sha, p: ["file.py"]

        payload = _ci_payload("failure", head_sha="head_sha")

        with patch(
            "orchestration_engine.regression.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
        ):
            result = handler.handle_ci_failure(payload)

        assert result is not None

    def test_failure_gh_returns_nonzero_still_returns_regression(
        self, handler, db, detector, tmp_path
    ):
        """Non-zero gh exit code is logged but regression is still returned."""
        db.store_green_sha("org/repo", "green_sha")
        detector._git.get_commit_range = lambda lg, h, p: ["sha1"]
        detector._git.get_commit_files = lambda sha, p: []

        payload = _ci_payload("failure", head_sha="head_sha")

        with patch(
            "orchestration_engine.regression.subprocess.run",
            return_value=_make_completed(returncode=1, stderr="label not found"),
        ):
            result = handler.handle_ci_failure(payload)

        assert result is not None

    def test_failure_detector_returns_none_returns_none(
        self, handler, db, detector, tmp_path
    ):
        """When detector.detect() returns None (empty range), handler returns None."""
        db.store_green_sha("org/repo", "green_sha")
        detector._git.get_commit_range = lambda lg, h, p: []  # empty range

        payload = _ci_payload("failure", head_sha="head_sha")

        with patch("orchestration_engine.regression.subprocess.run"):
            result = handler.handle_ci_failure(payload)

        assert result is None

    def test_unexpected_exception_returns_none(self, handler, db):
        """Any unexpected exception is caught and None is returned (never raises)."""
        db.store_green_sha("org/repo", "green_sha")
        mock_detector = MagicMock()
        mock_detector.detect.side_effect = RuntimeError("boom")
        handler._detector = mock_detector

        payload = _ci_payload("failure", head_sha="failsha")
        result = handler.handle_ci_failure(payload)
        assert result is None


# ---------------------------------------------------------------------------
# TestExtractCiErrorLog
# ---------------------------------------------------------------------------


class TestExtractCiErrorLog:
    """Tests for RegressionWebhookHandler._extract_ci_error_log."""

    def test_empty_payload_returns_empty_string(self):
        log = RegressionWebhookHandler._extract_ci_error_log({})
        assert log == ""

    def test_extracts_from_check_run_output(self):
        payload = {
            "check_suite": {
                "check_runs": [
                    {
                        "output": {
                            "title": "Tests failed",
                            "summary": "2 tests failed in src/engine.py",
                            "text": "FAIL src/engine.py::test_foo",
                        }
                    }
                ]
            }
        }
        log = RegressionWebhookHandler._extract_ci_error_log(payload)
        assert "src/engine.py" in log
        assert "Tests failed" in log

    def test_extracts_from_body_field(self):
        payload = {"body": "error in src/runner.py line 42"}
        log = RegressionWebhookHandler._extract_ci_error_log(payload)
        assert "src/runner.py" in log

    def test_multiple_check_runs_concatenated(self):
        payload = {
            "check_suite": {
                "check_runs": [
                    {"output": {"summary": "error in file_a.py"}},
                    {"output": {"summary": "error in file_b.py"}},
                ]
            }
        }
        log = RegressionWebhookHandler._extract_ci_error_log(payload)
        assert "file_a.py" in log
        assert "file_b.py" in log

    def test_missing_output_key_is_handled(self):
        payload = {
            "check_suite": {
                "check_runs": [{"name": "lint"}]  # no 'output' key
            }
        }
        log = RegressionWebhookHandler._extract_ci_error_log(payload)
        assert log == ""


# ---------------------------------------------------------------------------
# TestRegisterRegressionTrigger
# ---------------------------------------------------------------------------


class TestRegisterRegressionTrigger:
    """Tests for the register_regression_trigger module-level helper."""

    def test_creates_trigger_in_db(self, db):
        """Trigger is persisted to the DB and retrievable."""
        trigger = register_regression_trigger(
            db=db,
            trigger_id="regression-ci-trigger",
            template_id="fix-regression-v1",
        )
        row = db.get_trigger("regression-ci-trigger")
        assert row is not None
        assert row["template_id"] == "fix-regression-v1"

    def test_returns_trigger_config(self, db):
        """The returned object is a TriggerConfig with expected fields."""
        from orchestration_engine.webhooks import TriggerConfig
        trigger = register_regression_trigger(
            db=db,
            trigger_id="reg-trigger-01",
            template_id="my-template",
        )
        assert isinstance(trigger, TriggerConfig)
        assert trigger.mode == "fire_and_forget"

    def test_trigger_has_correct_filter(self, db):
        """Trigger filter includes action=completed."""
        trigger = register_regression_trigger(
            db=db,
            trigger_id="reg-trigger-02",
            template_id="my-template",
        )
        assert any(
            f.get("action") == "completed" for f in trigger.filters
        )

    def test_trigger_has_input_map(self, db):
        """input_map passes payload through as event_payload."""
        trigger = register_regression_trigger(
            db=db,
            trigger_id="reg-trigger-03",
            template_id="my-template",
        )
        assert "event_payload" in trigger.input_map

    def test_invalid_trigger_id_raises(self, db):
        """Validation error is propagated for an invalid trigger ID."""
        from orchestration_engine.webhooks import TriggerValidationError
        with pytest.raises(TriggerValidationError):
            register_regression_trigger(
                db=db,
                trigger_id="x",  # too short — fails validation
                template_id="my-template",
            )

    def test_duplicate_trigger_id_raises(self, db):
        """IntegrityError is propagated when ID already exists."""
        import sqlite3
        register_regression_trigger(db=db, trigger_id="dup-trigger-id", template_id="t1")
        with pytest.raises(sqlite3.IntegrityError):
            register_regression_trigger(db=db, trigger_id="dup-trigger-id", template_id="t2")

    def test_trigger_is_enabled_by_default(self, db):
        """Newly registered triggers are enabled."""
        trigger = register_regression_trigger(
            db=db,
            trigger_id="enabled-trigger",
            template_id="some-template",
        )
        assert trigger.enabled is True
        row = db.get_trigger("enabled-trigger")
        assert row["enabled"] is True
