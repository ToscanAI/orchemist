"""Tests for Issue #5.1.4 — pipeline result → PR/comment posting.

Covers:
- create_pr_for_issue: success path returns PR URL
- create_pr_for_issue: gh failure returns None
- create_pr_for_issue: FileNotFoundError returns None
- create_pr_for_issue: body contains 'Closes #<issue_number>'
- post_pipeline_result_comment: delegates to post_github_comment
- post_pipeline_result_comment: comment contains classification_type and run_id
- post_pipeline_result_comment: returns None when post_github_comment returns None
- post_failure_summary_comment: basic failure (no diagnosis)
- post_failure_summary_comment: with dict diagnosis (failure_class, remediation, confidence)
- post_failure_summary_comment: with object diagnosis (attribute access)
- post_failure_summary_comment: partial diagnosis (missing fields are skipped)
- post_failure_summary_comment: returns None when post_github_comment returns None
- db.get_issue_classification_by_run_id: returns row when run_id matches
- db.get_issue_classification_by_run_id: returns None when no match
- db.get_issue_classification_by_run_id: returns most recent row on duplicates
- _post_github_result_hook: skips when no issue_number in input
- _post_github_result_hook: skips when no repo in input
- _post_github_result_hook: posts failure comment on status='failed'
- _post_github_result_hook: creates PR for 'bug' classification on success
- _post_github_result_hook: creates PR for 'feature' classification on success
- _post_github_result_hook: creates PR for 'refactor' classification on success
- _post_github_result_hook: posts result comment for 'content' classification
- _post_github_result_hook: posts result comment for 'docs' classification
- _post_github_result_hook: posts result comment for 'research' classification
- _post_github_result_hook: uses fallback branch name when not in input
- _post_github_result_hook: non-fatal on unexpected exception
- _post_github_result_hook: uses DB classification_type over initial_input

All tests are independent — no shared mutable state, no real LLM calls,
no real subprocess invocations.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.issue_automation import (
    create_pr_for_issue,
    post_pipeline_result_comment,
    post_failure_summary_comment,
    post_github_comment,
)
from orchestration_engine.db import Database
from orchestration_engine.daemon import _post_github_result_hook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Create a fresh temp-file Database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(tmp.name)


def _insert_ipm_row(
    db: Database,
    run_id: str,
    issue_number: int = 42,
    repo: str = "owner/repo",
    classification_type: str = "feature",
) -> int:
    """Insert a minimal issue_pipeline_map row and return its row id."""
    return db.insert_issue_classification(
        data={
            "issue_number": issue_number,
            "repo": repo,
            "classification_type": classification_type,
            "confidence": 0.90,
            "reasoning": "test",
            "pipeline_template": "coding-pipeline-v1.yaml",
            "run_id": run_id,
            "status": "launched",
        }
    )


def _completed_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock CompletedProcess."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# create_pr_for_issue
# ---------------------------------------------------------------------------


class TestCreatePrForIssue:
    def test_success_returns_pr_url(self):
        pr_url = "https://github.com/owner/repo/pull/99"
        mock_result = _completed_result(returncode=0, stdout=pr_url + "\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            url = create_pr_for_issue(
                repo="owner/repo",
                issue_number=42,
                branch_name="feat/my-branch",
                title="My PR Title",
                body="Description.",
            )
        assert url == pr_url

    def test_failure_returns_none(self):
        mock_result = _completed_result(returncode=1, stderr="error: already exists")
        with patch("subprocess.run", return_value=mock_result):
            url = create_pr_for_issue(
                "owner/repo", 42, "feat/branch", "Title", "Body"
            )
        assert url is None

    def test_file_not_found_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            url = create_pr_for_issue(
                "owner/repo", 42, "feat/branch", "Title", "Body"
            )
        assert url is None

    def test_timeout_returns_none(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            url = create_pr_for_issue(
                "owner/repo", 42, "feat/branch", "Title", "Body"
            )
        assert url is None

    def test_body_contains_closes_reference(self):
        """The PR body must include 'Closes #<issue_number>'."""
        mock_result = _completed_result(returncode=0, stdout="https://github.com/owner/repo/pull/1\n")
        captured_args = {}
        def _mock_run(cmd, **kwargs):
            captured_args["cmd"] = cmd
            return mock_result
        with patch("subprocess.run", side_effect=_mock_run):
            create_pr_for_issue("owner/repo", 77, "feat/branch", "Title", "Body text")
        # The --body argument should contain 'Closes #77'
        cmd = captured_args["cmd"]
        body_idx = cmd.index("--body") + 1
        assert "Closes #77" in cmd[body_idx]

    def test_uses_correct_gh_pr_create_subcommand(self):
        mock_result = _completed_result(returncode=0, stdout="https://github.com/owner/repo/pull/1\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            create_pr_for_issue("owner/repo", 42, "feat/br", "Title", "Body")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "pr" in cmd
        assert "create" in cmd

    def test_empty_stdout_returns_none(self):
        mock_result = _completed_result(returncode=0, stdout="   ")
        with patch("subprocess.run", return_value=mock_result):
            url = create_pr_for_issue("owner/repo", 42, "feat/br", "Title", "Body")
        assert url is None


# ---------------------------------------------------------------------------
# post_pipeline_result_comment
# ---------------------------------------------------------------------------


class TestPostPipelineResultComment:
    def test_success_returns_url(self):
        comment_url = "https://github.com/owner/repo/issues/42#issuecomment-1"
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=comment_url,
        ) as mock_pgc:
            url = post_pipeline_result_comment(
                repo="owner/repo",
                issue_number=42,
                classification_type="content",
                result_text="Here is the output.",
                run_id="run-abc-123",
            )
        assert url == comment_url

    def test_comment_contains_classification_type(self):
        captured_body = {}
        def _mock_pgc(repo, issue_number, body):
            captured_body["body"] = body
            return "https://github.com/owner/repo/issues/42#issuecomment-1"
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            post_pipeline_result_comment(
                "owner/repo", 42, "research", "Some findings.", "run-xyz"
            )
        assert "research" in captured_body["body"]

    def test_comment_contains_run_id(self):
        captured_body = {}
        def _mock_pgc(repo, issue_number, body):
            captured_body["body"] = body
            return "url"
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            post_pipeline_result_comment(
                "owner/repo", 42, "docs", "Content.", "my-run-id-999"
            )
        assert "my-run-id-999" in captured_body["body"]

    def test_returns_none_when_post_fails(self):
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=None,
        ):
            url = post_pipeline_result_comment(
                "owner/repo", 42, "content", "text", "run-1"
            )
        assert url is None


# ---------------------------------------------------------------------------
# post_failure_summary_comment
# ---------------------------------------------------------------------------


class TestPostFailureSummaryComment:
    def test_basic_failure_no_diagnosis(self):
        captured = {}
        def _mock_pgc(repo, issue_number, body):
            captured["body"] = body
            return "https://github.com/owner/repo/issues/42#issuecomment-2"
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            url = post_failure_summary_comment(
                repo="owner/repo",
                issue_number=42,
                error_message="Phase 'build' timed out",
                run_id="run-fail-1",
            )
        assert url is not None
        assert "Phase 'build' timed out" in captured["body"]
        assert "run-fail-1" in captured["body"]

    def test_with_dict_diagnosis(self):
        captured = {}
        def _mock_pgc(repo, issue_number, body):
            captured["body"] = body
            return "url"
        diagnosis = {
            "failure_class": "timeout",
            "remediation": "Increase phase timeout to 300s",
            "confidence": 0.85,
        }
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            post_failure_summary_comment(
                "owner/repo", 42, "Something broke", "run-2", diagnosis=diagnosis
            )
        body = captured["body"]
        assert "timeout" in body
        assert "Increase phase timeout" in body
        assert "85%" in body

    def test_with_object_diagnosis(self):
        """Diagnosis object accessed via attributes (not dict)."""
        captured = {}
        def _mock_pgc(repo, issue_number, body):
            captured["body"] = body
            return "url"
        diag = MagicMock()
        diag.failure_class = "oom_error"
        diag.remediation = "Reduce batch size"
        diag.confidence = 0.70
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            post_failure_summary_comment(
                "owner/repo", 5, "Memory error", "run-3", diagnosis=diag
            )
        body = captured["body"]
        assert "oom_error" in body
        assert "Reduce batch size" in body
        assert "70%" in body

    def test_partial_diagnosis_skips_missing_fields(self):
        """Diagnosis with only failure_class; remediation and confidence absent."""
        captured = {}
        def _mock_pgc(repo, issue_number, body):
            captured["body"] = body
            return "url"
        diagnosis = {"failure_class": "network"}
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            side_effect=_mock_pgc,
        ):
            post_failure_summary_comment(
                "owner/repo", 1, "Net err", "run-4", diagnosis=diagnosis
            )
        body = captured["body"]
        assert "network" in body
        # Remediation/confidence lines should not appear
        assert "Remediation" not in body
        assert "Confidence" not in body

    def test_returns_none_when_post_fails(self):
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=None,
        ):
            url = post_failure_summary_comment(
                "owner/repo", 1, "error", "run-5"
            )
        assert url is None


# ---------------------------------------------------------------------------
# db.get_issue_classification_by_run_id
# ---------------------------------------------------------------------------


class TestGetIssueClassificationByRunId:
    def test_returns_row_when_match(self):
        db = _make_db()
        _insert_ipm_row(db, run_id="run-aaa", issue_number=10, classification_type="bug")
        row = db.get_issue_classification_by_run_id("run-aaa")
        assert row is not None
        assert row["run_id"] == "run-aaa"
        assert row["issue_number"] == 10
        assert row["classification_type"] == "bug"

    def test_returns_none_when_no_match(self):
        db = _make_db()
        row = db.get_issue_classification_by_run_id("nonexistent-run-id")
        assert row is None

    def test_returns_most_recent_on_duplicates(self):
        """When two rows share the same run_id, the higher id (latest) is returned."""
        db = _make_db()
        # Insert two rows with same run_id but different classification_type
        _insert_ipm_row(db, run_id="run-dup", classification_type="bug")
        _insert_ipm_row(db, run_id="run-dup", classification_type="feature")
        row = db.get_issue_classification_by_run_id("run-dup")
        # Should return the latest insert (feature)
        assert row["classification_type"] == "feature"

    def test_does_not_match_different_run_id(self):
        db = _make_db()
        _insert_ipm_row(db, run_id="run-aaa")
        row = db.get_issue_classification_by_run_id("run-bbb")
        assert row is None


# ---------------------------------------------------------------------------
# _post_github_result_hook (daemon helper)
# ---------------------------------------------------------------------------


def _make_mock_db(classification_type: Optional[str] = None) -> MagicMock:
    """Build a mock DB with get_issue_classification_by_run_id."""
    db = MagicMock()
    if classification_type is not None:
        db.get_issue_classification_by_run_id.return_value = {
            "classification_type": classification_type,
            "run_id": "test-run",
            "issue_number": 42,
            "repo": "owner/repo",
        }
    else:
        db.get_issue_classification_by_run_id.return_value = None
    return db


class TestPostGithubResultHook:
    def test_skips_when_no_issue_number(self):
        """Hook is a no-op when initial_input has no issue_number."""
        db = _make_mock_db("feature")
        with patch("orchestration_engine.issue_automation.post_failure_summary_comment") as mock_fn:
            _post_github_result_hook(
                run_id="run-1",
                db=db,
                initial_input={"repo": "owner/repo"},  # no issue_number
                phase_outputs={},
                final_status="failed",
                error_message="err",
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_fn.assert_not_called()

    def test_skips_when_no_repo(self):
        """Hook is a no-op when initial_input has no repo."""
        db = _make_mock_db("feature")
        with patch("orchestration_engine.issue_automation.post_failure_summary_comment") as mock_fn:
            _post_github_result_hook(
                run_id="run-1",
                db=db,
                initial_input={"issue_number": 42},  # no repo
                phase_outputs={},
                final_status="failed",
                error_message="err",
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_fn.assert_not_called()

    def test_posts_failure_comment_on_failed_status(self):
        db = _make_mock_db("feature")
        with patch(
            "orchestration_engine.issue_automation.post_failure_summary_comment",
            return_value="https://github.com/owner/repo/issues/42#issuecomment-9",
        ) as mock_fail:
            _post_github_result_hook(
                run_id="run-2",
                db=db,
                initial_input={"issue_number": 42, "repo": "owner/repo"},
                phase_outputs={},
                final_status="failed",
                error_message="Something went wrong",
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_fail.assert_called_once()
        kwargs = mock_fail.call_args[1] if mock_fail.call_args.kwargs else {}
        args = mock_fail.call_args[0]
        # Accept both positional and keyword calls
        all_args = list(args) + list(kwargs.values())
        assert any("Something went wrong" in str(a) for a in all_args)

    def test_creates_pr_for_bug_classification(self):
        db = _make_mock_db("bug")
        with patch(
            "orchestration_engine.issue_automation.create_pr_for_issue",
            return_value="https://github.com/owner/repo/pull/1",
        ) as mock_pr:
            _post_github_result_hook(
                run_id="run-3",
                db=db,
                initial_input={
                    "issue_number": 42,
                    "repo": "owner/repo",
                    "branch_name": "fix/bug-branch",
                },
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_pr.assert_called_once()

    def test_creates_pr_for_feature_classification(self):
        db = _make_mock_db("feature")
        with patch(
            "orchestration_engine.issue_automation.create_pr_for_issue",
            return_value="https://github.com/owner/repo/pull/2",
        ) as mock_pr:
            _post_github_result_hook(
                run_id="run-4",
                db=db,
                initial_input={
                    "issue_number": 10,
                    "repo": "owner/repo",
                    "branch_name": "feat/my-feature",
                },
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_pr.assert_called_once()

    def test_creates_pr_for_refactor_classification(self):
        db = _make_mock_db("refactor")
        with patch(
            "orchestration_engine.issue_automation.create_pr_for_issue",
            return_value="https://github.com/owner/repo/pull/3",
        ) as mock_pr:
            _post_github_result_hook(
                run_id="run-5",
                db=db,
                initial_input={
                    "issue_number": 7,
                    "repo": "owner/repo",
                    "branch_name": "refactor/cleanup",
                },
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_pr.assert_called_once()

    def test_posts_result_comment_for_content_classification(self):
        db = _make_mock_db("content")
        with patch(
            "orchestration_engine.issue_automation.post_pipeline_result_comment",
            return_value="https://github.com/owner/repo/issues/42#issuecomment-10",
        ) as mock_comment:
            _post_github_result_hook(
                run_id="run-6",
                db=db,
                initial_input={"issue_number": 42, "repo": "owner/repo"},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_comment.assert_called_once()

    def test_posts_result_comment_for_docs_classification(self):
        db = _make_mock_db("docs")
        with patch(
            "orchestration_engine.issue_automation.post_pipeline_result_comment",
            return_value="url",
        ) as mock_comment:
            _post_github_result_hook(
                run_id="run-7",
                db=db,
                initial_input={"issue_number": 5, "repo": "owner/repo"},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_comment.assert_called_once()

    def test_posts_result_comment_for_research_classification(self):
        db = _make_mock_db("research")
        with patch(
            "orchestration_engine.issue_automation.post_pipeline_result_comment",
            return_value="url",
        ) as mock_comment:
            _post_github_result_hook(
                run_id="run-8",
                db=db,
                initial_input={"issue_number": 3, "repo": "owner/repo"},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_comment.assert_called_once()

    def test_fallback_branch_name_when_missing_from_input(self):
        """When branch_name is absent, falls back to feat/issue-<number>."""
        db = _make_mock_db("feature")
        captured = {}
        def _mock_pr(repo, issue_number, branch_name, title, body):
            captured["branch_name"] = branch_name
            return "url"
        with patch("orchestration_engine.issue_automation.create_pr_for_issue", side_effect=_mock_pr):
            _post_github_result_hook(
                run_id="run-9",
                db=db,
                initial_input={"issue_number": 99, "repo": "owner/repo"},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        assert captured["branch_name"] == "feat/issue-99"

    def test_non_fatal_on_unexpected_exception(self):
        """An exception raised inside the hook must not propagate."""
        db = MagicMock()
        db.get_issue_classification_by_run_id.side_effect = RuntimeError("DB exploded")
        # Should NOT raise
        _post_github_result_hook(
            run_id="run-10",
            db=db,
            initial_input={"issue_number": 1, "repo": "owner/repo"},
            phase_outputs={},
            final_status="success",
            error_message=None,
            diagnosis=None,
            output_dir="/tmp/out",
        )

    def test_uses_db_classification_over_input(self):
        """DB row classification_type overrides initial_input value."""
        db = _make_mock_db("research")  # DB says research
        with patch(
            "orchestration_engine.issue_automation.post_pipeline_result_comment",
            return_value="url",
        ) as mock_comment:
            _post_github_result_hook(
                run_id="run-11",
                db=db,
                initial_input={
                    "issue_number": 42,
                    "repo": "owner/repo",
                    "classification_type": "feature",  # Input says feature — should be ignored
                },
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        # research → post_pipeline_result_comment, NOT create_pr_for_issue
        mock_comment.assert_called_once()

    def test_unrecognised_classification_type_skips(self):
        """An unknown classification_type neither posts a comment nor creates a PR."""
        db = _make_mock_db("unknown_type")
        with (
            patch("orchestration_engine.issue_automation.create_pr_for_issue") as mock_pr,
            patch("orchestration_engine.issue_automation.post_pipeline_result_comment") as mock_cmt,
            patch("orchestration_engine.issue_automation.post_failure_summary_comment") as mock_fail,
        ):
            _post_github_result_hook(
                run_id="run-12",
                db=db,
                initial_input={"issue_number": 42, "repo": "owner/repo"},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir="/tmp/out",
            )
        mock_pr.assert_not_called()
        mock_cmt.assert_not_called()
        mock_fail.assert_not_called()
