"""Tests for orchestration_engine.github_fetcher (Issue #507).

Covers:
- GitHubIssueData dataclass construction and methods
- GitHubIssueFetcher._parse_response
- GitHubIssueFetcher.fetch with various subprocess outcomes
- fetch_github_issue convenience wrapper
- Merge strategy via GitHubIssueData.merge_into
- Re-exports from issue_automation
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.github_fetcher import (
    GitHubIssueData,
    GitHubIssueFetcher,
    fetch_github_issue,
    _CANONICAL_FIELDS,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_API_RESPONSE = {
    "number": 42,
    "title": "Fix null pointer in runner",
    "body": "When the list is empty the runner crashes.",
    "labels": [{"name": "bug"}, {"name": "urgent"}],
    "assignees": [{"login": "alice"}, {"login": "bob"}],
    "milestone": {"title": "v2.0"},
    "state": "open",
    "html_url": "https://github.com/owner/repo/issues/42",
}


def _make_completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# GitHubIssueData tests
# ---------------------------------------------------------------------------


class TestGitHubIssueData:
    def test_basic_construction(self):
        data = GitHubIssueData(
            issue_number=1,
            title="Test",
            body="Body text",
            labels=["bug"],
            assignees=["alice"],
            milestone="v1",
            state="open",
            html_url="https://github.com/owner/repo/issues/1",
            repo="owner/repo",
        )
        assert data.issue_number == 1
        assert data.title == "Test"
        assert data.labels == ["bug"]
        assert data.milestone == "v1"

    def test_defaults(self):
        data = GitHubIssueData(issue_number=1, title="T", body="B")
        assert data.labels == []
        assert data.assignees == []
        assert data.milestone is None
        assert data.state == "open"
        assert data.html_url == ""
        assert data.repo == ""

    def test_to_input_dict_all_fields(self):
        data = GitHubIssueData(
            issue_number=42,
            title="Fix crash",
            body="Details here",
            labels=["bug"],
            assignees=["alice"],
            milestone="v2",
            state="open",
            html_url="https://github.com/owner/repo/issues/42",
            repo="owner/repo",
        )
        d = data.to_input_dict()
        assert d["issue_number"] == 42
        assert d["title"] == "Fix crash"
        assert d["body"] == "Details here"
        assert d["labels"] == ["bug"]
        assert d["assignees"] == ["alice"]
        assert d["milestone"] == "v2"
        assert d["state"] == "open"
        assert d["html_url"] == "https://github.com/owner/repo/issues/42"
        assert d["repo"] == "owner/repo"

    def test_to_input_dict_canonical_only(self):
        data = GitHubIssueData(issue_number=1, title="T", body="B", labels=["bug"])
        d = data.to_input_dict(canonical_only=True)
        assert set(d.keys()) == _CANONICAL_FIELDS
        assert "state" not in d
        assert "html_url" not in d

    def test_merge_into_fills_missing_keys(self):
        data = GitHubIssueData(
            issue_number=99,
            title="Auto title",
            body="Auto body",
            labels=["feature"],
            repo="owner/repo",
        )
        initial = {"some_custom_key": "custom_value"}
        merged = data.merge_into(initial)
        assert merged["some_custom_key"] == "custom_value"
        assert merged["issue_number"] == 99
        assert merged["title"] == "Auto title"

    def test_merge_into_canonical_fields_always_from_github(self):
        data = GitHubIssueData(
            issue_number=42,
            title="GitHub Title",
            body="GitHub Body",
            labels=["bug"],
            repo="owner/repo",
        )
        initial = {
            "title": "User Provided Title",
            "body": "User body",
            "labels": ["wrong"],
            "custom": "preserved",
        }
        merged = data.merge_into(initial)
        # Canonical fields from GitHub win
        assert merged["title"] == "GitHub Title"
        assert merged["body"] == "GitHub Body"
        assert merged["labels"] == ["bug"]
        assert merged["issue_number"] == 42
        # Non-canonical custom key preserved from initial
        assert merged["custom"] == "preserved"

    def test_merge_into_non_canonical_caller_wins(self):
        """Non-canonical fields: caller's initial_input takes precedence."""
        data = GitHubIssueData(
            issue_number=1, title="T", body="B",
            state="open", html_url="https://github.com/x", repo="owner/repo",
        )
        initial = {"state": "caller_override", "html_url": "https://custom.url"}
        merged = data.merge_into(initial)
        assert merged["state"] == "caller_override"
        assert merged["html_url"] == "https://custom.url"


# ---------------------------------------------------------------------------
# GitHubIssueFetcher._parse_response tests
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_full_response(self):
        data = GitHubIssueFetcher._parse_response(SAMPLE_API_RESPONSE, repo="owner/repo")
        assert data.issue_number == 42
        assert data.title == "Fix null pointer in runner"
        assert data.labels == ["bug", "urgent"]
        assert data.assignees == ["alice", "bob"]
        assert data.milestone == "v2.0"
        assert data.state == "open"
        assert data.repo == "owner/repo"

    def test_empty_labels_and_assignees(self):
        resp = {**SAMPLE_API_RESPONSE, "labels": [], "assignees": [], "milestone": None}
        data = GitHubIssueFetcher._parse_response(resp, repo="owner/repo")
        assert data.labels == []
        assert data.assignees == []
        assert data.milestone is None

    def test_null_body_becomes_empty_string(self):
        resp = {**SAMPLE_API_RESPONSE, "body": None}
        data = GitHubIssueFetcher._parse_response(resp, repo="owner/repo")
        assert data.body == ""

    def test_missing_milestone_title(self):
        resp = {**SAMPLE_API_RESPONSE, "milestone": {}}
        data = GitHubIssueFetcher._parse_response(resp, repo="owner/repo")
        assert data.milestone is None


# ---------------------------------------------------------------------------
# GitHubIssueFetcher.fetch tests
# ---------------------------------------------------------------------------


class TestGitHubIssueFetcherFetch:
    def test_successful_fetch(self):
        fetcher = GitHubIssueFetcher()
        stdout = json.dumps(SAMPLE_API_RESPONSE)
        with patch("subprocess.run", return_value=_make_completed_process(stdout=stdout)):
            result = fetcher.fetch("owner/repo", 42)
        assert result is not None
        assert result.issue_number == 42
        assert result.title == "Fix null pointer in runner"

    def test_gh_not_found_returns_none(self):
        fetcher = GitHubIssueFetcher()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = fetcher.fetch("owner/repo", 42)
        assert result is None

    def test_timeout_returns_none(self):
        fetcher = GitHubIssueFetcher()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15)):
            result = fetcher.fetch("owner/repo", 42)
        assert result is None

    def test_nonzero_returncode_returns_none(self):
        fetcher = GitHubIssueFetcher()
        with patch("subprocess.run", return_value=_make_completed_process(returncode=1, stderr="Not found")):
            result = fetcher.fetch("owner/repo", 42)
        assert result is None

    def test_invalid_json_returns_none(self):
        fetcher = GitHubIssueFetcher()
        with patch("subprocess.run", return_value=_make_completed_process(stdout="not-json")):
            result = fetcher.fetch("owner/repo", 42)
        assert result is None

    def test_os_error_returns_none(self):
        fetcher = GitHubIssueFetcher()
        with patch("subprocess.run", side_effect=OSError("permission denied")):
            result = fetcher.fetch("owner/repo", 42)
        assert result is None

    def test_calls_correct_gh_command(self):
        fetcher = GitHubIssueFetcher()
        stdout = json.dumps(SAMPLE_API_RESPONSE)
        with patch("subprocess.run", return_value=_make_completed_process(stdout=stdout)) as mock_run:
            fetcher.fetch("owner/repo", 42)
        call_args = mock_run.call_args[0][0]
        assert call_args == ["gh", "api", "repos/owner/repo/issues/42"]


# ---------------------------------------------------------------------------
# fetch_github_issue convenience wrapper tests
# ---------------------------------------------------------------------------


class TestFetchGitHubIssue:
    def test_returns_issue_data_on_success(self):
        stdout = json.dumps(SAMPLE_API_RESPONSE)
        with patch("subprocess.run", return_value=_make_completed_process(stdout=stdout)):
            result = fetch_github_issue(repo="owner/repo", issue_number=42)
        assert result is not None
        assert result.issue_number == 42

    def test_returns_none_on_failure(self):
        with patch("subprocess.run", return_value=_make_completed_process(returncode=1)):
            result = fetch_github_issue(repo="owner/repo", issue_number=999)
        assert result is None


# ---------------------------------------------------------------------------
# Re-export tests (issue_automation)
# ---------------------------------------------------------------------------


def test_reexports_from_issue_automation():
    from orchestration_engine.issue_automation import (
        GitHubIssueData as GID,
        GitHubIssueFetcher as GIF,
        fetch_github_issue as fgi,
    )
    assert GID is GitHubIssueData
    assert GIF is GitHubIssueFetcher
    assert fgi is fetch_github_issue
