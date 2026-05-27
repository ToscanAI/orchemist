"""Tests for GitHubApp — Issue #510.

6 groups:
  1. verify_webhook_signature — static method, stdlib only
  2. GitHubApp construction and from_config factory
  3. generate_jwt — raises RuntimeError when PyJWT missing
  4. get_installation_token — HTTP injection seam
  5. GitHubAppConfig field_validator (private_key_path ~ expansion)
  6. web/api.py handle_github_issues signature verification (opt-in)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Group 1: verify_webhook_signature
# ---------------------------------------------------------------------------

class TestVerifyWebhookSignature:
    """Tests for GitHubApp.verify_webhook_signature (static, stdlib only)."""

    def _sign(self, secret: str, body: bytes) -> str:
        mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    def test_valid_signature(self):
        from orchestration_engine.github_app import GitHubApp

        body = b'{"action": "opened"}'
        secret = "my-secret"
        sig = self._sign(secret, body)
        assert GitHubApp.verify_webhook_signature(secret, body, sig) is True

    def test_invalid_signature(self):
        from orchestration_engine.github_app import GitHubApp

        body = b'{"action": "opened"}'
        assert GitHubApp.verify_webhook_signature("secret", body, "sha256=deadbeef") is False

    def test_missing_header_returns_false(self):
        from orchestration_engine.github_app import GitHubApp

        assert GitHubApp.verify_webhook_signature("secret", b"body", None) is False

    def test_malformed_header_returns_false(self):
        from orchestration_engine.github_app import GitHubApp

        assert GitHubApp.verify_webhook_signature("secret", b"body", "md5=abc") is False

    def test_wrong_secret_returns_false(self):
        from orchestration_engine.github_app import GitHubApp

        body = b"payload"
        sig = self._sign("correct-secret", body)
        assert GitHubApp.verify_webhook_signature("wrong-secret", body, sig) is False

    def test_empty_body_valid_signature(self):
        from orchestration_engine.github_app import GitHubApp

        body = b""
        secret = "s"
        sig = self._sign(secret, body)
        assert GitHubApp.verify_webhook_signature(secret, body, sig) is True


# ---------------------------------------------------------------------------
# Group 2: GitHubApp construction and from_config factory
# ---------------------------------------------------------------------------

class TestGitHubAppConstruction:
    """Tests for GitHubApp __init__ and from_config."""

    def test_basic_construction(self):
        from orchestration_engine.github_app import GitHubApp

        app = GitHubApp(app_id=42, private_key="-----BEGIN RSA PRIVATE KEY-----\n...")
        assert app._app_id == 42
        assert "BEGIN RSA" in app._private_key

    def test_from_config_reads_key_file(self, tmp_path):
        from orchestration_engine.github_app import GitHubApp
        from orchestration_engine.config import GitHubAppConfig

        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n")

        cfg = GitHubAppConfig(
            app_id=7,
            private_key_path=str(key_file),
            webhook_secret="ws",
            installation_id=22,
        )
        app = GitHubApp.from_config(cfg)
        assert app._app_id == 7
        assert "FAKE" in app._private_key

    def test_from_config_missing_key_path_raises(self):
        from orchestration_engine.github_app import GitHubApp
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig(app_id=1)
        with pytest.raises(ValueError, match="private_key_path"):
            GitHubApp.from_config(cfg)

    def test_from_config_nonexistent_key_file_raises(self):
        from orchestration_engine.github_app import GitHubApp
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig(app_id=1, private_key_path="/nonexistent/key.pem")
        with pytest.raises(FileNotFoundError):
            GitHubApp.from_config(cfg)

    def test_from_config_tilde_path_expanded(self, tmp_path):
        """Config validator expands ~ before from_config reads the path."""
        from orchestration_engine.config import GitHubAppConfig

        # Just verify that the validator expands ~ in the config
        cfg = GitHubAppConfig(private_key_path="~/some/path.pem")
        assert cfg.private_key_path is not None
        assert not cfg.private_key_path.startswith("~")


# ---------------------------------------------------------------------------
# Group 3: generate_jwt
# ---------------------------------------------------------------------------

class TestGenerateJwt:
    def test_raises_when_pyjwt_missing(self):
        """Simulate missing PyJWT by injecting None sentinel into sys.modules."""
        import sys
        from orchestration_engine.github_app import GitHubApp

        app = GitHubApp(app_id=1, private_key="fake")
        # Setting sys.modules["jwt"] = None causes `import jwt` to raise ImportError
        original = sys.modules.get("jwt", _UNSET := object())
        sys.modules["jwt"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="PyJWT"):
                app.generate_jwt()
        finally:
            if original is _UNSET:
                del sys.modules["jwt"]
            else:
                sys.modules["jwt"] = original

    def test_generate_jwt_success(self, tmp_path):
        """Integration test — only runs when PyJWT[cryptography] installed."""
        try:
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.backends import default_backend
        except ImportError:
            pytest.skip("PyJWT[cryptography] not installed")

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        from orchestration_engine.github_app import GitHubApp

        app = GitHubApp(app_id=123, private_key=pem)
        token = app.generate_jwt()
        assert isinstance(token, str)
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["iss"] == "123"


# ---------------------------------------------------------------------------
# Group 4: get_installation_token — HTTP injection seam
# ---------------------------------------------------------------------------

class TestGetInstallationToken:
    """Tests for get_installation_token with injected http_client."""

    def _make_fake_jwt_app(self, tmp_path=None):
        """Build a GitHubApp with a patched generate_jwt."""
        from orchestration_engine.github_app import GitHubApp

        app = GitHubApp(app_id=1, private_key="fake")
        app.generate_jwt = lambda expiry_seconds=600: "fake.jwt.token"
        return app

    def _make_http_client(self, token: str, status: int = 201):
        class FakeResponse:
            def __init__(self):
                self.status_code = status

            def json(self):
                return {"token": token, "expires_at": "2099-01-01T00:00:00Z"}

        class FakeClient:
            def post(self, url, headers):
                return FakeResponse()

        return FakeClient()

    def test_returns_token(self):
        app = self._make_fake_jwt_app()
        client = self._make_http_client("ghs_test_token")
        result = app.get_installation_token(installation_id=99, http_client=client)
        assert result["token"] == "ghs_test_token"

    def test_raises_on_non_2xx(self):
        from orchestration_engine.github_app import GitHubAppAuthError

        app = self._make_fake_jwt_app()
        client = self._make_http_client("", status=401)
        with pytest.raises(GitHubAppAuthError, match="401"):
            app.get_installation_token(installation_id=5, http_client=client)

    def test_uses_provided_jwt(self):
        """When jwt arg supplied, generate_jwt is NOT called."""
        calls = []

        from orchestration_engine.github_app import GitHubApp

        app = GitHubApp(app_id=1, private_key="fake")
        original_generate = app.generate_jwt

        def patched_generate(**kwargs):
            calls.append("called")
            return original_generate(**kwargs)

        app.generate_jwt = patched_generate

        class FakeResp:
            status_code = 201

            def json(self):
                return {"token": "tok", "expires_at": "2099"}

        class FakeClient:
            def post(self, url, headers):
                return FakeResp()

        app.get_installation_token(installation_id=1, jwt="pre.built.jwt", http_client=FakeClient())
        assert calls == []  # generate_jwt was not called


# ---------------------------------------------------------------------------
# Group 5: GitHubAppConfig field_validator
# ---------------------------------------------------------------------------

class TestGitHubAppConfig:
    def test_private_key_path_expanded(self):
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig(private_key_path="~/some/path.pem")
        assert cfg.private_key_path is not None
        assert not cfg.private_key_path.startswith("~")
        assert cfg.private_key_path.endswith("some/path.pem")

    def test_none_private_key_path(self):
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig(private_key_path=None)
        assert cfg.private_key_path is None

    def test_absolute_path_unchanged(self, tmp_path):
        from orchestration_engine.config import GitHubAppConfig

        path = str(tmp_path / "key.pem")
        cfg = GitHubAppConfig(private_key_path=path)
        assert cfg.private_key_path == path

    def test_defaults_all_none(self):
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig()
        assert cfg.app_id is None
        assert cfg.webhook_secret is None
        assert cfg.installation_id is None

    def test_full_config(self):
        from orchestration_engine.config import GitHubAppConfig

        cfg = GitHubAppConfig(
            app_id=42,
            webhook_secret="s3cr3t",
            installation_id=99,
        )
        assert cfg.app_id == 42
        assert cfg.webhook_secret == "s3cr3t"
        assert cfg.installation_id == 99


# ---------------------------------------------------------------------------
# Group 6: web/api.py handle_github_issues — signature verification (opt-in)
# ---------------------------------------------------------------------------

class TestHandleGithubIssuesSignatureVerification:
    """Opt-in webhook signature check in handle_github_issues."""

    def _build_payload(self, action: str = "opened", label: str = "orchemist") -> dict:
        return {
            "action": action,
            "issue": {
                "number": 1,
                "title": "Test issue",
                "body": "body",
                "labels": [{"name": label}],
            },
            "repository": {"full_name": "owner/repo"},
        }

    def _sign(self, secret: str, body: bytes) -> str:
        mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    def _get_client(self, tmp_path):
        from orchestration_engine.web.api import create_api_app
        app = create_api_app(db_path=str(tmp_path / "test.db"))
        return TestClient(app, raise_server_exceptions=False)

    def test_no_secret_configured_accepts_request(self, tmp_path):
        """When no webhook_secret configured, request passes with a warning."""
        from orchestration_engine.config import EngineConfig

        payload = self._build_payload()
        body = json.dumps(payload).encode()
        mock_config = EngineConfig(github_app=None)

        with patch("orchestration_engine.config.get_global_config", return_value=mock_config):
            client = self._get_client(tmp_path)
            resp = client.post(
                "/api/v1/github/issues",
                content=body,
                headers={"X-GitHub-Event": "issues", "Content-Type": "application/json"},
            )
        assert resp.status_code != 403

    def test_valid_signature_accepted(self, tmp_path):
        """When secret configured and signature valid → not 401."""
        from orchestration_engine.config import EngineConfig, GitHubAppConfig

        secret = "test-secret"
        payload = self._build_payload()
        body = json.dumps(payload).encode()
        sig = self._sign(secret, body)
        mock_config = EngineConfig(github_app=GitHubAppConfig(webhook_secret=secret))

        with patch("orchestration_engine.config.get_global_config", return_value=mock_config):
            client = self._get_client(tmp_path)
            resp = client.post(
                "/api/v1/github/issues",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                },
            )
        assert resp.status_code != 403

    def test_invalid_signature_rejected(self, tmp_path):
        """When secret configured and signature invalid → 401."""
        from orchestration_engine.config import EngineConfig, GitHubAppConfig

        secret = "test-secret"
        payload = self._build_payload()
        body = json.dumps(payload).encode()
        mock_config = EngineConfig(github_app=GitHubAppConfig(webhook_secret=secret))

        with patch("orchestration_engine.config.get_global_config", return_value=mock_config):
            client = self._get_client(tmp_path)
            resp = client.post(
                "/api/v1/github/issues",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": "sha256=invalidsignature",
                },
            )
        assert resp.status_code == 403

    def test_missing_signature_rejected_when_secret_configured(self, tmp_path):
        """When secret configured but no sig header → 401."""
        from orchestration_engine.config import EngineConfig, GitHubAppConfig

        mock_config = EngineConfig(github_app=GitHubAppConfig(webhook_secret="sec"))

        with patch("orchestration_engine.config.get_global_config", return_value=mock_config):
            client = self._get_client(tmp_path)
            resp = client.post(
                "/api/v1/github/issues",
                content=b'{"action":"opened"}',
                headers={"X-GitHub-Event": "issues", "Content-Type": "application/json"},
            )
        assert resp.status_code == 403
