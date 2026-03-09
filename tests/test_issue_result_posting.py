"""Tests for Issue #5.1.3 — Result Posting (post_github_comment and comment building).

Covers:
- post_github_comment(): success path (returncode 0, non-empty stdout)
- post_github_comment(): failure path (non-zero returncode)
- post_github_comment(): subprocess.TimeoutExpired → returns None
- post_github_comment(): FileNotFoundError (gh not found) → returns None
- post_github_comment(): OSError → returns None
- post_github_comment(): empty stdout → returns None
- post_github_comment(): whitespace-only stdout → returns None
- post_github_comment(): verifies correct gh api command arguments
- IssueAutomation._build_comment(): with run_id
- IssueAutomation._build_comment(): without run_id (shows not-launched indicator)
- IssueAutomation._build_comment(): with reasoning
- IssueAutomation._build_comment(): without reasoning
- IssueAutomation._build_comment(): contains classification type
- IssueAutomation._build_comment(): contains template name
- IssueAutomation._build_comment(): contains "Orchemist" branding
- IssueAutomation._build_comment(): confidence shown as percentage
- comment_body included in process() result
- comment_url key present in webhook 202 response
- Module: post_github_comment in __all__ and importable

All tests are independent — no shared state, no real subprocess calls,
no real HTTP calls, no real LLM calls.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.issue_automation import (
    IssueAutomation,
    IssueClassification,
    IssueClassifier,
    InputExtractor,
    TemplateSelector,
    post_github_comment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(
    cls_type: str = "feature",
    confidence: float = 0.9,
    reasoning: str = "",
    template_id: str = "coding-pipeline",
) -> IssueClassification:
    """Create a minimal IssueClassification for testing."""
    return IssueClassification(
        issue_number=1,
        repo="owner/repo",
        classification_type=cls_type,
        confidence=confidence,
        template_id=template_id,
        reasoning=reasoning,
    )


def _make_classifier(cls_type: str = "bug", confidence: float = 0.9) -> IssueClassifier:
    """Return an IssueClassifier backed by a mock executor."""
    mock = MagicMock()
    mock.execute.return_value = json.dumps({
        "classification_type": cls_type,
        "confidence": confidence,
        "reasoning": "Test reasoning.",
    })
    return IssueClassifier(executor=mock)


def _make_automation(cls_type: str = "feature", confidence: float = 0.8) -> IssueAutomation:
    """Return an IssueAutomation with a mock classifier."""
    return IssueAutomation(
        classifier=_make_classifier(cls_type, confidence),
        selector=TemplateSelector(),
        extractor=InputExtractor(),
    )


# ===========================================================================
# Tests: post_github_comment
# ===========================================================================


class TestPostGithubComment:
    """Unit tests for post_github_comment()."""

    def test_success_returns_url(self):
        """Returncode 0 and non-empty stdout → returns the trimmed URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/owner/repo/issues/1#issuecomment-123\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url == "https://github.com/owner/repo/issues/1#issuecomment-123"

    def test_failure_returns_none(self):
        """Non-zero returncode → returns None."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "gh: authentication required"

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_timeout_returns_none(self):
        """subprocess.TimeoutExpired → returns None, no exception raised."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_gh_not_found_returns_none(self):
        """FileNotFoundError (gh CLI not installed) → returns None."""
        with patch("subprocess.run", side_effect=FileNotFoundError("gh: no such file")):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_os_error_returns_none(self):
        """OSError → returns None, no exception raised."""
        with patch("subprocess.run", side_effect=OSError("Broken pipe")):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_empty_stdout_returns_none(self):
        """Returncode 0 but completely empty stdout → returns None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_whitespace_only_stdout_returns_none(self):
        """Returncode 0 but whitespace-only stdout → strip yields empty string → None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   \n\t  \n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_subprocess_called_with_correct_repo(self):
        """The gh api command URL must include the repo path."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/x/y/issues/5#issuecomment-99\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("x/y", 5, "Test body")

        cmd = mock_run.call_args[0][0]
        assert any("repos/x/y/issues/5/comments" in arg for arg in cmd)

    def test_subprocess_called_with_post_method(self):
        """The gh api command must use --method POST."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/x/y/issues/5#issuecomment-99\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("x/y", 5, "Test body")

        cmd = mock_run.call_args[0][0]
        assert "--method" in cmd
        method_idx = cmd.index("--method")
        assert cmd[method_idx + 1] == "POST"

    def test_subprocess_includes_body_field(self):
        """The gh api command must include --field body=<body>."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/x/y/issues/5#issuecomment-99\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("x/y", 5, "My comment body")

        cmd = mock_run.call_args[0][0]
        assert "--field" in cmd
        # Find all --field values
        field_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--field"]
        assert any("body=My comment body" in fv for fv in field_values)

    def test_gh_is_first_command(self):
        """The command should start with 'gh'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/x/y/issues/1#issuecomment-1\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("x/y", 1, "Hello")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert cmd[1] == "api"

    def test_returns_stripped_url(self):
        """Returned URL should be stripped of surrounding whitespace/newlines."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  https://github.com/a/b/issues/3#issuecomment-999  \n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("a/b", 3, "Hi")

        assert url == "https://github.com/a/b/issues/3#issuecomment-999"

    def test_different_repo_and_number_in_command(self):
        """Verify parameterisation — correct issue_number is used in the API path."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/acme/svc/issues/42#issuecomment-7\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("acme/svc", 42, "body")

        cmd = mock_run.call_args[0][0]
        assert any("repos/acme/svc/issues/42/comments" in a for a in cmd)


# ===========================================================================
# Tests: IssueAutomation._build_comment
# ===========================================================================


class TestBuildComment:
    """Unit tests for IssueAutomation._build_comment()."""

    def test_comment_contains_orchemist(self):
        """The comment must mention Orchemist."""
        auto = _make_automation()
        cls = _make_classification("feature")
        comment = auto._build_comment(1, cls, "coding-pipeline", "run123")
        assert "Orchemist" in comment

    def test_comment_contains_classification_type(self):
        """The comment must contain the classification type."""
        auto = _make_automation()
        cls = _make_classification("bug")
        comment = auto._build_comment(1, cls, "coding-pipeline", "run123")
        assert "bug" in comment

    def test_comment_contains_run_id_when_present(self):
        """When run_id is given, it should appear in the comment."""
        auto = _make_automation()
        cls = _make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", "abc12345")
        assert "abc12345" in comment

    def test_comment_no_run_id_shows_not_launched(self):
        """When run_id is None, the comment must indicate the run was not launched."""
        auto = _make_automation()
        cls = _make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", None)
        lower = comment.lower()
        assert "not launched" in lower or "unavailable" in lower

    def test_comment_no_run_id_does_not_contain_fake_id(self):
        """When run_id is None, no run ID string should appear."""
        auto = _make_automation()
        cls = _make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", None)
        assert "abc12345" not in comment

    def test_comment_contains_reasoning_when_present(self):
        """If classification has reasoning, it should appear in the comment."""
        auto = _make_automation()
        cls = _make_classification(reasoning="Clear crash report with stack trace.")
        comment = auto._build_comment(1, cls, "coding-pipeline", "r1")
        assert "Clear crash report with stack trace." in comment

    def test_comment_handles_empty_reasoning(self):
        """Empty reasoning should not cause errors; comment should still be valid."""
        auto = _make_automation()
        cls = _make_classification(reasoning="")
        comment = auto._build_comment(1, cls, "coding-pipeline", "r1")
        assert isinstance(comment, str)
        assert len(comment) > 0

    def test_comment_contains_template_name(self):
        """The comment must contain the selected pipeline template name."""
        auto = _make_automation()
        cls = _make_classification()
        comment = auto._build_comment(1, cls, "content-pipeline", "r1")
        assert "content-pipeline" in comment

    def test_comment_shows_confidence_as_percentage(self):
        """Confidence should be represented as a percentage in the comment."""
        auto = _make_automation()
        cls = _make_classification(confidence=0.87)
        comment = auto._build_comment(1, cls, "coding-pipeline", "r1")
        # Should contain some % representation
        assert "%" in comment

    def test_comment_is_non_empty_string(self):
        """The comment must be a non-empty string."""
        auto = _make_automation()
        cls = _make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", "r1")
        assert isinstance(comment, str)
        assert comment.strip()

    def test_comment_for_docs_classification(self):
        """Works correctly for 'docs' classification type."""
        auto = _make_automation()
        cls = _make_classification(cls_type="docs")
        comment = auto._build_comment(1, cls, "content-pipeline", "docrun")
        assert "docs" in comment
        assert "content-pipeline" in comment

    def test_comment_for_refactor_classification(self):
        """Works correctly for 'refactor' classification type."""
        auto = _make_automation()
        cls = _make_classification(cls_type="refactor")
        comment = auto._build_comment(1, cls, "coding-pipeline", "refrun")
        assert "refactor" in comment


# ===========================================================================
# Tests: comment_body in process() result
# ===========================================================================


class TestProcessCommentBody:
    """Verify that process() always includes a valid comment_body in its result."""

    def test_comment_body_present_in_result(self):
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        assert "comment_body" in result

    def test_comment_body_is_non_empty(self):
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        assert result["comment_body"]
        assert len(result["comment_body"]) > 0

    def test_comment_body_contains_classification(self):
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Bug title")
        assert "bug" in result["comment_body"]

    def test_comment_body_contains_template(self):
        auto = _make_automation("content")
        result = auto.process(issue_number=1, repo="o/r", title="Blog post")
        assert "content-pipeline" in result["comment_body"]

    def test_comment_body_with_run_id_contains_run_id(self):
        """When launcher provides a run_id, comment_body should contain it."""
        mock_launcher = MagicMock(return_value={"run_id": "myrun999"})
        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("feature")
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Feature",
            launcher=mock_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert "myrun999" in result["comment_body"]

    def test_comment_body_without_launcher_shows_not_launched(self):
        """Without a launcher, comment_body should indicate no run was launched."""
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        lower = result["comment_body"].lower()
        assert "not launched" in lower or "unavailable" in lower


# ===========================================================================
# Tests: comment_url in API webhook response
# ===========================================================================


class TestCommentUrlInWebhookResponse:
    """Verify that the webhook 202 response includes comment_url."""

    def _make_client(self):
        from fastapi.testclient import TestClient
        from orchestration_engine.web.api import create_api_app

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        app = create_api_app(db_path=tmp.name)
        return TestClient(app, raise_server_exceptions=True)

    def _issue_payload(
        self,
        action: str = "opened",
        labels: Optional[list] = None,
    ) -> Dict[str, Any]:
        return {
            "action": action,
            "issue": {
                "number": 1,
                "title": "Test issue",
                "body": "Test body",
                "labels": [{"name": lbl} for lbl in (labels or [])],
            },
            "repository": {"full_name": "owner/repo"},
        }

    def test_comment_url_key_present_when_comment_returns_url(self):
        """When post_github_comment returns a URL, it should be in the response."""
        client = self._make_client()
        payload = self._issue_payload(action="opened", labels=["orchemist"])
        comment_url = "https://github.com/owner/repo/issues/1#issuecomment-1"

        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=comment_url,
        ):
            resp = client.post(
                "/api/v1/github/issues",
                json=payload,
                headers={"X-GitHub-Event": "issues"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert "comment_url" in data
        assert data["comment_url"] == comment_url

    def test_comment_url_key_present_when_comment_fails(self):
        """Even when post_github_comment returns None, comment_url key should exist."""
        client = self._make_client()
        payload = self._issue_payload(action="opened", labels=["orchemist"])

        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=None,
        ):
            resp = client.post(
                "/api/v1/github/issues",
                json=payload,
                headers={"X-GitHub-Event": "issues"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert "comment_url" in data
        assert data["comment_url"] is None


# ===========================================================================
# Tests: Module exports
# ===========================================================================


class TestModuleExports:
    """Verify module-level exports for post_github_comment."""

    def test_post_github_comment_in_issue_automation_all(self):
        from orchestration_engine import issue_automation
        assert "post_github_comment" in issue_automation.__all__

    def test_post_github_comment_importable_from_module(self):
        from orchestration_engine.issue_automation import post_github_comment
        assert callable(post_github_comment)

    def test_post_github_comment_in_top_level_all(self):
        import orchestration_engine
        assert "post_github_comment" in orchestration_engine.__all__

    def test_post_github_comment_importable_from_top_level(self):
        from orchestration_engine import post_github_comment
        assert callable(post_github_comment)
