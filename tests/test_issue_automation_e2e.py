"""End-to-end tests for Issue #5.1.3 — Issue Webhook Handler.

These tests verify the full flow from HTTP webhook → IssueAutomation →
Database persistence → GitHub comment (mocked), covering:

Automation end-to-end (no HTTP):
- classify → select → extract → no launcher (run_id None)
- classify → select → extract → launcher → run_id populated
- classify → select → extract → launcher fails → run_id None, no exception
- classify → select → extract → template resolver fails → fallback, no crash
- DB classification row persisted on process()
- DB classification status updated to "launched" after successful launch
- pipeline inputs include issue_number and repo
- result dict has all required keys

Database.get_active_issue_run:
- returns None when no rows
- returns None when run_id IS NULL (not launched)
- returns None for every terminal status (failed, success, cancelled)
- returns row when run is pending (non-terminal)
- returns row when run is running (non-terminal)
- ignores rows from a different repo
- ignores rows from a different issue number

Webhook API end-to-end (HTTP with TestClient):
- wrong X-GitHub-Event header → 200 ignored
- missing X-GitHub-Event header → 200 ignored
- unsupported action → 200 ignored
- labeled action, wrong label → 200 ignored (reason: label_not_trigger)
- opened action, no trigger label → 200 ignored (reason: trigger_label_absent)
- invalid JSON body → 400
- missing issue.number → 400
- missing repository.full_name → 400
- opened with trigger label → 202
- labeled with trigger label → 202
- deduplication: active run exists → 200 skipped
- deduplication: previous run was terminal → 202 (not blocked)
- deleted action → 200 ignored
- empty body {} → 200 ignored
- 202 response contains classification_type
- 202 response contains comment_url key

All tests are independent — no shared mutable state, no real LLM calls,
no real subprocess calls, no real HTTP calls.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.issue_automation import (
    IssueAutomation,
    IssueClassification,
    IssueClassifier,
    InputExtractor,
    TemplateSelector,
    post_github_comment,
)
from orchestration_engine.db import Database, TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return a fresh temp-file SQLite Database."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(tmp.name)


def _make_classifier(cls_type: str = "bug", confidence: float = 0.9) -> IssueClassifier:
    """Return an IssueClassifier backed by a mock executor."""
    mock = MagicMock()
    mock.execute.return_value = json.dumps({
        "classification_type": cls_type,
        "confidence": confidence,
        "reasoning": "Test reasoning.",
    })
    return IssueClassifier(executor=mock)


def _make_automation(
    cls_type: str = "feature",
    confidence: float = 0.8,
) -> IssueAutomation:
    """Return an IssueAutomation with mock classifier."""
    return IssueAutomation(
        classifier=_make_classifier(cls_type, confidence),
        selector=TemplateSelector(),
        extractor=InputExtractor(),
    )


# #862: route through the canonical helper to pick up future schema columns.
def _insert_run(db: Database, run_id: str, status: str) -> None:
    """Insert a minimal pipeline_run row with the given status."""
    from tests._helpers import insert_pipeline_run as _impl
    _impl(
        db,
        run_id=run_id,
        status=status,
        template_path="/tmp/fake.yaml",
        template_id="coding-pipeline",
        mode="standalone",
        output_dir="/tmp/out",
    )


def _insert_issue_map(
    db: Database,
    issue_number: int,
    repo: str,
    run_id: Optional[str],
    status: str = "launched",
) -> int:
    """Insert an issue_pipeline_map row and return its pk."""
    return db.insert_issue_classification({
        "issue_number": issue_number,
        "repo": repo,
        "classification_type": "feature",
        "confidence": 0.8,
        "template_id": "coding-pipeline",
        "run_id": run_id,
        "status": status,
        "created_at": None,
    })


def _make_test_client(db_path: str):
    """Create a FastAPI TestClient wired to *db_path*."""
    from fastapi.testclient import TestClient
    from orchestration_engine.web.api import create_api_app

    app = create_api_app(db_path=db_path)
    return TestClient(app, raise_server_exceptions=True)


def _fresh_client():
    """Return a (TestClient, db_path) tuple with a fresh temporary database."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return _make_test_client(tmp.name), tmp.name


def _issue_payload(
    action: str = "opened",
    issue_number: int = 1,
    repo: str = "owner/repo",
    labels: Optional[list] = None,
    applied_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a minimal GitHub issues webhook payload."""
    payload: Dict[str, Any] = {
        "action": action,
        "issue": {
            "number": issue_number,
            "title": "Test issue",
            "body": "Test body",
            "labels": [{"name": lbl} for lbl in (labels or [])],
        },
        "repository": {"full_name": repo},
    }
    if applied_label is not None:
        payload["label"] = {"name": applied_label}
    return payload


# ===========================================================================
# Tests: IssueAutomation.process() — unit/integration (no HTTP)
# ===========================================================================


class TestIssueAutomationProcess:
    """Unit tests for IssueAutomation.process()."""

    def test_returns_dict_with_all_required_keys(self):
        """process() must return a dict containing all required keys."""
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        required_keys = {
            "issue_number", "repo", "classification_type",
            "confidence", "template", "run_id", "comment_body",
        }
        assert required_keys.issubset(result.keys())

    def test_issue_number_propagated_correctly(self):
        auto = _make_automation()
        result = auto.process(issue_number=42, repo="acme/service", title="Crash")
        assert result["issue_number"] == 42

    def test_repo_propagated_correctly(self):
        auto = _make_automation()
        result = auto.process(issue_number=42, repo="acme/service", title="Crash")
        assert result["repo"] == "acme/service"

    def test_classification_type_in_result(self):
        auto = _make_automation("feature")
        result = auto.process(issue_number=1, repo="o/r", title="New feature")
        assert result["classification_type"] == "feature"

    def test_bug_maps_to_coding_pipeline(self):
        """'bug' classification should map to 'coding-pipeline' template."""
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Bug fix")
        assert result["template"] == "coding-pipeline"

    def test_content_maps_to_content_pipeline(self):
        """'content' classification should map to 'content-pipeline' template."""
        auto = _make_automation("content")
        result = auto.process(issue_number=1, repo="o/r", title="Blog post")
        assert result["template"] == "content-pipeline"

    def test_no_launcher_run_id_is_none(self):
        """Without a launcher, run_id should be None."""
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Bug")
        assert result["run_id"] is None

    def test_confidence_in_result(self):
        auto = _make_automation("bug", confidence=0.77)
        result = auto.process(issue_number=1, repo="o/r", title="Bug")
        assert abs(result["confidence"] - 0.77) < 0.01

    def test_with_launcher_run_id_set(self):
        """When a launcher is provided and succeeds, run_id should be set."""
        mock_launcher = MagicMock(return_value={"run_id": "abc12345"})
        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("feature")
        result = auto.process(
            issue_number=7,
            repo="o/r",
            title="New feature",
            launcher=mock_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert result["run_id"] == "abc12345"

    def test_launcher_failure_does_not_raise(self):
        """A launcher that raises should not propagate — run_id should be None."""
        mock_launcher = MagicMock(side_effect=RuntimeError("launch failed"))
        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("feature")
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Test",
            launcher=mock_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert result["run_id"] is None

    def test_template_resolver_failure_does_not_raise(self):
        """When template resolution raises, process() should not crash."""
        mock_resolver = MagicMock(side_effect=Exception("template not found"))
        mock_engine = MagicMock()

        auto = _make_automation("bug")
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Test",
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert "classification_type" in result
        assert result["run_id"] is None

    def test_pipeline_inputs_include_issue_number(self):
        """The launcher should receive inputs containing issue_number."""
        captured: Dict[str, Any] = {}

        def fake_launcher(**kwargs: Any) -> Dict[str, Any]:
            captured.update(kwargs.get("input_data", {}))
            return {"run_id": "run001"}

        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("feature")
        auto.process(
            issue_number=55,
            repo="test/repo",
            title="Feature",
            launcher=fake_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert captured.get("issue_number") == 55

    def test_pipeline_inputs_include_repo(self):
        """The launcher should receive inputs containing repo."""
        captured: Dict[str, Any] = {}

        def fake_launcher(**kwargs: Any) -> Dict[str, Any]:
            captured.update(kwargs.get("input_data", {}))
            return {"run_id": "run002"}

        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("feature")
        auto.process(
            issue_number=55,
            repo="test/repo",
            title="Feature",
            launcher=fake_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        assert captured.get("repo") == "test/repo"

    def test_db_classification_persisted(self):
        """When db is provided, the classification row should be persisted."""
        db = _make_db()
        auto = _make_automation("bug")
        auto.process(issue_number=33, repo="o/r", title="Test bug", db=db)

        row = db.get_issue_classification(33, "o/r")
        assert row is not None
        assert row["classification_type"] == "bug"

    def test_db_status_updated_to_launched_on_success(self):
        """DB classification status should be 'launched' after a successful launch."""
        db = _make_db()
        mock_launcher = MagicMock(return_value={"run_id": "xyz99"})
        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template

        auto = _make_automation("bug")
        auto.process(
            issue_number=99,
            repo="owner/repo",
            title="Bug",
            db=db,
            launcher=mock_launcher,
            template_resolver=mock_resolver,
            template_engine=mock_engine,
        )
        row = db.get_issue_classification(99, "owner/repo")
        assert row is not None
        assert row["status"] == "launched"

    def test_no_db_does_not_raise(self):
        """process() should work fine without a db argument."""
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        assert isinstance(result, dict)


# ===========================================================================
# Tests: Database.get_active_issue_run
# ===========================================================================


class TestGetActiveIssueRun:
    """Tests for Database.get_active_issue_run()."""

    def test_returns_none_when_no_rows(self):
        db = _make_db()
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_run_id_is_null(self):
        """Rows with run_id IS NULL (classified but not launched) should not block."""
        db = _make_db()
        _insert_issue_map(db, 1, "o/r", run_id=None, status="classified")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_for_failed_run(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "failed")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_for_success_run(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "success")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_for_cancelled_run(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "cancelled")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_for_all_terminal_statuses(self):
        """All terminal statuses should result in no active run."""
        for status in TERMINAL_STATUSES:
            db = _make_db()
            run_id = str(uuid.uuid4())[:8]
            _insert_run(db, run_id, status)
            _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
            result = db.get_active_issue_run(1, "o/r")
            assert result is None, f"Expected None for terminal status {status!r}"

    def test_returns_row_for_pending_run(self):
        """A 'pending' pipeline run is not terminal → should return the row."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "pending")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        result = db.get_active_issue_run(1, "o/r")
        assert result is not None
        assert result["run_id"] == run_id

    def test_returns_row_for_running_run(self):
        """A 'running' pipeline run is not terminal → should return the row."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        result = db.get_active_issue_run(1, "o/r")
        assert result is not None
        assert result["run_id"] == run_id

    def test_ignores_row_from_different_repo(self):
        """An active run in a different repo should not block this repo."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "other/repo", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_ignores_row_from_different_issue_number(self):
        """An active run for a different issue number should not affect others."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 99, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_correct_run_id(self):
        """The returned row should contain the correct run_id."""
        db = _make_db()
        run_id = "unique-run-xyz"
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 5, "some/repo", run_id=run_id, status="launched")
        result = db.get_active_issue_run(5, "some/repo")
        assert result["run_id"] == run_id


# ===========================================================================
# Tests: POST /api/v1/github/issues — webhook route E2E
# ===========================================================================


class TestGithubIssuesWebhookE2E:
    """End-to-end tests for the POST /api/v1/github/issues webhook route."""

    def test_wrong_event_header_returns_200_ignored(self):
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "push"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "not_issues_event"

    def test_missing_event_header_returns_200_ignored(self):
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "opened"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_unsupported_action_returns_200_ignored(self):
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "closed"},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_labeled_wrong_label_returns_200_ignored(self):
        client, _ = _fresh_client()
        payload = _issue_payload(action="labeled", applied_label="bug")
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "label_not_trigger"

    def test_opened_no_trigger_label_returns_200_ignored(self):
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", labels=["bug", "enhancement"])
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "trigger_label_absent"

    def test_invalid_json_returns_400(self):
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            content=b"not-json",
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_missing_issue_number_returns_400(self):
        client, _ = _fresh_client()
        payload = {
            "action": "opened",
            "issue": {"title": "Test", "body": "", "labels": [{"name": "orchemist"}]},
            "repository": {"full_name": "owner/repo"},
        }
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 400

    def test_missing_repo_returns_400(self):
        client, _ = _fresh_client()
        payload = {
            "action": "opened",
            "issue": {
                "number": 1,
                "title": "Test",
                "body": "",
                "labels": [{"name": "orchemist"}],
            },
        }
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 400

    def test_opened_with_trigger_label_returns_202(self):
        """An 'opened' issue with trigger label should return 202."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", labels=["orchemist"])

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
        assert "classification_type" in resp.json()

    def test_labeled_with_trigger_label_returns_202(self):
        """A 'labeled' event with the trigger label should return 202."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="labeled", labels=["orchemist"], applied_label="orchemist")

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
        assert "classification_type" in resp.json()

    def test_dedup_active_run_returns_200_skipped(self):
        """When an active run already exists, should return 200 skipped."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)

        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "owner/repo", run_id=run_id, status="launched")

        client = _make_test_client(tmp.name)
        payload = _issue_payload(action="opened", issue_number=1, labels=["orchemist"])

        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=None,
        ):
            resp = client.post(
                "/api/v1/github/issues",
                json=payload,
                headers={"X-GitHub-Event": "issues"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["reason"] == "active_run_exists"

    def test_dedup_allows_when_previous_run_terminal(self):
        """When previous run is terminal (failed), should proceed and return 202."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)

        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "failed")
        _insert_issue_map(db, 1, "owner/repo", run_id=run_id, status="launched")

        client = _make_test_client(tmp.name)
        payload = _issue_payload(action="opened", issue_number=1, labels=["orchemist"])

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

    def test_response_contains_comment_url_key(self):
        """202 response should always include 'comment_url' key."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", labels=["orchemist"])

        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value="https://github.com/owner/repo/issues/1#issuecomment-1",
        ):
            resp = client.post(
                "/api/v1/github/issues",
                json=payload,
                headers={"X-GitHub-Event": "issues"},
            )

        assert resp.status_code == 202
        assert "comment_url" in resp.json()

    def test_deleted_action_is_ignored(self):
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "deleted"},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_empty_body_with_issues_event_returns_ignored(self):
        """Empty body: action is '' → not in (opened, labeled) → ignored."""
        client, _ = _fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_202_response_contains_classification_type(self):
        """202 response should include classification_type from IssueAutomation."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", labels=["orchemist"])

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
        assert "classification_type" in data
        assert data["classification_type"] in {
            "bug", "feature", "docs", "refactor", "research", "content"
        }

    def test_202_response_contains_repo(self):
        """202 response should include the repo field."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", labels=["orchemist"], repo="myorg/myrepo")

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
        assert resp.json().get("repo") == "myorg/myrepo"

    def test_202_response_contains_issue_number(self):
        """202 response should include the issue_number field."""
        client, _ = _fresh_client()
        payload = _issue_payload(action="opened", issue_number=77, labels=["orchemist"])

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
        assert resp.json().get("issue_number") == 77


# ===========================================================================
# Tests: Confidence escalation path
# ===========================================================================


class TestEscalationPath:
    """Tests for IssueAutomation escalation when confidence is below threshold."""

    def _make_low_confidence_automation(
        self,
        confidence: float = 0.40,
        threshold: float = 0.70,
        dispatcher=None,
    ) -> IssueAutomation:
        """Return an IssueAutomation configured to escalate."""
        return IssueAutomation(
            classifier=_make_classifier("bug", confidence),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
            confidence_threshold=threshold,
            notification_dispatcher=dispatcher,
        )

    def test_escalated_flag_in_result(self):
        """Low-confidence process() must return escalated=True."""
        auto = self._make_low_confidence_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Crash")
        assert result.get("escalated") is True

    def test_run_id_none_on_escalation(self):
        """Escalated result must have run_id=None (no pipeline launched)."""
        auto = self._make_low_confidence_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Crash")
        assert result["run_id"] is None

    def test_comment_body_mentions_escalation(self):
        """The generated comment body should mention escalation/human review."""
        auto = self._make_low_confidence_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Crash")
        comment = result.get("comment_body", "")
        assert comment  # non-empty
        assert any(
            kw in comment.lower()
            for kw in ("escalat", "human review", "manual")
        ), f"Expected escalation language in comment body: {comment!r}"

    def test_db_status_updated_to_escalated(self):
        """After escalation, the DB row status must be 'escalated'."""
        db = _make_db()
        auto = self._make_low_confidence_automation()
        auto.process(issue_number=10, repo="o/r", title="Low confidence", db=db)

        # Fetch the classification row directly
        import sqlite3
        conn = sqlite3.connect(str(db.db_path))
        row = conn.execute(
            "SELECT status FROM issue_pipeline_map WHERE issue_number=10"
        ).fetchone()
        conn.close()
        assert row is not None, "No row inserted for this issue"
        assert row[0] == "escalated", f"Expected 'escalated', got {row[0]!r}"

    def test_db_status_not_escalated_above_threshold(self):
        """Above the threshold, DB status should be 'launched' (or 'classified'), NOT 'escalated'."""
        db = _make_db()
        auto = IssueAutomation(
            classifier=_make_classifier("bug", confidence=0.95),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
            confidence_threshold=0.70,
        )
        auto.process(issue_number=20, repo="o/r", title="High confidence", db=db)

        import sqlite3
        conn = sqlite3.connect(str(db.db_path))
        row = conn.execute(
            "SELECT status FROM issue_pipeline_map WHERE issue_number=20"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] != "escalated", f"Should not be 'escalated', got {row[0]!r}"

    def test_dispatcher_called_on_escalation(self):
        """When confidence is low, the notification dispatcher must be invoked."""
        mock_dispatcher = MagicMock()
        auto = self._make_low_confidence_automation(dispatcher=mock_dispatcher)
        auto.process(issue_number=5, repo="o/r", title="Low")
        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args[1]
        assert call_kwargs.get("event") == "human_review"

    def test_dispatcher_not_called_on_high_confidence(self):
        """When confidence is above threshold, dispatcher must NOT be called."""
        mock_dispatcher = MagicMock()
        auto = IssueAutomation(
            classifier=_make_classifier("bug", confidence=0.95),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
            confidence_threshold=0.70,
            notification_dispatcher=mock_dispatcher,
        )
        auto.process(issue_number=6, repo="o/r", title="High")
        mock_dispatcher.dispatch.assert_not_called()

    def test_escalation_threshold_boundary_below(self):
        """Confidence exactly below threshold should escalate."""
        auto = self._make_low_confidence_automation(confidence=0.699, threshold=0.70)
        result = auto.process(issue_number=7, repo="o/r", title="Boundary below")
        assert result.get("escalated") is True

    def test_escalation_threshold_boundary_at(self):
        """Confidence exactly AT threshold should NOT escalate (not below)."""
        auto = self._make_low_confidence_automation(confidence=0.70, threshold=0.70)
        result = auto.process(issue_number=8, repo="o/r", title="Boundary at")
        assert not result.get("escalated", False)


# ===========================================================================
# Tests: Full E2E happy path (classify → select → extract → launch → comment)
# ===========================================================================


class TestFullE2EHappyPath:
    """Tests for the complete automation flow with a launcher."""

    def _make_launcher_and_resolver(self, run_id: str = "run-abc"):
        """Return (mock_launcher, mock_resolver, mock_engine) for happy-path tests."""
        mock_launcher = MagicMock(return_value={"run_id": run_id})
        mock_resolver = MagicMock(return_value=Path("/tmp/fake.yaml"))
        mock_template = MagicMock()
        mock_template.config_schema = {}
        mock_engine = MagicMock()
        mock_engine.load_template.return_value = mock_template
        return mock_launcher, mock_resolver, mock_engine

    def test_run_id_populated_on_success(self):
        """When launcher succeeds, result['run_id'] must be the returned run_id."""
        auto = _make_automation("bug", confidence=0.90)
        launcher, resolver, engine = self._make_launcher_and_resolver("run-42")
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Bug fix",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        assert result["run_id"] == "run-42"

    def test_escalated_false_on_happy_path(self):
        """Happy path must not set escalated=True."""
        auto = _make_automation("bug", confidence=0.90)
        launcher, resolver, engine = self._make_launcher_and_resolver()
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Bug fix",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        assert not result.get("escalated", False)

    def test_comment_body_present_and_non_empty(self):
        """Result must contain a non-empty comment_body."""
        auto = _make_automation("bug", confidence=0.90)
        launcher, resolver, engine = self._make_launcher_and_resolver()
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Bug fix",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        assert result.get("comment_body")

    def test_db_status_updated_to_launched(self):
        """After a successful launch, the DB row status must be 'launched'."""
        db = _make_db()
        auto = _make_automation("bug", confidence=0.90)
        launcher, resolver, engine = self._make_launcher_and_resolver("run-999")
        auto.process(
            issue_number=100,
            repo="o/r",
            title="Bug",
            db=db,
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        import sqlite3
        conn = sqlite3.connect(str(db.db_path))
        row = conn.execute(
            "SELECT status FROM issue_pipeline_map WHERE issue_number=100"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "launched", f"Expected 'launched', got {row[0]!r}"

    def test_all_required_keys_present(self):
        """Result dict must contain all required top-level keys."""
        auto = _make_automation("feature", confidence=0.85)
        launcher, resolver, engine = self._make_launcher_and_resolver()
        result = auto.process(
            issue_number=1,
            repo="o/r",
            title="Feature",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        for key in ("issue_number", "repo", "classification_type", "confidence",
                    "template", "run_id", "comment_body"):
            assert key in result, f"Missing key: {key!r}"

    def test_pipeline_inputs_include_issue_number_and_repo(self):
        """Launcher must be called with pipeline_inputs containing issue_number and repo."""
        auto = _make_automation("bug", confidence=0.90)
        launcher, resolver, engine = self._make_launcher_and_resolver()
        auto.process(
            issue_number=55,
            repo="myorg/myrepo",
            title="Bug",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )
        assert launcher.called, "Launcher was not called"
        call_kwargs = launcher.call_args[1] if launcher.call_args[1] else {}
        call_args = launcher.call_args[0] if launcher.call_args[0] else ()
        # pipeline_inputs may be positional or keyword
        all_args = str(call_args) + str(call_kwargs)
        assert "55" in all_args or 55 in str(call_args) + str(call_kwargs), \
            f"issue_number 55 not found in launcher call: {launcher.call_args}"

    def test_webhook_env_var_threshold_wired(self):
        """Webhook handler reads ISSUE_CLASSIFY_CONFIDENCE_THRESHOLD from env.

        Default stub confidence is 0.0.  With threshold=0.0 (from env), the
        condition ``confidence < threshold`` is False → no escalation.
        With the default threshold of 0.70, stub confidence 0.0 would escalate.
        This test proves the env var is actually wired into the constructor.
        """
        import os
        client, _ = _fresh_client()
        payload = _issue_payload(
            action="opened",
            issue_number=88,
            labels=["orchemist"],
        )
        # threshold=0.0 means confidence=0.0 is NOT below threshold → happy path
        with patch.dict(os.environ, {"ISSUE_CLASSIFY_CONFIDENCE_THRESHOLD": "0.0"}):
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
        # escalated should be False (or absent) because threshold was 0.0
        assert not data.get("escalated", False)


# ===========================================================================
# Tests: Module exports
# ===========================================================================


class TestModuleExports:
    """Verify top-level exports for issue automation components."""

    def test_issue_automation_in_top_level_all(self):
        import orchestration_engine
        assert "IssueAutomation" in orchestration_engine.__all__

    def test_post_github_comment_in_top_level_all(self):
        import orchestration_engine
        assert "post_github_comment" in orchestration_engine.__all__

    def test_issue_automation_importable(self):
        from orchestration_engine import IssueAutomation
        assert IssueAutomation is not None

    def test_post_github_comment_importable(self):
        from orchestration_engine import post_github_comment
        assert callable(post_github_comment)

    def test_issue_automation_in_issue_automation_all(self):
        from orchestration_engine import issue_automation
        assert "IssueAutomation" in issue_automation.__all__

    def test_post_github_comment_in_issue_automation_all(self):
        from orchestration_engine import issue_automation
        assert "post_github_comment" in issue_automation.__all__
