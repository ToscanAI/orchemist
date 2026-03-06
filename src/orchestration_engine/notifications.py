"""Notification dispatcher for HITL review queue events (Issue #331.4).

Dispatches human-readable alerts when a pipeline run enters ``pending_review``
status.  Supports an extensible set of backends:

- **log** (always active) — structured Python logging at WARNING level.
- **openclaw** — posts a message to the OpenClaw gateway session.
- **webhook** — HTTP POST to a configurable URL with a JSON payload.

All backends are non-fatal: exceptions are caught and logged so that a
misconfigured notification channel never blocks pipeline execution.

Configuration is driven entirely by environment variables so the dispatcher
can be instantiated without touching application config files::

    NOTIFY_OPENCLAW_ENABLED=1
    NOTIFY_OPENCLAW_GATEWAY_URL=http://localhost:18789
    NOTIFY_OPENCLAW_GATEWAY_TOKEN=<token>
    NOTIFY_OPENCLAW_SESSION=agent:main:main

    NOTIFY_WEBHOOK_ENABLED=1
    NOTIFY_WEBHOOK_URL=https://hooks.example.com/review-queue
    NOTIFY_WEBHOOK_SECRET=<optional-hmac-secret>

Usage::

    from orchestration_engine.notifications import NotificationDispatcher

    dispatcher = NotificationDispatcher.from_env()
    dispatcher.dispatch(
        event="human_review",
        run_id="abc12345",
        tier="needs_review",
        score=0.62,
        justification="Test coverage below threshold",
    )
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Dispatch HITL review notifications to one or more backends.

    Instantiate via :meth:`from_env` to read configuration from environment
    variables, or supply the config dict directly for testing.

    Args:
        config: Dict with optional keys:
            - ``openclaw_enabled`` (bool): Enable OpenClaw session notifications.
            - ``openclaw_gateway_url`` (str): Gateway base URL.
            - ``openclaw_gateway_token`` (str): Gateway auth token.
            - ``openclaw_session`` (str): Target session ID/name.
            - ``webhook_enabled`` (bool): Enable outbound HTTP webhook.
            - ``webhook_url`` (str): Webhook destination URL.
            - ``webhook_secret`` (str, optional): HMAC-SHA256 signing secret.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "NotificationDispatcher":
        """Create a dispatcher configured from environment variables.

        Reads the following env vars (all optional):

        OpenClaw backend:
            ``NOTIFY_OPENCLAW_ENABLED``       — ``"1"`` / ``"true"`` to enable.
            ``NOTIFY_OPENCLAW_GATEWAY_URL``   — Gateway base URL.
            ``NOTIFY_OPENCLAW_GATEWAY_TOKEN`` — Auth token.
            ``NOTIFY_OPENCLAW_SESSION``       — Target session (default
                                               ``"agent:main:main"``).

        Webhook backend:
            ``NOTIFY_WEBHOOK_ENABLED`` — ``"1"`` / ``"true"`` to enable.
            ``NOTIFY_WEBHOOK_URL``     — Destination URL.
            ``NOTIFY_WEBHOOK_SECRET``  — Optional HMAC signing secret.

        Returns:
            A configured :class:`NotificationDispatcher` instance.
        """
        def _truthy(val: Optional[str]) -> bool:
            return val is not None and val.lower() in {"1", "true", "yes"}

        config: Dict[str, Any] = {
            # OpenClaw backend
            "openclaw_enabled": _truthy(os.environ.get("NOTIFY_OPENCLAW_ENABLED")),
            "openclaw_gateway_url": (
                os.environ.get("NOTIFY_OPENCLAW_GATEWAY_URL")
                or os.environ.get("OPENCLAW_GATEWAY_URL", "")
            ),
            "openclaw_gateway_token": (
                os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN")
                or os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
            ),
            "openclaw_session": os.environ.get(
                "NOTIFY_OPENCLAW_SESSION", "agent:main:main"
            ),
            # Webhook backend
            "webhook_enabled": _truthy(os.environ.get("NOTIFY_WEBHOOK_ENABLED")),
            "webhook_url": os.environ.get("NOTIFY_WEBHOOK_URL", ""),
            "webhook_secret": os.environ.get("NOTIFY_WEBHOOK_SECRET", ""),
        }
        return cls(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(
        self,
        event: str,
        run_id: str,
        tier: str = "",
        score: float = 0.0,
        justification: str = "",
        **extra: Any,
    ) -> None:
        """Dispatch a notification to all configured backends.

        Always emits a structured log message at WARNING level regardless of
        which backends are configured.  Additional backends (OpenClaw, webhook)
        are invoked if enabled; their failures are caught and logged.

        Args:
            event:         Event name (e.g. ``"human_review"``).
            run_id:        Pipeline run identifier.
            tier:          Routing tier name (e.g. ``"needs_review"``).
            score:         Composite confidence score (0.0 – 1.0).
            justification: Human-readable explanation from the routing engine.
            **extra:       Additional key-value pairs included in webhook payload.
        """
        payload: Dict[str, Any] = {
            "event": event,
            "run_id": run_id,
            "tier": tier,
            "score": round(score, 4),
            "justification": justification,
            **extra,
        }

        # Always log
        logger.warning(
            "HITL notification [%s]: run_id=%s tier=%s score=%.4f — %s",
            event,
            run_id,
            tier,
            score,
            justification or "(no justification)",
        )

        if self._config.get("openclaw_enabled"):
            self._dispatch_openclaw(payload)

        if self._config.get("webhook_enabled"):
            self._dispatch_webhook(payload)

    # ------------------------------------------------------------------
    # Backends (private)
    # ------------------------------------------------------------------

    def _dispatch_openclaw(self, payload: Dict[str, Any]) -> None:
        """Send a message to an OpenClaw gateway session.

        Constructs a human-readable alert string and posts it to the
        ``/api/sessions/{session}/messages`` endpoint of the gateway.

        Args:
            payload: Notification payload dict from :meth:`dispatch`.
        """
        try:
            gateway_url = self._config.get("openclaw_gateway_url", "").rstrip("/")
            token = self._config.get("openclaw_gateway_token", "")
            session = self._config.get("openclaw_session", "agent:main:main")

            if not gateway_url:
                logger.debug("OpenClaw notification skipped: no gateway URL configured.")
                return

            run_id = payload.get("run_id", "?")
            score = payload.get("score", 0.0)
            tier = payload.get("tier", "?")
            justification = payload.get("justification", "")

            message = (
                f"🔔 **HITL Review Required**\n"
                f"Run `{run_id}` has entered `pending_review` status.\n"
                f"• Tier: `{tier}` | Score: `{score:.4f}`\n"
                f"• Reason: {justification or '(none)'}\n"
                f"Use `orch review list` or the API to action this run."
            )

            body = json.dumps({"message": message}).encode("utf-8")
            url = f"{gateway_url}/api/sessions/{session}/messages"
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.getcode()
                logger.debug(
                    "OpenClaw notification delivered: run_id=%s status=%s",
                    run_id,
                    status,
                )
        except Exception as exc:
            logger.warning("OpenClaw notification failed (non-fatal): %s", exc)

    def _dispatch_webhook(self, payload: Dict[str, Any]) -> None:
        """HTTP POST the notification payload to a configured URL.

        When a ``webhook_secret`` is configured, adds an
        ``X-Orch-Signature-256: sha256=<hex>`` header using HMAC-SHA256 so
        the receiver can verify authenticity.

        Args:
            payload: Notification payload dict from :meth:`dispatch`.
        """
        try:
            url = self._config.get("webhook_url", "")
            secret = self._config.get("webhook_secret", "")

            if not url:
                logger.debug("Webhook notification skipped: no URL configured.")
                return

            body = json.dumps(payload).encode("utf-8")
            headers: Dict[str, str] = {"Content-Type": "application/json"}

            if secret:
                sig = hmac.new(
                    secret.encode("utf-8"),
                    body,
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Orch-Signature-256"] = f"sha256={sig}"

            req = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                logger.debug(
                    "Webhook notification delivered: run_id=%s status=%s",
                    payload.get("run_id", "?"),
                    status,
                )
        except Exception as exc:
            logger.warning("Webhook notification failed (non-fatal): %s", exc)
