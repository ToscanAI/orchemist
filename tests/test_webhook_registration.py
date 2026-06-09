"""Integration tests for GitHub webhook registration and the regression trigger.

Covers the full end-to-end path from a ``check_suite.completed`` GitHub
webhook POST through HMAC verification, filter matching, input mapping,
and pipeline launch — proving that ``register_regression_trigger()`` wires
up the system correctly.

Also tests the new ``GET /api/v1/health/webhook`` endpoint introduced by
issue #429.2.

Issue: #429.2
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient

from orchestration_engine.db import Database  # noqa: E402
from orchestration_engine.regression import register_regression_trigger  # noqa: E402
from orchestration_engine.web.api import create_api_app  # noqa: E402

# Use the coding pipeline as a stand-in template (bundled, always present).
_TEMPLATE_ID = "coding-pipeline-standard"
_TRIGGER_ID = "regression-ci-trigger"
_WEBHOOK_SECRET = "test-hmac-secret-for-regression-trigger"


# ---------------------------------------------------------------------------
# Realistic check_suite.completed payload
# ---------------------------------------------------------------------------

GITHUB_CHECK_SUITE_COMPLETED_FAILURE = {
    "action": "completed",
    "check_suite": {
        "id": 12345678,
        "conclusion": "failure",
        "head_sha": "abc123def456abc123def456abc123def456abc1",
        "status": "completed",
        "url": "https://api.github.com/repos/ToscanAI/orchestration-engine/check-suites/12345678",
        "check_runs_url": "https://api.github.com/repos/ToscanAI/orchestration-engine/check-suites/12345678/check-runs",
    },
    "repository": {
        "full_name": "ToscanAI/orchestration-engine",
        "name": "orchestration-engine",
        "owner": {"login": "ToscanAI"},
        "html_url": "https://github.com/ToscanAI/orchestration-engine",
        "default_branch": "main",
    },
    "sender": {"login": "github-actions[bot]"},
}

GITHUB_CHECK_SUITE_COMPLETED_SUCCESS = {
    "action": "completed",
    "check_suite": {
        "id": 12345679,
        "conclusion": "success",
        "head_sha": "def456abc123def456abc123def456abc123def4",
        "status": "completed",
        "url": "https://api.github.com/repos/ToscanAI/orchestration-engine/check-suites/12345679",
    },
    "repository": {
        "full_name": "ToscanAI/orchestration-engine",
        "name": "orchestration-engine",
    },
    "sender": {"login": "github-actions[bot]"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Compute the GitHub-style X-Hub-Signature-256 header value."""
    digest = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _make_app_with_trigger(
    tmp_path: Path,
    trigger_id: str = _TRIGGER_ID,
    template_id: str = _TEMPLATE_ID,
    secret: str = _WEBHOOK_SECRET,
) -> tuple:
    """Create a TestClient and Database with a pre-registered regression trigger.

    Returns:
        (TestClient, Database) tuple.
    """
    db_path = tmp_path / "test-engine.db"
    db = Database(db_path=db_path)

    # Register the trigger using the production function
    register_regression_trigger(db=db, trigger_id=trigger_id, template_id=template_id)
    # Set the HMAC secret on the trigger
    db.update_trigger(trigger_id, secret=secret)

    app = create_api_app(db_path=str(db_path))
    client = TestClient(app, raise_server_exceptions=False)
    return client, db


# ---------------------------------------------------------------------------
# Tests — register_regression_trigger() DB side effects
# ---------------------------------------------------------------------------


class TestRegisterRegressionTrigger:
    """Unit tests for the register_regression_trigger helper."""

    def test_creates_trigger_row(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        row = db.get_trigger(_TRIGGER_ID)
        assert row is not None
        assert row["id"] == _TRIGGER_ID
        assert row["template_id"] == _TEMPLATE_ID

    def test_trigger_mode_is_fire_and_forget(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        row = db.get_trigger(_TRIGGER_ID)
        assert row["mode"] == "fire_and_forget"

    def test_trigger_has_action_completed_filter(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        row = db.get_trigger(_TRIGGER_ID)
        filters = row.get("filters") or []
        # Expect at least one filter targeting action == "completed"
        assert any(f.get("action") == "completed" for f in filters), (
            f"Expected filter with action='completed', got: {filters!r}"
        )

    def test_trigger_has_event_payload_input_map(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        row = db.get_trigger(_TRIGGER_ID)
        input_map = row.get("input_map") or {}
        assert "event_payload" in input_map, (
            f"Expected 'event_payload' key in input_map, got: {input_map!r}"
        )

    def test_trigger_is_idempotent_raises_integrity_error(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        with pytest.raises(sqlite3.IntegrityError):
            register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)

    def test_secret_stored_after_update(self, tmp_path):
        db = Database(db_path=tmp_path / "engine.db")
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        db.update_trigger(_TRIGGER_ID, secret=_WEBHOOK_SECRET)
        row = db.get_trigger(_TRIGGER_ID)
        # Secret is stored (but we can verify it's not None)
        assert row.get("secret") is not None


# ---------------------------------------------------------------------------
# Tests — Webhook POST end-to-end
# ---------------------------------------------------------------------------


class TestCheckSuiteWebhookEndToEnd:
    """Full-stack E2E tests: webhook POST → signature → filter → launch."""

    def _patch_launch(self):
        """Return a context manager that patches the daemon Popen subprocess."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        return patch("subprocess.Popen", return_value=mock_proc)

    def test_check_suite_completed_failure_fires_pipeline(self, tmp_path):
        """A properly signed check_suite.completed (failure) fires the pipeline."""
        client, db = _make_app_with_trigger(tmp_path)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_FAILURE).encode()
        sig = _sign_payload(payload_bytes, _WEBHOOK_SECRET)

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                },
            )

        # fire_and_forget → 200 accepted
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"
        assert "run_id" in body

    def test_check_suite_completed_success_fires_pipeline(self, tmp_path):
        """A properly signed check_suite.completed (success) also fires the pipeline.

        The regression handler filters on conclusion — but the *trigger* filter
        only checks ``action == 'completed'``, so both pass through.
        """
        client, db = _make_app_with_trigger(tmp_path)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_SUCCESS).encode()
        sig = _sign_payload(payload_bytes, _WEBHOOK_SECRET)

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                },
            )

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def test_invalid_signature_rejected(self, tmp_path):
        """A request with an invalid HMAC signature is rejected with 403."""
        client, _ = _make_app_with_trigger(tmp_path)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_FAILURE).encode()

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": "sha256=badhash",
                    "X-GitHub-Event": "check_suite",
                },
            )

        assert response.status_code == 403

    def test_missing_signature_rejected(self, tmp_path):
        """A request without a signature header is rejected with 403."""
        client, _ = _make_app_with_trigger(tmp_path)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_FAILURE).encode()

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 403

    def test_filter_mismatch_skipped(self, tmp_path):
        """A payload whose action is not 'completed' is skipped (filter mismatch)."""
        client, _ = _make_app_with_trigger(tmp_path)

        # Payload with action = "created" should not match the "completed" filter
        payload = {**GITHUB_CHECK_SUITE_COMPLETED_FAILURE, "action": "created"}
        payload_bytes = json.dumps(payload).encode()
        sig = _sign_payload(payload_bytes, _WEBHOOK_SECRET)

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "filter_mismatch"

    def test_unknown_trigger_returns_404(self, tmp_path):
        """A request to an unregistered trigger ID returns 404."""
        db_path = tmp_path / "engine.db"
        app = create_api_app(db_path=str(db_path))
        client = TestClient(app, raise_server_exceptions=False)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_FAILURE).encode()

        response = client.post(
            "/api/v1/webhooks/does-not-exist",
            content=payload_bytes,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 404

    def test_invocation_recorded_in_db(self, tmp_path):
        """A successful webhook POST records an invocation in the DB."""
        client, db = _make_app_with_trigger(tmp_path)

        payload_bytes = json.dumps(GITHUB_CHECK_SUITE_COMPLETED_FAILURE).encode()
        sig = _sign_payload(payload_bytes, _WEBHOOK_SECRET)

        with self._patch_launch():
            response = client.post(
                f"/api/v1/webhooks/{_TRIGGER_ID}",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                },
            )

        assert response.status_code == 200
        # Verify an invocation was recorded
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(seconds=10)
        count = db.count_webhook_invocations_since(_TRIGGER_ID, since)
        assert count >= 1


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/health/webhook
# ---------------------------------------------------------------------------


class TestWebhookHealthEndpoint:
    """Tests for the GET /api/v1/health/webhook endpoint."""

    def _app_and_client(self, tmp_path: Path) -> tuple:
        db_path = tmp_path / "engine.db"
        app = create_api_app(db_path=str(db_path))
        client = TestClient(app, raise_server_exceptions=False)
        db = Database(db_path=db_path)
        return client, db, db_path

    def test_returns_error_when_trigger_not_registered(self, tmp_path):
        """When no regression trigger exists, status is 'error'."""
        client, db, _ = self._app_and_client(tmp_path)
        env = {"REGRESSION_TRIGGER_ID": "regression-ci-trigger"}
        with patch.dict(os.environ, env):
            response = client.get("/api/v1/health/webhook")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        assert body["trigger_registered"] is False
        assert body["trigger_id"] is None

    def test_returns_degraded_when_trigger_registered_but_gh_unavailable(self, tmp_path):
        """When trigger is in DB but gh CLI is unavailable, status is 'degraded'."""
        client, db, db_path = self._app_and_client(tmp_path)

        # Register the trigger
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)

        env = {"REGRESSION_TRIGGER_ID": _TRIGGER_ID}
        # Patch subprocess.run to simulate gh not being available
        with patch.dict(os.environ, env):
            with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
                response = client.get("/api/v1/health/webhook")

        assert response.status_code == 200
        body = response.json()
        assert body["trigger_registered"] is True
        assert body["trigger_id"] == _TRIGGER_ID
        assert body["github_webhook_id"] is None
        assert body["github_webhook_active"] is None
        assert body["status"] == "degraded"

    def test_returns_ok_when_trigger_and_github_hook_active(self, tmp_path):
        """When trigger is in DB and GitHub returns an active hook, status is 'ok'."""
        client, db, _ = self._app_and_client(tmp_path)
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)

        # Simulate gh returning a hook matching the trigger ID
        gh_hooks_response = [
            {
                "id": 98765,
                "active": True,
                "config": {
                    "url": f"https://myhost.example.com/api/v1/webhooks/{_TRIGGER_ID}",
                    "content_type": "json",
                },
            }
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(gh_hooks_response)

        env = {"REGRESSION_TRIGGER_ID": _TRIGGER_ID}
        with patch.dict(os.environ, env):
            with patch("subprocess.run", return_value=mock_result):
                response = client.get("/api/v1/health/webhook")

        assert response.status_code == 200
        body = response.json()
        assert body["trigger_registered"] is True
        assert body["trigger_id"] == _TRIGGER_ID
        assert body["github_webhook_id"] == 98765
        assert body["github_webhook_active"] is True
        assert body["status"] == "ok"

    def test_returns_degraded_when_github_hook_not_found(self, tmp_path):
        """When trigger is in DB but GitHub hook not found, status is 'degraded'."""
        client, db, _ = self._app_and_client(tmp_path)
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)

        # gh returns an empty list (no matching hooks)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([])

        env = {"REGRESSION_TRIGGER_ID": _TRIGGER_ID}
        with patch.dict(os.environ, env):
            with patch("subprocess.run", return_value=mock_result):
                response = client.get("/api/v1/health/webhook")

        assert response.status_code == 200
        body = response.json()
        assert body["trigger_registered"] is True
        assert body["github_webhook_id"] is None
        assert body["status"] == "degraded"

    def test_fallback_scan_finds_regression_template(self, tmp_path):
        """Without REGRESSION_TRIGGER_ID set, endpoint scans by template_id."""
        client, db, _ = self._app_and_client(tmp_path)

        # Register using a known regression template ID
        register_regression_trigger(
            db=db,
            trigger_id="custom-regression-trigger",
            template_id="regression-pipeline-v1",
        )

        # Ensure env var is not set so fallback scan is used
        env_without = {k: v for k, v in os.environ.items() if k != "REGRESSION_TRIGGER_ID"}

        with patch.dict(os.environ, {"REGRESSION_TRIGGER_ID": ""}, clear=False):
            # Override REGRESSION_TRIGGER_ID to empty string so fallback activates
            with patch.dict(os.environ, {"REGRESSION_TRIGGER_ID": "nonexistent-trigger-xxx"}):
                with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
                    response = client.get("/api/v1/health/webhook")

        # The DB check for "nonexistent-trigger-xxx" fails, but fallback scan
        # should find "custom-regression-trigger" whose template is "regression-pipeline-v1"
        assert response.status_code == 200
        body = response.json()
        # Status should be degraded (trigger found via fallback, gh unavailable)
        assert body["trigger_registered"] is True
        assert body["trigger_id"] == "custom-regression-trigger"

    def test_response_shape(self, tmp_path):
        """Verify the response always contains all expected keys."""
        client, db, _ = self._app_and_client(tmp_path)
        env = {"REGRESSION_TRIGGER_ID": "regression-ci-trigger"}
        with patch.dict(os.environ, env):
            with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
                response = client.get("/api/v1/health/webhook")

        body = response.json()
        assert "trigger_registered" in body
        assert "trigger_id" in body
        assert "github_webhook_id" in body
        assert "github_webhook_active" in body
        assert "status" in body
        assert body["status"] in ("ok", "degraded", "error")

    def test_gh_returns_non_zero_degrades_gracefully(self, tmp_path):
        """When gh exits with non-zero code, the endpoint still returns degraded."""
        client, db, _ = self._app_and_client(tmp_path)
        register_regression_trigger(db=db, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "gh: Not authenticated"

        env = {"REGRESSION_TRIGGER_ID": _TRIGGER_ID}
        with patch.dict(os.environ, env):
            with patch("subprocess.run", return_value=mock_result):
                response = client.get("/api/v1/health/webhook")

        assert response.status_code == 200
        body = response.json()
        assert body["trigger_registered"] is True
        assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# Tests — scripts/register_webhook.py
# ---------------------------------------------------------------------------


class TestRegisterWebhookScript:
    """Unit tests for the register_webhook.py setup script."""

    def test_generate_and_store_secret(self, tmp_path):
        """generate_and_store_secret() creates a 64-char hex secret with mode 0600."""
        import sys
        # Ensure the script is importable
        script_dir = Path(__file__).parent.parent / "scripts"
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))

        # Import the function directly
        import importlib.util
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        secret_path = tmp_path / "webhook-secret"
        secret = module.generate_and_store_secret(secret_path)

        # Secret must be 64 hex chars (32 bytes)
        assert len(secret) == 64
        assert all(c in "0123456789abcdef" for c in secret)

        # File must exist and be readable
        assert secret_path.exists()
        assert secret_path.read_text() == secret

        # File permissions must be 0600
        mode = oct(secret_path.stat().st_mode)[-3:]
        assert mode == "600", f"Expected mode 600, got {mode}"

    def test_parse_args_defaults(self, tmp_path):
        """parse_args() returns correct defaults."""
        import importlib.util
        script_dir = Path(__file__).parent.parent / "scripts"
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        with patch.dict(os.environ, {"WEBHOOK_PUBLIC_URL": "", "REGRESSION_TRIGGER_ID": ""}):
            args = module.parse_args(["--url", "https://example.com/hook"])

        assert args.repo == "ToscanAI/orchestration-engine"
        assert args.url == "https://example.com/hook"
        assert args.dry_run is False

    def test_dry_run_exits_zero_without_side_effects(self, tmp_path):
        """--dry-run exits 0 without creating files, triggers, or calling gh."""
        import importlib.util
        script_dir = Path(__file__).parent.parent / "scripts"
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        result = module.main(
            [
                "--url", "https://example.com/hook",
                "--db", str(tmp_path / "engine.db"),
                "--dry-run",
            ]
        )
        assert result == 0
        # No DB file should have been created
        assert not (tmp_path / "engine.db").exists()

    def test_missing_url_exits_nonzero(self, tmp_path):
        """Missing --url causes the script to return a non-zero exit code."""
        import importlib.util
        script_dir = Path(__file__).parent.parent / "scripts"
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        with patch.dict(os.environ, {"WEBHOOK_PUBLIC_URL": ""}):
            result = module.main(["--db", str(tmp_path / "engine.db")])

        assert result == 1

    def test_main_success_with_mocked_gh(self, tmp_path):
        """main() succeeds when gh CLI is mocked to return a webhook ID."""
        import importlib.util
        script_dir = Path(__file__).parent.parent / "scripts"
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"id": 99999, "active": True})

        with patch("subprocess.run", return_value=mock_result):
            result = module.main(
                [
                    "--url", "https://example.com/api/v1/webhooks/regression-ci-trigger",
                    "--db", str(tmp_path / "engine.db"),
                ]
            )

        assert result == 0

        # Verify trigger was created in DB
        db = Database(db_path=tmp_path / "engine.db")
        row = db.get_trigger("regression-ci-trigger")
        assert row is not None
        assert row["mode"] == "fire_and_forget"
        # Secret should be set
        assert row.get("secret") is not None

    def test_main_idempotent_on_existing_trigger(self, tmp_path):
        """Running main() twice doesn't raise — the second run warns and skips."""
        import importlib.util
        script_dir = Path(__file__).parent.parent / "scripts"
        spec_obj = importlib.util.spec_from_file_location(
            "register_webhook",
            str(script_dir / "register_webhook.py"),
        )
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"id": 99999, "active": True})

        db_path = str(tmp_path / "engine.db")
        args = ["--url", "https://example.com/hook", "--db", db_path]

        with patch("subprocess.run", return_value=mock_result):
            result1 = module.main(args)

        with patch("subprocess.run", return_value=mock_result):
            result2 = module.main(args)

        # Both runs should succeed (first creates trigger, second skips)
        assert result1 == 0
        assert result2 == 0
