"""Tests for the webhook HTTP endpoint: POST /api/v1/webhooks/{trigger_id}.

Covers:
  - Group A: Trigger resolution (404 for unknown, 200-skip for disabled)
  - Group B: Signature verification (valid, missing, bad when secret set)
  - Group C: Rate-limit enforcement (429 when exceeded, 201 when under limit)
  - Group D: Successful pipeline launch (async/sync → 201, fire_and_forget → 200)
  - Group E: Input map transformation
  - Group F: Helper unit tests (_verify_github_signature, _apply_input_map,
    _check_rate_limit)
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Skip entire module when FastAPI / starlette is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from orchestration_engine.db import Database  # noqa: E402
from orchestration_engine.web.api import (  # noqa: E402
    _apply_input_map,
    _check_rate_limit,
    _verify_github_signature,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A real bundled template ID — used so template resolution works without mocks.
_TEMPLATE_ID = "coding-pipeline-standard"

# A valid trigger ID matching the required pattern.
_TRIGGER_ID = "trig-webhook0001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by an isolated file-based DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _fake_popen(*args, **kwargs) -> MagicMock:
    """Return a mock Popen whose ``pid`` attribute is always 99999."""
    mock = MagicMock()
    mock.pid = 99999
    return mock


def _make_sig(secret: str, body: bytes) -> str:
    """Compute a valid GitHub-style HMAC-SHA256 signature header value."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _insert_trigger(
    db_path: str,
    trigger_id: str = _TRIGGER_ID,
    template_id: str = _TEMPLATE_ID,
    mode: str = "async",
    secret: str = None,
    rate_limit: int = 0,
    input_map: Dict[str, Any] = None,
    filters: list = None,
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
            "input_map": input_map or {},
            "filters": filters or [],
            "enabled": enabled,
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return path to an isolated test database file."""
    return str(tmp_path / "test-engine.db")


@pytest.fixture()
def client(tmp_path: Path):
    """Isolated TestClient per test (separate DB file in tmp_path)."""
    with _make_client(tmp_path) as c:
        yield c


@pytest.fixture()
def client_with_db(tmp_path: Path):
    """Yield (TestClient, db_path) tuple for tests that need both."""
    db_file = str(tmp_path / "test-engine.db")
    from orchestration_engine.web.api import create_api_app
    app = create_api_app(db_path=db_file)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, db_file


# ---------------------------------------------------------------------------
# Group A: Trigger Resolution
# ---------------------------------------------------------------------------

class TestTriggerResolution:
    """Trigger lookup and enabled-flag handling."""

    def test_unknown_trigger_returns_404(self, client):
        """POST to a nonexistent trigger_id must return 404."""
        res = client.post(
            "/api/v1/webhooks/nonexistent-trigger-id",
            json={"event": "push"},
        )
        assert res.status_code == 404

    def test_unknown_trigger_error_message(self, client):
        """404 response must mention the trigger id."""
        res = client.post(
            "/api/v1/webhooks/nonexistent-trigger-id",
            json={},
        )
        assert "nonexistent-trigger-id" in res.text

    def test_disabled_trigger_returns_200_skipped(self, client_with_db):
        """Disabled trigger must return 200 with status=skipped."""
        client, db_path = client_with_db
        _insert_trigger(db_path, enabled=False)
        res = client.post(
            f"/api/v1/webhooks/{_TRIGGER_ID}",
            json={"event": "push"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "trigger_disabled"

    def test_disabled_trigger_does_not_launch_pipeline(self, client_with_db):
        """Disabled trigger must not spawn a daemon subprocess."""
        client, db_path = client_with_db
        _insert_trigger(db_path, enabled=False)
        with patch("subprocess.Popen", side_effect=_fake_popen) as mock_popen:
            client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        mock_popen.assert_not_called()

    def test_enabled_trigger_proceeds_past_enabled_check(self, client_with_db):
        """Enabled trigger must not return 200 skipped."""
        client, db_path = client_with_db
        _insert_trigger(db_path, enabled=True)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        # Should be 201 (launched), not 200 skipped
        assert res.status_code != 200 or res.json().get("status") != "skipped"


# ---------------------------------------------------------------------------
# Group B: Signature Verification
# ---------------------------------------------------------------------------

class TestSignatureVerification:
    """HMAC-SHA256 GitHub-style webhook signature enforcement."""

    def test_missing_signature_header_returns_403(self, client_with_db):
        """When secret is set, missing X-Hub-Signature-256 must return 403."""
        client, db_path = client_with_db
        _insert_trigger(db_path, secret="my-secret")
        res = client.post(
            f"/api/v1/webhooks/{_TRIGGER_ID}",
            json={"event": "push"},
        )
        assert res.status_code == 403

    def test_bad_signature_returns_403(self, client_with_db):
        """When secret is set, a wrong signature must return 403."""
        client, db_path = client_with_db
        _insert_trigger(db_path, secret="my-secret")
        res = client.post(
            f"/api/v1/webhooks/{_TRIGGER_ID}",
            content=b'{"event": "push"}',
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=deadbeefdeadbeefdeadbeefdeadbeef00000000000000000000000000000000",
            },
        )
        assert res.status_code == 403

    def test_valid_signature_proceeds(self, client_with_db):
        """Correct HMAC-SHA256 signature must allow the request to proceed."""
        client, db_path = client_with_db
        _insert_trigger(db_path, secret="my-secret")
        body = b'{"event": "push"}'
        sig = _make_sig("my-secret", body)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                },
            )
        assert res.status_code == 201

    def test_no_secret_no_sig_proceeds(self, client_with_db):
        """When no secret is configured, no signature check is performed."""
        client, db_path = client_with_db
        _insert_trigger(db_path, secret=None)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert res.status_code == 201

    def test_no_secret_with_sig_header_proceeds(self, client_with_db):
        """Signature header is ignored when trigger has no secret."""
        client, db_path = client_with_db
        _insert_trigger(db_path, secret=None)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
                headers={"X-Hub-Signature-256": "sha256=irrelevant"},
            )
        assert res.status_code == 201


# ---------------------------------------------------------------------------
# Group C: Rate-Limit Enforcement
# ---------------------------------------------------------------------------

class TestRateLimitEnforcement:
    """Per-trigger rate-limit (requests per minute) enforcement."""

    def test_rate_limit_zero_means_unlimited(self, client_with_db):
        """rate_limit=0 (unlimited) must never return 429."""
        client, db_path = client_with_db
        _insert_trigger(db_path, rate_limit=0)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            for _ in range(5):
                res = client.post(
                    f"/api/v1/webhooks/{_TRIGGER_ID}",
                    json={"event": "push"},
                )
        assert res.status_code == 201

    def test_rate_limit_exceeded_returns_429(self, client_with_db):
        """When rate_limit is 1, the second request in the same minute must return 429."""
        client, db_path = client_with_db
        _insert_trigger(db_path, rate_limit=1)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res1 = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        # Second request without waiting → rate limited
        res2 = client.post(
            f"/api/v1/webhooks/{_TRIGGER_ID}",
            json={"event": "push"},
        )
        assert res1.status_code == 201
        assert res2.status_code == 429

    def test_rate_limit_not_yet_exceeded_returns_201(self, client_with_db):
        """Requests under the rate limit must succeed."""
        client, db_path = client_with_db
        _insert_trigger(db_path, rate_limit=5)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            for _ in range(5):
                res = client.post(
                    f"/api/v1/webhooks/{_TRIGGER_ID}",
                    json={"event": "push"},
                )
        assert res.status_code == 201

    def test_rate_limit_exceeded_error_detail(self, client_with_db):
        """429 response must mention the trigger id and rate limit."""
        client, db_path = client_with_db
        _insert_trigger(db_path, rate_limit=1)
        with patch("subprocess.Popen", side_effect=_fake_popen):
            client.post(f"/api/v1/webhooks/{_TRIGGER_ID}", json={})
        res = client.post(f"/api/v1/webhooks/{_TRIGGER_ID}", json={})
        assert res.status_code == 429
        assert _TRIGGER_ID in res.text


# ---------------------------------------------------------------------------
# Group D: Successful Pipeline Launch
# ---------------------------------------------------------------------------

class TestSuccessfulLaunch:
    """Successful webhook → pipeline launch response shapes."""

    def test_async_trigger_returns_201(self, client_with_db):
        """Async mode trigger must return 201 with a run record."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="async")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert res.status_code == 201

    def test_async_trigger_response_has_run_id(self, client_with_db):
        """Async launch response must contain a run_id field."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="async")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert "run_id" in res.json()

    def test_sync_trigger_returns_201(self, client_with_db):
        """Sync mode trigger must return 201 with a run record."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="sync")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert res.status_code == 201

    def test_fire_and_forget_returns_200(self, client_with_db):
        """fire_and_forget mode must return 200 (not 201)."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="fire_and_forget")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert res.status_code == 200

    def test_fire_and_forget_response_has_run_id(self, client_with_db):
        """fire_and_forget response must contain a run_id field."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="fire_and_forget")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        body = res.json()
        assert "run_id" in body

    def test_fire_and_forget_status_accepted(self, client_with_db):
        """fire_and_forget response must have status='accepted'."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="fire_and_forget")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        assert res.json()["status"] == "accepted"

    def test_launch_records_invocation(self, client_with_db):
        """Successful webhook must record an invocation row in webhook_invocations."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="async")
        with patch("subprocess.Popen", side_effect=_fake_popen):
            client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        db = Database(Path(db_path))
        since = datetime.now() - timedelta(seconds=10)
        count = db.count_webhook_invocations_since(_TRIGGER_ID, since)
        assert count == 1

    def test_daemon_is_spawned(self, client_with_db):
        """Successful webhook must call subprocess.Popen exactly once."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="async")
        with patch("subprocess.Popen", side_effect=_fake_popen) as mock_popen:
            client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push"},
            )
        mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# Group E: Input Map Transformation
# ---------------------------------------------------------------------------

class TestInputMapTransformation:
    """Webhook payload → pipeline input via input_map."""

    def test_empty_input_map_passes_payload_directly(self, client_with_db):
        """When input_map is empty, the full payload is passed as pipeline input."""
        client, db_path = client_with_db
        _insert_trigger(db_path, mode="async", input_map={})
        with patch("subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                json={"event": "push", "ref": "refs/heads/main"},
            )
        assert res.status_code == 201

    def test_input_map_literal_value(self):
        """_apply_input_map with a literal value (no $. prefix) returns it directly."""
        payload = {"event": "push"}
        input_map = {"env": "production"}
        result = _apply_input_map(payload, input_map)
        assert result["env"] == "production"

    def test_input_map_path_extraction(self):
        """_apply_input_map with $. path extracts nested payload value."""
        payload = {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main"}
        input_map = {
            "repo": "$.repository.full_name",
            "branch": "$.ref",
        }
        result = _apply_input_map(payload, input_map)
        assert result["repo"] == "org/repo"
        assert result["branch"] == "refs/heads/main"

    def test_input_map_missing_path_returns_none(self):
        """_apply_input_map returns None for paths that don't exist in payload."""
        payload = {"event": "push"}
        input_map = {"sha": "$.after"}
        result = _apply_input_map(payload, input_map)
        assert result["sha"] is None

    def test_input_map_nested_path(self):
        """_apply_input_map handles deeply nested paths."""
        payload = {"a": {"b": {"c": "deep_value"}}}
        input_map = {"val": "$.a.b.c"}
        result = _apply_input_map(payload, input_map)
        assert result["val"] == "deep_value"

    def test_input_map_mixed(self):
        """_apply_input_map handles a mix of path and literal values."""
        payload = {"event": "push", "ref": "refs/heads/main"}
        input_map = {
            "branch": "$.ref",
            "pipeline_env": "production",
        }
        result = _apply_input_map(payload, input_map)
        assert result["branch"] == "refs/heads/main"
        assert result["pipeline_env"] == "production"


# ---------------------------------------------------------------------------
# Group F: Helper Unit Tests
# ---------------------------------------------------------------------------

class TestVerifyGithubSignature:
    """Unit tests for _verify_github_signature helper."""

    def test_valid_signature_returns_true(self):
        """Valid HMAC-SHA256 signature must return True."""
        secret = "test-secret"
        body = b'{"event": "push"}'
        sig = _make_sig(secret, body)
        assert _verify_github_signature(secret, body, sig) is True

    def test_wrong_signature_returns_false(self):
        """Wrong signature value must return False."""
        secret = "test-secret"
        body = b'{"event": "push"}'
        assert _verify_github_signature(secret, body, "sha256=wrongvalue0000000000000000000000000000000000000000000000000000") is False

    def test_missing_sig_header_returns_false(self):
        """None signature header must return False."""
        assert _verify_github_signature("secret", b"body", None) is False

    def test_empty_sig_header_returns_false(self):
        """Empty string signature header must return False."""
        assert _verify_github_signature("secret", b"body", "") is False

    def test_malformed_sig_no_prefix_returns_false(self):
        """Signature header without sha256= prefix must return False."""
        assert _verify_github_signature("secret", b"body", "notasignature") is False

    def test_different_secret_returns_false(self):
        """Signature computed with a different secret must return False."""
        body = b'{"event": "push"}'
        sig = _make_sig("correct-secret", body)
        assert _verify_github_signature("wrong-secret", body, sig) is False

    def test_different_body_returns_false(self):
        """Signature computed for a different body must return False."""
        secret = "test-secret"
        sig = _make_sig(secret, b"original-body")
        assert _verify_github_signature(secret, b"tampered-body", sig) is False


class TestCheckRateLimit:
    """Unit tests for _check_rate_limit helper."""

    def test_rate_limit_zero_always_false(self, tmp_path):
        """rate_limit=0 (unlimited) must always return False."""
        db = Database(tmp_path / "rl.db")
        assert _check_rate_limit("trig-test0001", 0, db) is False

    def test_rate_limit_exceeded_returns_true(self, tmp_path):
        """When invocation count >= rate_limit, must return True."""
        db = Database(tmp_path / "rl.db")
        trigger_id = "trig-test0001"
        db.record_webhook_invocation(trigger_id)
        assert _check_rate_limit(trigger_id, 1, db) is True

    def test_rate_limit_not_exceeded_returns_false(self, tmp_path):
        """When invocation count < rate_limit, must return False."""
        db = Database(tmp_path / "rl.db")
        trigger_id = "trig-test0001"
        # 0 invocations, limit 5 → not exceeded
        assert _check_rate_limit(trigger_id, 5, db) is False

    def test_rate_limit_exactly_at_limit_is_exceeded(self, tmp_path):
        """When invocation count == rate_limit, must return True (>= check)."""
        db = Database(tmp_path / "rl.db")
        trigger_id = "trig-test0001"
        db.record_webhook_invocation(trigger_id)
        db.record_webhook_invocation(trigger_id)
        assert _check_rate_limit(trigger_id, 2, db) is True
