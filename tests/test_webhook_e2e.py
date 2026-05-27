"""E2E integration tests for the complete GitHub webhook flow.

Tests the full pipeline: GitHub payload → signature verification →
filter matching → input interpolation → pipeline launch → DB state.

Unlike the unit-slice tests (test_webhook_endpoint.py, test_webhooks.py,
test_trigger_matching.py), this file exercises the complete end-to-end
chain with *realistic* GitHub payloads:

  - Real ``issues.opened`` payload with labels flows through the full stack
  - Filter evaluation against real nested payloads (not stub dicts)
  - Both ``$.path`` and ``{{payload.*}}`` input_map variants on real payloads
  - Multiple triggers registered for the same event — only matching ones fire
  - All guards (signature, rate-limit, disabled, filter mismatch) from a
    consumer perspective
  - DB post-conditions verified after each successful launch

Issue: #329.5
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient

from orchestration_engine.db import Database  # noqa: E402
from orchestration_engine.web.api import create_api_app  # noqa: E402

# A stable fixture template — no mocking needed for template resolution.
_TEMPLATE_ID = "coding-pipeline-fixture"

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True, scope="module")
def _add_examples_to_templates_path():
    """Add examples/ to ORCH_TEMPLATES_PATH so the fixture template is resolvable."""
    old = os.environ.get("ORCH_TEMPLATES_PATH", "")
    examples = str(REPO_ROOT / "examples")
    os.environ["ORCH_TEMPLATES_PATH"] = f"{examples}:{old}" if old else examples
    yield
    try:
        if old:
            os.environ["ORCH_TEMPLATES_PATH"] = old
        else:
            os.environ.pop("ORCH_TEMPLATES_PATH", None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Realistic GitHub payload fixtures (module-level dicts for reuse/parametrize)
# ---------------------------------------------------------------------------

# ── FIXTURE: issues.opened with "bug" label ──────────────────────────────────
GITHUB_ISSUE_OPENED_BUG = {
    "action": "opened",
    "issue": {
        "number": 42,
        "title": "Something is broken",
        "body": "Describe the bug here.",
        "user": {"login": "octocat"},
        "labels": [{"name": "bug"}, {"name": "help wanted"}],
    },
    "label": {"name": "bug"},          # GitHub sends both label + labels
    "labels": [{"name": "bug"}, {"name": "help wanted"}],
    "repository": {
        "full_name": "org/my-repo",
        "name": "my-repo",
        "owner": {"login": "org"},
        "html_url": "https://github.com/org/my-repo",
        "default_branch": "main",
    },
    "sender": {"login": "octocat"},
}

# ── FIXTURE: push to main branch ─────────────────────────────────────────────
GITHUB_PUSH_TO_MAIN = {
    "ref": "refs/heads/main",
    "before": "0000000000000000000000000000000000000000",
    "after": "abc123def456abc123def456abc123def456abc1",
    "repository": {
        "full_name": "org/my-repo",
        "name": "my-repo",
        "owner": {"login": "org"},
        "html_url": "https://github.com/org/my-repo",
        "default_branch": "main",
    },
    "commits": [
        {
            "id": "abc123def456abc123def456abc123def456abc1",
            "message": "Fix critical bug",
            "author": {"name": "octocat", "email": "octocat@github.com"},
            "added": ["README.md"],
            "modified": ["src/main.py"],
            "removed": [],
        }
    ],
    "pusher": {"name": "octocat", "email": "octocat@github.com"},
    "sender": {"login": "octocat"},
}

# ── FIXTURE: pull_request.opened ─────────────────────────────────────────────
GITHUB_PR_OPENED = {
    "action": "opened",
    "number": 7,
    "pull_request": {
        "number": 7,
        "title": "Add new feature",
        "body": "This PR adds a new feature.",
        "user": {"login": "octocat"},
        "head": {
            "ref": "feature/new-thing",
            "sha": "def456abc123def456abc123def456abc123def4",
        },
        "base": {
            "ref": "main",
            "sha": "abc123def456abc123def456abc123def456abc1",
        },
        "labels": [],
        "draft": False,
    },
    "repository": {
        "full_name": "org/my-repo",
        "name": "my-repo",
        "owner": {"login": "org"},
        "html_url": "https://github.com/org/my-repo",
        "default_branch": "main",
    },
    "sender": {"login": "octocat"},
}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_app_and_db(tmp_path: Path):
    """Return (TestClient, db_path) backed by an isolated temp-file DB."""
    db_file = str(tmp_path / "e2e-test.db")
    app = create_api_app(db_path=db_file)
    client = TestClient(app, raise_server_exceptions=False)
    return client, db_file


def _make_sig(secret: str, body: bytes) -> str:
    """Compute a valid GitHub-style HMAC-SHA256 signature header value."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _fake_popen(*args, **kwargs) -> MagicMock:
    """Return a mock Popen whose ``pid`` attribute is always 99999."""
    mock = MagicMock()
    mock.pid = 99999
    return mock


def _insert_trigger(
    db_path: str,
    trigger_id: str,
    template_id: str = _TEMPLATE_ID,
    mode: str = "async",
    secret: Optional[str] = None,
    rate_limit: int = 0,
    input_map: Optional[Dict[str, Any]] = None,
    filters: Optional[list] = None,
    enabled: bool = True,
) -> None:
    """Helper to insert a trigger row directly into the test database."""
    db = Database(Path(db_path))
    db.create_trigger(
        {
            "id": trigger_id,
            "template_id": template_id,
            "mode": mode,
            "secret": secret,
            "rate_limit": rate_limit,
            "input_map": input_map if input_map is not None else {},
            "filters": filters if filters is not None else [],
            "enabled": enabled,
        }
    )


def _post_github(client, trigger_id: str, payload: dict, secret: Optional[str] = None,
                 extra_headers: Optional[dict] = None) -> Any:
    """POST a GitHub webhook payload, optionally signing it."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "push"}
    if extra_headers:
        headers.update(extra_headers)
    if secret:
        headers["X-Hub-Signature-256"] = _make_sig(secret, body)
    return client.post(
        f"/api/v1/webhooks/{trigger_id}",
        content=body,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Group A: Happy-path end-to-end with realistic GitHub payloads
# ---------------------------------------------------------------------------

class TestE2EIssueOpenedHappyPath:
    """Full end-to-end flow with a real issues.opened GitHub payload."""

    def test_issue_opened_bug_label_launches_pipeline(self, tmp_path):
        """issues.opened with bug label → full stack → 201 with run_id."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0issues00", mode="async")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0issues00", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 201
        assert "run_id" in res.json()

    def test_issue_opened_records_invocation_in_db(self, tmp_path):
        """Successful launch must record an invocation in webhook_invocations."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0issues01")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            _post_github(client, "trig-e2e0issues01", GITHUB_ISSUE_OPENED_BUG)
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)
        assert db.count_webhook_invocations_since("trig-e2e0issues01", since) == 1

    def test_push_to_main_launches_pipeline(self, tmp_path):
        """push to main branch → full stack → 201 with run_id."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0push0002")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0push0002", GITHUB_PUSH_TO_MAIN)
        assert res.status_code == 201
        assert "run_id" in res.json()

    def test_pr_opened_launches_pipeline(self, tmp_path):
        """pull_request.opened → full stack → 201 with run_id."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0pr000003")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0pr000003", GITHUB_PR_OPENED)
        assert res.status_code == 201
        assert "run_id" in res.json()

    def test_fire_and_forget_with_real_payload(self, tmp_path):
        """fire_and_forget mode with real payload → 200 with status=accepted."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0faf00004", mode="fire_and_forget")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0faf00004", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "accepted"
        assert "run_id" in body


# ---------------------------------------------------------------------------
# Group B: Filter evaluation against real payloads
# ---------------------------------------------------------------------------

class TestE2EFilterEvaluation:
    """Filter matching evaluated against realistic GitHub payloads (not stub dicts)."""

    def test_label_filter_matches_bug_label(self, tmp_path):
        """issues.opened payload with bug label passes labels filter."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0lbl00005",
            filters=[{"labels": ["bug"]}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0lbl00005", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 201

    def test_label_filter_misses_wrong_label(self, tmp_path):
        """issues.opened payload without 'critical' label → filter_mismatch → 200 skipped."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0lbl00006",
            filters=[{"labels": ["critical"]}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0lbl00006", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "filter_mismatch"

    def test_branch_filter_matches_main(self, tmp_path):
        """push payload with refs/heads/main passes branch=main filter."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0brn00007",
            filters=[{"branch": "main"}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0brn00007", GITHUB_PUSH_TO_MAIN)
        assert res.status_code == 201

    def test_branch_filter_misses_feature_branch(self, tmp_path):
        """push payload with refs/heads/main does NOT match branch=develop → skipped."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0brn00008",
            filters=[{"branch": "develop"}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0brn00008", GITHUB_PUSH_TO_MAIN)
        assert res.status_code == 200
        assert res.json()["reason"] == "filter_mismatch"

    def test_action_filter_matches_opened(self, tmp_path):
        """issues.opened payload passes action=opened filter."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0act00009",
            filters=[{"action": "opened"}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0act00009", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 201

    def test_action_filter_misses_wrong_action(self, tmp_path):
        """pull_request.opened does NOT match action=closed → skipped."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0act00010",
            filters=[{"action": "closed"}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0act00010", GITHUB_PR_OPENED)
        assert res.status_code == 200
        assert res.json()["reason"] == "filter_mismatch"

    def test_combined_action_and_label_filter_matches(self, tmp_path):
        """AND combination: action=opened AND labels=[bug] both match → 201."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0and00011",
            filters=[{"action": "opened"}, {"labels": ["bug"]}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0and00011", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 201

    def test_combined_filter_fails_when_label_missing(self, tmp_path):
        """AND combination: action=opened OK but labels=[critical] fails → skipped."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0and00012",
            filters=[{"action": "opened"}, {"labels": ["critical"]}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0and00012", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 200
        assert res.json()["reason"] == "filter_mismatch"

    def test_no_filters_always_fires(self, tmp_path):
        """Empty filters list passes every payload (no filtering)."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0nof00013", filters=[])
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0nof00013", GITHUB_PUSH_TO_MAIN)
        assert res.status_code == 201


# ---------------------------------------------------------------------------
# Group C: Multiple triggers — selective firing
# ---------------------------------------------------------------------------

class TestE2EMultipleTriggers:
    """Multiple triggers registered for the same event — only matching ones fire."""

    def test_two_triggers_different_filters_only_matching_fires(self, tmp_path):
        """
        Two triggers for issues.opened:
          - trig-A has labels=[bug] → should fire (201)
          - trig-B has labels=[critical] → should skip (filter_mismatch)
        """
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0mul00014",
            filters=[{"labels": ["bug"]}],
        )
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0mul00015",
            filters=[{"labels": ["critical"]}],
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res_a = _post_github(client, "trig-e2e0mul00014", GITHUB_ISSUE_OPENED_BUG)
            res_b = _post_github(client, "trig-e2e0mul00015", GITHUB_ISSUE_OPENED_BUG)

        assert res_a.status_code == 201
        assert res_b.status_code == 200
        assert res_b.json()["reason"] == "filter_mismatch"

    def test_two_triggers_same_event_one_disabled(self, tmp_path):
        """Disabled trigger skips while enabled trigger fires."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00016", enabled=True)
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00017", enabled=False)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res_enabled = _post_github(client, "trig-e2e0mul00016", GITHUB_PUSH_TO_MAIN)
            res_disabled = _post_github(client, "trig-e2e0mul00017", GITHUB_PUSH_TO_MAIN)
        assert res_enabled.status_code == 201
        assert res_disabled.status_code == 200
        assert res_disabled.json()["reason"] == "trigger_disabled"

    def test_three_triggers_branch_specificity(self, tmp_path):
        """
        Three triggers:
          - main-trigger: branch=main → fires on push to main
          - develop-trigger: branch=develop → skips on push to main
          - any-trigger: no filters → fires on push to main
        """
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00018", filters=[{"branch": "main"}])
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00019", filters=[{"branch": "develop"}])
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00020", filters=[])

        with patch("subprocess.Popen", side_effect=_fake_popen):
            res_main = _post_github(client, "trig-e2e0mul00018", GITHUB_PUSH_TO_MAIN)
            res_dev = _post_github(client, "trig-e2e0mul00019", GITHUB_PUSH_TO_MAIN)
            res_any = _post_github(client, "trig-e2e0mul00020", GITHUB_PUSH_TO_MAIN)

        assert res_main.status_code == 201
        assert res_dev.status_code == 200
        assert res_dev.json()["reason"] == "filter_mismatch"
        assert res_any.status_code == 201

    def test_separate_invocations_tracked_per_trigger(self, tmp_path):
        """Each trigger's invocations are tracked independently in the DB."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00021", filters=[])
        _insert_trigger(db_path, trigger_id="trig-e2e0mul00022", filters=[])

        with patch("subprocess.Popen", side_effect=_fake_popen):
            _post_github(client, "trig-e2e0mul00021", GITHUB_PUSH_TO_MAIN)
            _post_github(client, "trig-e2e0mul00021", GITHUB_PUSH_TO_MAIN)
            _post_github(client, "trig-e2e0mul00022", GITHUB_PUSH_TO_MAIN)

        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)
        assert db.count_webhook_invocations_since("trig-e2e0mul00021", since) == 2
        assert db.count_webhook_invocations_since("trig-e2e0mul00022", since) == 1


# ---------------------------------------------------------------------------
# Group D: Input map with real GitHub payloads
# ---------------------------------------------------------------------------

def _get_run_input(db_path: str, run_id: str) -> Dict[str, Any]:
    """Read the input_json from a pipeline run record and deserialize it."""
    db = Database(Path(db_path))
    run = db.get_pipeline_run(run_id)
    assert run is not None, f"Run {run_id} not found in DB"
    return json.loads(run["input_json"])


class TestE2EInputMap:
    """Both $.path and {{payload.*}} input_map variants on real GitHub payloads."""

    def test_dot_path_extracts_repository_full_name(self, tmp_path):
        """$.repository.full_name extracts correctly from push payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0023",
            input_map={"repo": "$.repository.full_name"},
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0023", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("repo") == "org/my-repo"

    def test_dot_path_extracts_ref(self, tmp_path):
        """$.ref extracts the push ref from the payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0024",
            input_map={"branch_ref": "$.ref"},
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0024", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("branch_ref") == "refs/heads/main"

    def test_dot_path_extracts_nested_sender(self, tmp_path):
        """$.sender.login extracts nested field from push payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0024b",
            input_map={"actor": "$.sender.login"},
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0024b", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("actor") == "octocat"

    def test_template_syntax_extracts_issue_title(self, tmp_path):
        """{{payload.issue.title}} extracts issue title from issues.opened payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0025",
            input_map={"issue_title": "{{payload.issue.title}}"},
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0025", GITHUB_ISSUE_OPENED_BUG)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("issue_title") == "Something is broken"

    def test_template_syntax_extracts_sender_login(self, tmp_path):
        """{{payload.sender.login}} extracts sender from push payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0026",
            input_map={"sender": "{{payload.sender.login}}"},
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0026", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("sender") == "octocat"

    def test_mixed_dot_path_and_template_syntax(self, tmp_path):
        """Mixed input_map: $.path and {{payload.*}} variants on same trigger."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0imap0027",
            input_map={
                "repo": "$.repository.full_name",      # $.path style
                "actor": "{{payload.sender.login}}",   # template style
                "env": "production",                    # literal
            },
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0027", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        assert input_data.get("repo") == "org/my-repo"
        assert input_data.get("actor") == "octocat"
        assert input_data.get("env") == "production"

    def test_empty_input_map_passes_full_payload(self, tmp_path):
        """When input_map is empty, the full payload dict is passed as input."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0imap0028", input_map={})
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0imap0028", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        input_data = _get_run_input(db_path, run_id)
        # Full payload should be present
        assert input_data.get("ref") == "refs/heads/main"
        assert "repository" in input_data


# ---------------------------------------------------------------------------
# Group E: Guards — all guards from a consumer perspective
# ---------------------------------------------------------------------------

class TestE2EAllGuards:
    """All webhook guards (sig, rate-limit, disabled, filter) in E2E context."""

    def test_disabled_trigger_skips_with_real_payload(self, tmp_path):
        """Disabled trigger returns skipped even for valid real payload."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00029", enabled=False)
        with patch("subprocess.Popen", side_effect=_fake_popen) as mock_popen:
            res = _post_github(client, "trig-e2e0grd00029", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "trigger_disabled"
        mock_popen.assert_not_called()

    def test_signature_required_with_real_payload_missing_sig(self, tmp_path):
        """Trigger with secret rejects real GitHub payload missing signature."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00030", secret="my-webhook-secret")
        res = _post_github(client, "trig-e2e0grd00030", GITHUB_PUSH_TO_MAIN)
        # No signature → 403
        assert res.status_code == 403

    def test_signature_valid_with_real_payload(self, tmp_path):
        """Trigger with secret accepts real payload with correct HMAC-SHA256."""
        client, db_path = _make_app_and_db(tmp_path)
        secret = "my-webhook-secret"
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00031", secret=secret)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(
                client, "trig-e2e0grd00031", GITHUB_PUSH_TO_MAIN, secret=secret
            )
        assert res.status_code == 201

    def test_signature_wrong_secret_rejected(self, tmp_path):
        """Real payload signed with wrong secret is rejected with 403."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00032", secret="correct-secret")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(
                client, "trig-e2e0grd00032", GITHUB_PUSH_TO_MAIN, secret="wrong-secret"
            )
        assert res.status_code == 403

    def test_rate_limit_exceeded_with_real_payload(self, tmp_path):
        """Rate-limit=1: first real payload fires, second is rejected 429."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00033", rate_limit=1)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res1 = _post_github(client, "trig-e2e0grd00033", GITHUB_PUSH_TO_MAIN)
        # Second without mock (no launch expected due to rate limit)
        res2 = _post_github(client, "trig-e2e0grd00033", GITHUB_PUSH_TO_MAIN)

        assert res1.status_code == 201
        assert res2.status_code == 429

    def test_filter_mismatch_prevents_invocation_record(self, tmp_path):
        """Filter mismatch must NOT record an invocation in the DB."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0grd00034",
            filters=[{"labels": ["critical"]}],  # bug label won't match
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0grd00034", GITHUB_ISSUE_OPENED_BUG)

        assert res.status_code == 200
        assert res.json()["reason"] == "filter_mismatch"
        # No invocation should be recorded for a skipped payload
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)
        assert db.count_webhook_invocations_since("trig-e2e0grd00034", since) == 0

    def test_unknown_trigger_returns_404_with_real_payload(self, tmp_path):
        """POST to unknown trigger returns 404 regardless of payload validity."""
        client, db_path = _make_app_and_db(tmp_path)
        res = _post_github(client, "trig-nonexistent-e2e", GITHUB_ISSUE_OPENED_BUG)
        assert res.status_code == 404

    def test_disabled_trigger_does_not_record_invocation(self, tmp_path):
        """Disabled trigger must not record an invocation in the DB."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0grd00035", enabled=False)
        _post_github(client, "trig-e2e0grd00035", GITHUB_ISSUE_OPENED_BUG)
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)
        assert db.count_webhook_invocations_since("trig-e2e0grd00035", since) == 0


# ---------------------------------------------------------------------------
# Group F: DB post-conditions after successful launches
# ---------------------------------------------------------------------------

class TestE2EDbPostConditions:
    """DB state verification after successful end-to-end webhook launches."""

    def test_run_record_created_after_launch(self, tmp_path):
        """Successful webhook creates a pipeline_run record in the DB."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0db000036")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0db000036", GITHUB_PUSH_TO_MAIN)

        assert res.status_code == 201
        run_id = res.json()["run_id"]
        assert run_id

        # Verify the run record exists via the API
        run_res = client.get(f"/api/v1/runs/{run_id}")
        assert run_res.status_code == 200
        assert run_res.json()["run_id"] == run_id

    def test_invocation_count_increments_per_launch(self, tmp_path):
        """Each successful launch increments the invocation count by exactly 1."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0db000037")
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)

        with patch("subprocess.Popen", side_effect=_fake_popen):
            _post_github(client, "trig-e2e0db000037", GITHUB_PUSH_TO_MAIN)
            assert db.count_webhook_invocations_since("trig-e2e0db000037", since) == 1
            _post_github(client, "trig-e2e0db000037", GITHUB_PUSH_TO_MAIN)
            assert db.count_webhook_invocations_since("trig-e2e0db000037", since) == 2
            _post_github(client, "trig-e2e0db000037", GITHUB_PUSH_TO_MAIN)
            assert db.count_webhook_invocations_since("trig-e2e0db000037", since) == 3

    def test_multiple_launches_create_distinct_run_ids(self, tmp_path):
        """Multiple webhook deliveries create distinct pipeline run records."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(db_path, trigger_id="trig-e2e0db000038")
        run_ids = []
        with patch("subprocess.Popen", side_effect=_fake_popen):
            for payload in [GITHUB_PUSH_TO_MAIN, GITHUB_ISSUE_OPENED_BUG, GITHUB_PR_OPENED]:
                res = _post_github(client, "trig-e2e0db000038", payload)
                assert res.status_code == 201
                run_ids.append(res.json()["run_id"])

        # All run IDs must be unique
        assert len(set(run_ids)) == 3

    def test_run_record_has_correct_template_id(self, tmp_path):
        """Pipeline run record must reference the trigger's template_id."""
        client, db_path = _make_app_and_db(tmp_path)
        _insert_trigger(
            db_path,
            trigger_id="trig-e2e0db000039",
            template_id=_TEMPLATE_ID,
        )
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, "trig-e2e0db000039", GITHUB_PUSH_TO_MAIN)

        run_id = res.json()["run_id"]
        run_res = client.get(f"/api/v1/runs/{run_id}")
        assert run_res.status_code == 200
        assert run_res.json()["template_id"] == _TEMPLATE_ID


# ---------------------------------------------------------------------------
# Group G: Parametrized cross-payload tests
# ---------------------------------------------------------------------------

class TestE2EParametrized:
    """Parametrized tests covering all three GitHub payloads."""

    @pytest.mark.parametrize("payload,desc", [
        (GITHUB_ISSUE_OPENED_BUG, "issues.opened"),
        (GITHUB_PUSH_TO_MAIN, "push"),
        (GITHUB_PR_OPENED, "pull_request.opened"),
    ])
    def test_all_payloads_launch_successfully(self, tmp_path, payload, desc):
        """All three GitHub payload types must launch successfully."""
        client, db_path = _make_app_and_db(tmp_path)
        # Use a unique trigger per parametrize invocation
        trigger_id = f"trig-e2e0par{abs(hash(desc)) % 100000:05d}"
        _insert_trigger(db_path, trigger_id=trigger_id)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = _post_github(client, trigger_id, payload)
        assert res.status_code == 201, f"Failed for {desc}: {res.text}"
        assert "run_id" in res.json()

    @pytest.mark.parametrize("payload,desc", [
        (GITHUB_ISSUE_OPENED_BUG, "issues.opened"),
        (GITHUB_PUSH_TO_MAIN, "push"),
        (GITHUB_PR_OPENED, "pull_request.opened"),
    ])
    def test_all_payloads_record_invocation(self, tmp_path, payload, desc):
        """All payload types must record an invocation after launch."""
        client, db_path = _make_app_and_db(tmp_path)
        trigger_id = f"trig-e2e0pr2{abs(hash(desc)) % 100000:05d}"
        _insert_trigger(db_path, trigger_id=trigger_id)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            _post_github(client, trigger_id, payload)
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=30)
        count = db.count_webhook_invocations_since(trigger_id, since)
        assert count == 1, f"Expected 1 invocation for {desc}, got {count}"
