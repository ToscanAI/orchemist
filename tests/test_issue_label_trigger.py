"""Tests for Issue #511 — Issue Event Handler: Label Triggers Pipeline.

Covers:
- slugify_branch(): normal titles
- slugify_branch(): unicode / accented characters
- slugify_branch(): empty / whitespace-only input → "issue"
- slugify_branch(): special chars only → "issue"
- slugify_branch(): long titles → truncated to max_length
- slugify_branch(): does not start or end with hyphens
- generate_pipeline_input(): branch_name format feat/{num}-{slug}
- generate_pipeline_input(): all required keys present
- generate_pipeline_input(): optional repo_path included when provided
- generate_pipeline_input(): optional repo_path omitted when None
- remove_github_label(): success path (returncode 0) → True
- remove_github_label(): failure path (non-zero returncode) → False
- remove_github_label(): TimeoutExpired → False
- remove_github_label(): FileNotFoundError (gh not found) → False
- remove_github_label(): label with spaces is URL-encoded in endpoint
- API POST /api/v1/github/issues/pipeline-ready: wrong X-GitHub-Event → 200
- API POST /api/v1/github/issues/pipeline-ready: action != labeled → 200
- API POST /api/v1/github/issues/pipeline-ready: label != pipeline-ready → 200
- API POST /api/v1/github/issues/pipeline-ready: missing issue.number → 400
- API POST /api/v1/github/issues/pipeline-ready: missing repository.full_name → 400
- API POST /api/v1/github/issues/pipeline-ready: active run exists → 200 skipped
- API POST /api/v1/github/issues/pipeline-ready: success path → 202 with run_id + branch_name
- API POST /api/v1/github/issues/pipeline-ready: pipeline-ready label removed after launch
- API POST /api/v1/github/issues/pipeline-ready: comment posted after launch
- API POST /api/v1/github/issues/pipeline-ready: invalid JSON → 400
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.issue_automation import (
    generate_pipeline_input,
    remove_github_label,
    slugify_branch,
)


# ---------------------------------------------------------------------------
# slugify_branch tests
# ---------------------------------------------------------------------------

class TestSlugifyBranch:
    def test_normal_title(self):
        result = slugify_branch("Fix null pointer in pipeline runner")
        assert result == "fix-null-pointer-in-pipeline-runner"

    def test_uppercase(self):
        result = slugify_branch("Add New Feature")
        assert result == "add-new-feature"

    def test_unicode_accents(self):
        result = slugify_branch("Add résumé parser")
        assert result == "add-resume-parser"

    def test_unicode_emoji(self):
        # emoji stripped → remaining text slugified
        result = slugify_branch("Fix bug 🚀 fast")
        assert result == "fix-bug-fast"

    def test_empty_string(self):
        assert slugify_branch("") == "issue"

    def test_whitespace_only(self):
        assert slugify_branch("   ") == "issue"

    def test_special_chars_only(self):
        # All non-alphanumeric → "issue"
        assert slugify_branch("!!! ---") == "issue"

    def test_long_title_truncated(self):
        long = "a" * 100
        result = slugify_branch(long, max_length=40)
        assert len(result) <= 40

    def test_no_leading_trailing_hyphens(self):
        result = slugify_branch("--hello world--")
        assert not result.startswith("-")
        assert not result.endswith("-")

    def test_consecutive_hyphens_collapsed(self):
        result = slugify_branch("hello   world")
        assert "--" not in result

    def test_custom_max_length(self):
        result = slugify_branch("a b c d e f g h i j k l m n o p", max_length=10)
        assert len(result) <= 10

    def test_numbers_preserved(self):
        result = slugify_branch("Issue 511 fix")
        assert "511" in result


# ---------------------------------------------------------------------------
# generate_pipeline_input tests
# ---------------------------------------------------------------------------

class TestGeneratePipelineInput:
    def test_branch_name_format(self):
        inp = generate_pipeline_input(42, "Fix NPE in runner", "body", "org/repo")
        assert inp["branch_name"].startswith("feat/42-")

    def test_branch_name_slug(self):
        inp = generate_pipeline_input(42, "Fix NPE in runner", "body", "org/repo")
        assert inp["branch_name"] == "feat/42-fix-npe-in-runner"

    def test_required_keys_present(self):
        inp = generate_pipeline_input(7, "Test", "body", "owner/repo")
        assert "issue_number" in inp
        assert "repo" in inp
        assert "title" in inp
        assert "body" in inp
        assert "branch_name" in inp

    def test_values_correct(self):
        inp = generate_pipeline_input(7, "Test title", "Test body", "owner/repo")
        assert inp["issue_number"] == 7
        assert inp["repo"] == "owner/repo"
        assert inp["title"] == "Test title"
        assert inp["body"] == "Test body"

    def test_repo_path_included_when_provided(self):
        inp = generate_pipeline_input(1, "T", "B", "o/r", repo_path="/tmp/repo")
        assert inp["repo_path"] == "/tmp/repo"

    def test_repo_path_omitted_when_none(self):
        inp = generate_pipeline_input(1, "T", "B", "o/r", repo_path=None)
        assert "repo_path" not in inp

    def test_empty_title_fallback(self):
        inp = generate_pipeline_input(99, "", "body", "o/r")
        # empty title → slug "issue" → branch "feat/99-issue"
        assert inp["branch_name"] == "feat/99-issue"


# ---------------------------------------------------------------------------
# remove_github_label tests
# ---------------------------------------------------------------------------

class TestRemoveGithubLabel:
    def test_success_returns_true(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = remove_github_label("owner/repo", 42, "pipeline-ready")
        assert result is True
        mock_run.assert_called_once()

    def test_failure_returns_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Not Found"
        with patch("subprocess.run", return_value=mock_result):
            result = remove_github_label("owner/repo", 42, "pipeline-ready")
        assert result is False

    def test_timeout_returns_false(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            result = remove_github_label("owner/repo", 42, "pipeline-ready")
        assert result is False

    def test_file_not_found_returns_false(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = remove_github_label("owner/repo", 42, "pipeline-ready")
        assert result is False

    def test_label_url_encoded_in_endpoint(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            remove_github_label("owner/repo", 1, "needs: review")
        args = mock_run.call_args[0][0]
        # URL-encoded label should appear in the endpoint argument
        endpoint_arg = args[2]  # "repos/owner/repo/issues/1/labels/needs%3A%20review"
        assert "needs%3A%20review" in endpoint_arg or "needs%3A+review" in endpoint_arg or "needs%3A%20review" in " ".join(args)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def _make_payload(
    action: str = "labeled",
    label_name: str = "pipeline-ready",
    issue_number: int = 511,
    repo_full_name: str = "owner/repo",
    issue_title: str = "Test issue",
    issue_body: str = "Test body",
) -> Dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label_name},
        "issue": {
            "number": issue_number,
            "title": issue_title,
            "body": issue_body,
            "labels": [{"name": label_name}],
        },
        "repository": {"full_name": repo_full_name},
    }


@pytest.fixture()
def api_client():
    """Return a TestClient for the REST API backed by a temp DB."""
    from fastapi.testclient import TestClient
    from orchestration_engine.web.api import create_api_app

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        app = create_api_app(db_path=db_path)
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client


def _post_pipeline_ready(client, payload: Dict, event: str = "issues"):
    return client.post(
        "/api/v1/github/issues/pipeline-ready",
        json=payload,
        headers={"X-GitHub-Event": event},
    )


class TestPipelineReadyEndpoint:
    def test_wrong_event_header_ignored(self, api_client):
        resp = _post_pipeline_ready(api_client, _make_payload(), event="push")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_action_opened_ignored(self, api_client):
        resp = _post_pipeline_ready(api_client, _make_payload(action="opened"))
        assert resp.status_code == 200
        assert resp.json()["reason"] == "action_opened_not_relevant"

    def test_wrong_label_ignored(self, api_client):
        resp = _post_pipeline_ready(api_client, _make_payload(label_name="orchemist"))
        assert resp.status_code == 200
        assert resp.json()["reason"] == "label_not_pipeline_ready"

    def test_missing_issue_number_400(self, api_client):
        payload = _make_payload()
        del payload["issue"]["number"]
        resp = _post_pipeline_ready(api_client, payload)
        assert resp.status_code == 400

    def test_missing_repo_400(self, api_client):
        payload = _make_payload()
        del payload["repository"]["full_name"]
        resp = _post_pipeline_ready(api_client, payload)
        assert resp.status_code == 400

    def test_invalid_json_400(self, api_client):
        from fastapi.testclient import TestClient
        resp = api_client.post(
            "/api/v1/github/issues/pipeline-ready",
            content=b"not-json",
            headers={"X-GitHub-Event": "issues", "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_active_run_dedup_skipped(self, api_client):
        """When get_active_issue_run returns a row, the endpoint returns 200 skipped."""
        active_row = {"run_id": "abc123"}
        with patch(
            "orchestration_engine.db.Database.get_active_issue_run",
            return_value=active_row,
        ):
            resp = _post_pipeline_ready(api_client, _make_payload())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["run_id"] == "abc123"

    def _mock_template(self):
        """Return a mock template and a fake template path (as a real temp file)."""
        import tempfile as _tempfile
        tmp = _tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        tmp.write(b"id: coding-pipeline-v1\nname: Coding Pipeline\nphases: []\n")
        tmp.close()
        mock_tpl = MagicMock()
        mock_tpl.id = "coding-pipeline-v1"
        return Path(tmp.name), mock_tpl

    def _launch_patches(self, template_path: Path, mock_tpl: Any, fake_run: Dict):
        """Return a context manager stack that mocks template resolution + daemon launch."""
        import contextlib

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        return (
            patch("orchestration_engine.db.Database.get_active_issue_run", return_value=None),
            patch("orchestration_engine.templates.TemplateEngine.resolve_template", return_value=template_path),
            patch("orchestration_engine.templates.TemplateEngine.load_template", return_value=mock_tpl),
            patch("orchestration_engine.templates.TemplateEngine.validate_template", return_value=[]),
            patch("orchestration_engine.db.Database.insert_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.update_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.get_pipeline_run", return_value=fake_run),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("orchestration_engine.issue_automation.remove_github_label", return_value=True),
            patch("orchestration_engine.issue_automation.post_github_comment", return_value=None),
        )

    def test_success_202_with_run_id_and_branch(self, api_client):
        """Happy path: pipeline launched, 202 returned with run_id and branch_name."""
        template_path, mock_tpl = self._mock_template()
        fake_run = {
            "run_id": "run-xyz", "status": "pending", "template_id": "coding-pipeline-v1",
            "template_path": str(template_path), "mode": "standalone", "current_phase": None,
            "completed_phases": "[]", "pid": 99999, "output_dir": "/tmp/out",
            "error_message": None, "gateway_url": None, "skip_scoring": 0,
            "scoring_status": None, "scoring_score": None, "started_at": None,
            "completed_at": None, "created_at": None,
        }

        patches = self._launch_patches(template_path, mock_tpl, fake_run)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            resp = _post_pipeline_ready(api_client, _make_payload(issue_number=511, issue_title="My Feature"))

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "run_id" in data
        assert "branch_name" in data
        assert data["branch_name"].startswith("feat/511-")

    def test_label_removed_after_launch(self, api_client):
        """remove_github_label should be called after successful pipeline launch."""
        template_path, mock_tpl = self._mock_template()
        fake_run = {
            "run_id": "run-abc", "status": "pending", "template_id": "coding-pipeline-v1",
            "template_path": str(template_path), "mode": "standalone", "current_phase": None,
            "completed_phases": "[]", "pid": 99999, "output_dir": "/tmp/out",
            "error_message": None, "gateway_url": None, "skip_scoring": 0,
            "scoring_status": None, "scoring_score": None, "started_at": None,
            "completed_at": None, "created_at": None,
        }
        remove_mock = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch("orchestration_engine.db.Database.get_active_issue_run", return_value=None),
            patch("orchestration_engine.templates.TemplateEngine.resolve_template", return_value=template_path),
            patch("orchestration_engine.templates.TemplateEngine.load_template", return_value=mock_tpl),
            patch("orchestration_engine.templates.TemplateEngine.validate_template", return_value=[]),
            patch("orchestration_engine.db.Database.insert_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.update_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.get_pipeline_run", return_value=fake_run),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("orchestration_engine.issue_automation.remove_github_label", remove_mock),
            patch("orchestration_engine.issue_automation.post_github_comment", return_value=None),
        ):
            resp = _post_pipeline_ready(
                api_client,
                _make_payload(issue_number=99, repo_full_name="owner/repo"),
            )

        assert resp.status_code == 202
        remove_mock.assert_called_once_with("owner/repo", 99, "pipeline-ready")

    def test_comment_posted_after_launch(self, api_client):
        """post_github_comment should be called after successful pipeline launch."""
        template_path, mock_tpl = self._mock_template()
        fake_run = {
            "run_id": "run-def", "status": "pending", "template_id": "coding-pipeline-v1",
            "template_path": str(template_path), "mode": "standalone", "current_phase": None,
            "completed_phases": "[]", "pid": 99999, "output_dir": "/tmp/out",
            "error_message": None, "gateway_url": None, "skip_scoring": 0,
            "scoring_status": None, "scoring_score": None, "started_at": None,
            "completed_at": None, "created_at": None,
        }
        comment_mock = MagicMock(return_value="https://github.com/owner/repo/issues/7#comment-1")

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch("orchestration_engine.db.Database.get_active_issue_run", return_value=None),
            patch("orchestration_engine.templates.TemplateEngine.resolve_template", return_value=template_path),
            patch("orchestration_engine.templates.TemplateEngine.load_template", return_value=mock_tpl),
            patch("orchestration_engine.templates.TemplateEngine.validate_template", return_value=[]),
            patch("orchestration_engine.db.Database.insert_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.update_pipeline_run", return_value=None),
            patch("orchestration_engine.db.Database.get_pipeline_run", return_value=fake_run),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("orchestration_engine.issue_automation.remove_github_label", return_value=True),
            patch("orchestration_engine.issue_automation.post_github_comment", comment_mock),
        ):
            resp = _post_pipeline_ready(
                api_client,
                _make_payload(issue_number=7, repo_full_name="owner/repo"),
            )

        assert resp.status_code == 202
        comment_mock.assert_called_once()
        call_kwargs = comment_mock.call_args
        # Verify repo was passed correctly
        all_args = str(call_kwargs)
        assert "owner/repo" in all_args
