"""Tests for Issue #5.1.3 — Issue Webhook Handler.

Covers:
- post_github_comment(): success path (returncode 0)
- post_github_comment(): failure path (non-zero returncode)
- post_github_comment(): subprocess.TimeoutExpired → returns None
- post_github_comment(): FileNotFoundError (gh not found) → returns None
- post_github_comment(): empty stdout → returns None
- IssueAutomation.__init__: stores injected dependencies
- IssueAutomation.process(): classify → select → extract (no launcher)
- IssueAutomation.process(): with launcher, updates status to "launched"
- IssueAutomation.process(): launcher failure → run_id is None, no exception raised
- IssueAutomation.process(): template resolver failure → fallback config schema
- IssueAutomation._build_comment(): with run_id
- IssueAutomation._build_comment(): without run_id
- IssueAutomation._build_comment(): with reasoning
- Database.get_active_issue_run(): returns None when no rows exist
- Database.get_active_issue_run(): returns None when run_id IS NULL (no run launched)
- Database.get_active_issue_run(): returns None when linked run is terminal
- Database.get_active_issue_run(): returns row when linked run is non-terminal (pending)
- Database.get_active_issue_run(): returns row when linked run is non-terminal (running)
- Database.get_active_issue_run(): ignores rows from other repos
- Database.get_active_issue_run(): ignores rows from other issue numbers
- API POST /api/v1/github/issues: wrong X-GitHub-Event header → 200 ignored
- API POST /api/v1/github/issues: correct header, unsupported action → 200 ignored
- API POST /api/v1/github/issues: action=labeled, wrong label → 200 ignored
- API POST /api/v1/github/issues: action=opened, no trigger label → 200 ignored
- API POST /api/v1/github/issues: action=opened, trigger label present → 202
- API POST /api/v1/github/issues: action=labeled, correct trigger label → 202
- API POST /api/v1/github/issues: deduplication — active run exists → 200 skipped
- API POST /api/v1/github/issues: invalid JSON body → 400
- API POST /api/v1/github/issues: missing issue.number → 400
- API POST /api/v1/github/issues: missing repository.full_name → 400
- Module exports (IssueAutomation, post_github_comment in __all__)

All tests are independent — no shared mutable state, no real LLM calls,
no real subprocess calls, no real HTTP calls.
"""

from __future__ import annotations

import json
import subprocess
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
        extractor=InputExtractor(),  # stub mode
    )


def _insert_run(db: Database, run_id: str, status: str) -> None:
    """Insert a minimal pipeline_run row with the given status."""
    db.insert_pipeline_run({
        "run_id": run_id,
        "template_path": "/tmp/fake.yaml",
        "template_id": "coding-pipeline",
        "input_json": "{}",
        "mode": "standalone",
        "output_dir": "/tmp/out",
        "gateway_url": None,
        "skip_scoring": 0,
        "status": status,
    })


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


# ---------------------------------------------------------------------------
# FastAPI test client helper
# ---------------------------------------------------------------------------


def _make_test_client(db_path: str) -> "TestClient":  # noqa: F821
    """Create a FastAPI TestClient wired to *db_path*."""
    from fastapi.testclient import TestClient
    from orchestration_engine.web.api import create_api_app

    app = create_api_app(db_path=db_path)
    return TestClient(app, raise_server_exceptions=True)


# ===========================================================================
# Tests: post_github_comment
# ===========================================================================


class TestPostGithubComment:
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

    def test_empty_stdout_returns_none(self):
        """Returncode 0 but empty stdout → returns None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   \n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            url = post_github_comment("owner/repo", 1, "Hello!")

        assert url is None

    def test_subprocess_called_with_correct_args(self):
        """Verify the gh api command arguments."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/x/y/issues/5#issuecomment-99\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            post_github_comment("x/y", 5, "Test body")

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "gh"
        assert cmd[1] == "api"
        assert "repos/x/y/issues/5/comments" in cmd
        assert "--method" in cmd
        assert "POST" in cmd
        # The body field should be included
        field_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--field"]
        assert any("body=Test body" in f for f in field_args)


# ===========================================================================
# Tests: IssueAutomation — constructor and basic flow
# ===========================================================================


class TestIssueAutomationInit:
    def test_stores_classifier(self):
        clf = _make_classifier()
        auto = IssueAutomation(
            classifier=clf,
            selector=TemplateSelector(),
            extractor=InputExtractor(),
        )
        assert auto.classifier is clf

    def test_stores_selector(self):
        sel = TemplateSelector()
        auto = IssueAutomation(
            classifier=_make_classifier(),
            selector=sel,
            extractor=InputExtractor(),
        )
        assert auto.selector is sel

    def test_stores_extractor(self):
        ext = InputExtractor()
        auto = IssueAutomation(
            classifier=_make_classifier(),
            selector=TemplateSelector(),
            extractor=ext,
        )
        assert auto.extractor is ext


class TestIssueAutomationProcess:
    def test_returns_dict_with_required_keys(self):
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        required = {
            "issue_number", "repo", "classification_type",
            "confidence", "template", "run_id", "comment_body",
        }
        assert required.issubset(result.keys())

    def test_issue_number_and_repo_in_result(self):
        auto = _make_automation()
        result = auto.process(issue_number=42, repo="acme/service", title="Crash")
        assert result["issue_number"] == 42
        assert result["repo"] == "acme/service"

    def test_no_launcher_run_id_is_none(self):
        """Without a launcher, run_id should be None."""
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Bug")
        assert result["run_id"] is None

    def test_classification_type_in_result(self):
        auto = _make_automation("feature")
        result = auto.process(issue_number=1, repo="o/r", title="New feature")
        assert result["classification_type"] == "feature"

    def test_template_in_result(self):
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Bug fix")
        # "bug" maps to "coding-pipeline" in DEFAULT_TEMPLATE_MAPPING
        assert result["template"] == "coding-pipeline"

    def test_comment_body_not_empty(self):
        auto = _make_automation()
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        assert result["comment_body"]
        assert len(result["comment_body"]) > 0

    def test_comment_body_contains_classification(self):
        auto = _make_automation("bug")
        result = auto.process(issue_number=1, repo="o/r", title="Test")
        assert "bug" in result["comment_body"]

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

    def test_with_launcher_status_updated_to_launched(self):
        """Status of the classification row should be 'launched' after a successful launch."""
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

    def test_launcher_failure_does_not_raise(self):
        """A launcher that raises should not propagate — run_id is None."""
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

    def test_template_resolver_failure_falls_back_to_empty_schema(self):
        """When template resolution raises, extractor uses empty schema (no crash)."""
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
        # Should still return a dict with all required keys
        assert "classification_type" in result
        assert result["run_id"] is None

    def test_pipeline_inputs_contain_issue_number_and_repo(self):
        """The launcher should be called with inputs that include issue_number and repo."""
        captured_inputs: Dict[str, Any] = {}

        def fake_launcher(**kwargs: Any) -> Dict[str, Any]:
            captured_inputs.update(kwargs.get("input_data", {}))
            return {"run_id": "run123"}

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
        assert captured_inputs.get("issue_number") == 55
        assert captured_inputs.get("repo") == "test/repo"

    def test_db_classification_persisted(self):
        """When db is provided, the classification row should be persisted."""
        db = _make_db()
        auto = _make_automation("bug")
        auto.process(issue_number=33, repo="o/r", title="Test bug", db=db)

        row = db.get_issue_classification(33, "o/r")
        assert row is not None
        assert row["classification_type"] == "bug"


# ===========================================================================
# Tests: IssueAutomation._build_comment
# ===========================================================================


class TestBuildComment:
    def _make_classification(
        self,
        cls_type: str = "feature",
        confidence: float = 0.9,
        reasoning: str = "",
    ) -> IssueClassification:
        return IssueClassification(
            issue_number=1,
            repo="o/r",
            classification_type=cls_type,
            confidence=confidence,
            template_id="coding-pipeline-v1",
            reasoning=reasoning,
        )

    def test_comment_contains_orchemist(self):
        auto = _make_automation()
        cls = self._make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", "run123")
        assert "Orchemist" in comment

    def test_comment_contains_classification_type(self):
        auto = _make_automation()
        cls = self._make_classification("bug")
        comment = auto._build_comment(1, cls, "coding-pipeline", "run123")
        assert "bug" in comment

    def test_comment_contains_run_id(self):
        auto = _make_automation()
        cls = self._make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", "abc12345")
        assert "abc12345" in comment

    def test_comment_no_run_id_shows_not_launched(self):
        auto = _make_automation()
        cls = self._make_classification()
        comment = auto._build_comment(1, cls, "coding-pipeline", None)
        assert "abc12345" not in comment
        # Should indicate run was not launched
        assert "not launched" in comment.lower() or "unavailable" in comment.lower()

    def test_comment_contains_reasoning_when_present(self):
        auto = _make_automation()
        cls = self._make_classification(reasoning="Clear crash report.")
        comment = auto._build_comment(1, cls, "coding-pipeline", "r1")
        assert "Clear crash report." in comment

    def test_comment_contains_template(self):
        auto = _make_automation()
        cls = self._make_classification()
        comment = auto._build_comment(1, cls, "content-pipeline", "r1")
        assert "content-pipeline" in comment


# ===========================================================================
# Tests: Database.get_active_issue_run
# ===========================================================================


class TestGetActiveIssueRun:
    def test_returns_none_when_no_rows(self):
        db = _make_db()
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_run_id_is_null(self):
        """Rows with run_id IS NULL (classified but not launched) should not block."""
        db = _make_db()
        _insert_issue_map(db, 1, "o/r", run_id=None, status="classified")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_run_is_failed(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "failed")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_run_is_success(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "success")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_run_is_cancelled(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "cancelled")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_row_when_run_is_pending(self):
        """A 'pending' pipeline run is not terminal → should return the row."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "pending")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        result = db.get_active_issue_run(1, "o/r")
        assert result is not None
        assert result["run_id"] == run_id

    def test_returns_row_when_run_is_running(self):
        """A 'running' pipeline run is not terminal → should return the row."""
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
        result = db.get_active_issue_run(1, "o/r")
        assert result is not None
        assert result["run_id"] == run_id

    def test_ignores_different_repo(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "other/repo", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_ignores_different_issue_number(self):
        db = _make_db()
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 99, "o/r", run_id=run_id, status="launched")
        assert db.get_active_issue_run(1, "o/r") is None

    def test_returns_none_when_all_terminal_statuses(self):
        """All terminal statuses should result in no active run."""
        for status in TERMINAL_STATUSES:
            db = _make_db()
            run_id = str(uuid.uuid4())[:8]
            _insert_run(db, run_id, status)
            _insert_issue_map(db, 1, "o/r", run_id=run_id, status="launched")
            assert db.get_active_issue_run(1, "o/r") is None, (
                f"Expected None for terminal status {status!r}"
            )


# ===========================================================================
# Tests: POST /api/v1/github/issues — webhook route
# ===========================================================================


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
        "repository": {
            "full_name": repo,
        },
    }
    if applied_label is not None:
        payload["label"] = {"name": applied_label}
    return payload


class TestGithubIssuesWebhook:
    """Tests for POST /api/v1/github/issues."""

    def _client_for_db(self, db: Database):
        """Return a TestClient sharing the given Database instance's file."""
        return _make_test_client(str(db.db_path))

    def _fresh_client(self) -> tuple:
        """Return (client, db_path_str)."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        client = _make_test_client(db_path)
        return client, db_path

    def test_wrong_event_header_returns_200_ignored(self):
        client, _ = self._fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "push"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "not_issues_event"

    def test_missing_event_header_returns_200_ignored(self):
        client, _ = self._fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "opened"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_unsupported_action_returns_200_ignored(self):
        client, _ = self._fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "closed"},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_labeled_action_wrong_label_returns_200_ignored(self):
        client, _ = self._fresh_client()
        payload = _issue_payload(action="labeled", applied_label="bug")
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "label_not_trigger"

    def test_opened_action_no_trigger_label_returns_200_ignored(self):
        client, _ = self._fresh_client()
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
        client, _ = self._fresh_client()
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
        client, _ = self._fresh_client()
        payload = {
            "action": "opened",
            "issue": {"title": "Test", "body": "", "labels": [{"name": "orchemist"}]},
            # missing "number"
            "repository": {"full_name": "owner/repo"},
        }
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 400

    def test_missing_repo_returns_400(self):
        client, _ = self._fresh_client()
        payload = {
            "action": "opened",
            "issue": {
                "number": 1,
                "title": "Test",
                "body": "",
                "labels": [{"name": "orchemist"}],
            },
            # missing "repository"
        }
        resp = client.post(
            "/api/v1/github/issues",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 400

    def test_opened_with_trigger_label_returns_202(self):
        """An 'opened' issue with the trigger label should return 202."""
        client, db_path = self._fresh_client()
        payload = _issue_payload(
            action="opened",
            labels=["orchemist"],
        )

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

    def test_labeled_with_trigger_label_returns_202(self):
        """A 'labeled' event with the trigger label should return 202."""
        client, db_path = self._fresh_client()
        payload = _issue_payload(
            action="labeled",
            labels=["orchemist"],
            applied_label="orchemist",
        )

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

    def test_dedup_skips_when_active_run_exists(self):
        """When an active run already exists for the issue, return 200 skipped."""
        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        db = Database(db_path)

        # Insert a non-terminal run for this issue
        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "running")
        _insert_issue_map(db, 1, "owner/repo", run_id=run_id, status="launched")

        client = _make_test_client(db_path)
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

    def test_dedup_allows_when_previous_run_is_terminal(self):
        """When the previous run is terminal (failed), should proceed and return 202."""
        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        db = Database(db_path)

        run_id = str(uuid.uuid4())[:8]
        _insert_run(db, run_id, "failed")
        _insert_issue_map(db, 1, "owner/repo", run_id=run_id, status="launched")

        client = _make_test_client(db_path)
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

        # Should proceed (not be deduplicated)
        assert resp.status_code == 202

    def test_response_contains_comment_url_key(self):
        """202 response should always include the 'comment_url' key."""
        client, _ = self._fresh_client()
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
        """'deleted' action should return 200 ignored."""
        client, _ = self._fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={"action": "deleted"},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200

    def test_empty_body_with_issues_event_returns_ignored_or_400(self):
        """Empty body with correct event header — action is missing → ignored."""
        client, _ = self._fresh_client()
        resp = client.post(
            "/api/v1/github/issues",
            json={},
            headers={"X-GitHub-Event": "issues"},
        )
        # action is empty string → not in (opened, labeled) → ignored
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


# ===========================================================================
# Tests: Module exports
# ===========================================================================


class TestModuleExports:
    def test_issue_automation_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "IssueAutomation")

    def test_post_github_comment_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "post_github_comment")

    def test_issue_automation_in_all(self):
        import orchestration_engine
        assert "IssueAutomation" in orchestration_engine.__all__

    def test_post_github_comment_in_all(self):
        import orchestration_engine
        assert "post_github_comment" in orchestration_engine.__all__

    def test_direct_import_issue_automation(self):
        from orchestration_engine import IssueAutomation
        assert IssueAutomation is not None

    def test_direct_import_post_github_comment(self):
        from orchestration_engine import post_github_comment
        assert callable(post_github_comment)

    def test_issue_automation_in_issue_automation_all(self):
        from orchestration_engine import issue_automation
        assert "IssueAutomation" in issue_automation.__all__

    def test_post_github_comment_in_issue_automation_all(self):
        from orchestration_engine import issue_automation
        assert "post_github_comment" in issue_automation.__all__
