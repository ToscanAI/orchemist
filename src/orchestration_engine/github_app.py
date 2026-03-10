"""GitHub App authentication module for the Orchestration Engine (Issue #510).

Provides JWT generation and installation token exchange for GitHub App-based
authentication, as well as webhook signature verification.

This module is self-contained: it has zero runtime imports from the rest of the
engine, so it can be used and tested in isolation without risk of circular imports.

Optional dependency: ``PyJWT[cryptography]>=2.8.0`` (the ``github`` extra) is
required only by :meth:`GitHubApp.generate_jwt`.  All other functionality works
with the Python standard library alone.

Usage::

    from orchestration_engine.github_app import GitHubApp, GitHubAppAuthError

    app = GitHubApp(app_id=12345, private_key="-----BEGIN RSA PRIVATE KEY-----\\n...")
    jwt = app.generate_jwt()
    token_data = app.get_installation_token(installation_id=67890, jwt=jwt)
    access_token = token_data["token"]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["GitHubApp", "GitHubAppAuthError"]


class GitHubAppAuthError(Exception):
    """Raised when GitHub App authentication fails (e.g. non-2xx from GitHub API)."""


class GitHubApp:
    """GitHub App authentication: JWT generation + installation token exchange.

    Construct via :meth:`from_config` (reads PEM from disk) or directly by
    passing the PEM content string as *private_key*.

    All network calls are made lazily — nothing happens at instantiation time.
    """

    def __init__(self, app_id: int, private_key: str) -> None:
        """Initialise a GitHubApp authenticator.

        Args:
            app_id: GitHub App numeric ID (shown on the App settings page).
            private_key: PEM-encoded RSA private key *content* (not a file path).
        """
        self._app_id = app_id
        self._private_key = private_key

    # ------------------------------------------------------------------
    # Class-method constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Any) -> "GitHubApp":
        """Construct a :class:`GitHubApp` from a :class:`~orchestration_engine.config.GitHubAppConfig`.

        Reads the PEM private key from disk at ``cfg.private_key_path``.

        Args:
            cfg: A ``GitHubAppConfig`` instance (or any object with ``app_id``
                 and ``private_key_path`` attributes).

        Returns:
            Configured :class:`GitHubApp` instance.

        Raises:
            ValueError: When ``cfg.private_key_path`` is ``None``.
            FileNotFoundError: When the key file does not exist on disk.
        """
        if cfg.private_key_path is None:
            raise ValueError(
                "GitHubAppConfig.private_key_path is not set. "
                "Configure it in [github_app] → private_key_path."
            )
        key_path = Path(cfg.private_key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"GitHub App private key not found: {key_path}. "
                "Generate a private key in the GitHub App settings and save it there."
            )
        private_key = key_path.read_text(encoding="utf-8")
        return cls(app_id=cfg.app_id, private_key=private_key)

    # ------------------------------------------------------------------
    # JWT generation
    # ------------------------------------------------------------------

    def generate_jwt(self, expiry_seconds: int = 600) -> str:
        """Generate a signed RS256 JWT for authenticating as the GitHub App.

        The JWT payload follows GitHub's specification:

        * ``iat`` — issued-at: ``now - 60`` (60-second clock-skew tolerance)
        * ``exp`` — expiry: ``now + expiry_seconds`` (max 600 s per GitHub docs)
        * ``iss`` — issuer: ``str(app_id)``

        Args:
            expiry_seconds: Token lifetime in seconds (default 600, GitHub max).

        Returns:
            Encoded JWT string (``str`` — PyJWT ≥ 2.x always returns ``str``).

        Raises:
            RuntimeError: When ``PyJWT`` (with the ``cryptography`` backend) is
                not installed.  Install with:
                ``pip install 'orchestration-engine[github]'``
        """
        try:
            import jwt as pyjwt  # PyJWT ≥ 2.x
        except ImportError as exc:
            raise RuntimeError(
                "PyJWT is required for GitHub App JWT generation. "
                "Install with: pip install 'orchestration-engine[github]'"
            ) from exc

        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued-at with clock-skew tolerance
            "exp": now + expiry_seconds,
            "iss": str(self._app_id),
        }
        return pyjwt.encode(payload, self._private_key, algorithm="RS256")

    # ------------------------------------------------------------------
    # Installation token exchange
    # ------------------------------------------------------------------

    def get_installation_token(
        self,
        installation_id: int,
        jwt: Optional[str] = None,
        http_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Exchange a JWT for a GitHub App installation access token.

        Calls ``POST https://api.github.com/app/installations/{installation_id}/access_tokens``.

        Args:
            installation_id: GitHub App installation ID (unique per org/user that
                installed the app).
            jwt: Pre-generated JWT string.  When ``None``, :meth:`generate_jwt`
                is called internally.
            http_client: Optional injected HTTP client for testing.  Must expose
                ``.post(url, headers) → response`` where the response has
                ``.status_code`` (int) and ``.json()`` (callable returning dict).
                When ``None``, the Python standard-library ``urllib.request`` is
                used — no third-party dependencies required.

        Returns:
            Dict with at least ``{"token": str, "expires_at": str}`` as returned
            by the GitHub API.

        Raises:
            GitHubAppAuthError: When GitHub responds with a non-2xx status code.
        """
        if jwt is None:
            jwt = self.generate_jwt()

        url = (
            f"https://api.github.com/app/installations/"
            f"{installation_id}/access_tokens"
        )
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        if http_client is not None:
            # Injected client (used in tests / alternative HTTP libraries)
            resp = http_client.post(url, headers=headers)
            if resp.status_code not in (200, 201):
                raise GitHubAppAuthError(
                    f"Token exchange failed: HTTP {resp.status_code}"
                )
            return resp.json()

        # Default path: stdlib urllib — no third-party HTTP library required
        req = urllib.request.Request(url, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise GitHubAppAuthError(
                f"Token exchange failed: HTTP {exc.code}"
            ) from exc

    # ------------------------------------------------------------------
    # Webhook signature verification (static — no instance state needed)
    # ------------------------------------------------------------------

    @staticmethod
    def verify_webhook_signature(
        secret: str,
        payload_bytes: bytes,
        sig_header: Optional[str],
    ) -> bool:
        """Verify an HMAC-SHA256 GitHub webhook signature.

        GitHub sends ``X-Hub-Signature-256: sha256=<hex_digest>``.  This method
        recomputes the HMAC-SHA256 of *payload_bytes* using *secret* and compares
        it to the digest in *sig_header* using a constant-time comparison to
        prevent timing attacks.

        This is a self-contained duplicate of the ``_verify_github_signature``
        helper in ``web/api.py``, kept here so this module has no dependency on
        the FastAPI application code.

        Args:
            secret: Shared HMAC secret string.
            payload_bytes: Raw request body bytes.
            sig_header: Value of the ``X-Hub-Signature-256`` header
                (e.g. ``"sha256=abc123..."``).  May be ``None``.

        Returns:
            ``True`` when the signature is valid, ``False`` otherwise
            (including when *sig_header* is ``None`` or malformed).
        """
        if not sig_header:
            return False
        if not sig_header.startswith("sha256="):
            return False
        expected = sig_header[len("sha256="):]
        computed = hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, expected)
