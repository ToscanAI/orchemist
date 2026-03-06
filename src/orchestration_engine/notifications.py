"""Notification subsystem for the Orchestration Engine (Issue #331.4).

Provides a pluggable notification architecture for dispatching pipeline
lifecycle events (e.g. ``human_review``) to one or more notifier backends.

Usage::

    from orchestration_engine.notifications import NotificationDispatcher

    dispatcher = NotificationDispatcher.from_env()
    dispatcher.dispatch(event="human_review", run_id="abc123", tier="review", score=0.72)

Environment variables:
    ORCH_WEBHOOK_URL: When set, a WebhookNotifier is automatically added
        alongside the always-present LogNotifier.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, List
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


class BaseNotifier(ABC):
    """Abstract base class for event notifiers."""

    @abstractmethod
    def dispatch(self, event: str, run_id: str, **kwargs: Any) -> None:
        """Dispatch an event notification."""


class LogNotifier(BaseNotifier):
    """Notifier that logs events via the Python logging infrastructure."""

    _logger: logging.Logger = logging.getLogger("orchestration_engine.notifications")

    def dispatch(self, event: str, run_id: str, **kwargs: Any) -> None:
        extra = "  ".join(f"{k}={v}" for k, v in kwargs.items())
        self._logger.info(
            "Event '%s' for run '%s'%s",
            event,
            run_id,
            f"  {extra}" if extra else "",
        )


class WebhookNotifier(BaseNotifier):
    """Notifier that POSTs a JSON payload to a configured webhook URL."""

    def __init__(self, url: str, timeout: int = 10) -> None:
        self.url = url
        self.timeout = timeout

    def dispatch(self, event: str, run_id: str, **kwargs: Any) -> None:
        payload = {"event": event, "run_id": run_id, **kwargs}
        data = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            _ = resp.read()


class NotificationDispatcher:
    """Dispatches pipeline lifecycle events to configured backends.

    Supports two optional backends (openclaw, webhook) controlled via a
    ``_config`` dict.  Always emits a WARNING-level log entry regardless of
    which backends are enabled.

    Usage::

        # From environment variables
        d = NotificationDispatcher.from_env()
        d.dispatch(event="human_review", run_id="abc123", score=0.72)

        # From explicit config
        d = NotificationDispatcher({"openclaw_enabled": True, ...})
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config: dict = {
            "openclaw_enabled": False,
            "openclaw_gateway_url": "",
            "openclaw_gateway_token": "",
            "openclaw_session": "agent:main:main",
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_secret": "",
        }
        if config:
            self._config.update(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(self, event: str, run_id: str, **kwargs: Any) -> None:
        """Dispatch *event* for *run_id* to all enabled backends.

        Always logs at WARNING level.  Per-backend exceptions are caught and
        logged so that a broken backend never aborts the caller.
        """
        extra = "  ".join(f"{k}={v}" for k, v in kwargs.items())
        logger.warning(
            "Notification event='%s' run_id='%s'%s",
            event,
            run_id,
            f"  {extra}" if extra else "",
        )

        if self._config.get("openclaw_enabled"):
            try:
                self._dispatch_openclaw(event=event, run_id=run_id, **kwargs)
            except Exception as exc:
                logger.warning("OpenClaw notification failed (swallowed): %s", exc)

        if self._config.get("webhook_enabled"):
            try:
                self._dispatch_webhook(event=event, run_id=run_id, **kwargs)
            except Exception as exc:
                logger.warning("Webhook notification failed (swallowed): %s", exc)

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _dispatch_openclaw(self, event: str, run_id: str, **kwargs: Any) -> None:
        """POST a message to the OpenClaw gateway sessions_send endpoint."""
        import hmac as _hmac
        gateway_url = self._config.get("openclaw_gateway_url", "").rstrip("/")
        token = self._config.get("openclaw_gateway_token", "")
        session = self._config.get("openclaw_session", "agent:main:main")

        extra = "  ".join(f"{k}={v}" for k, v in kwargs.items())
        text = (
            f"🔔 **Review Required** — event=`{event}` run_id=`{run_id}`"
            + (f"\n{extra}" if extra else "")
        )
        payload = json.dumps(
            {"session": session, "message": text}
        ).encode("utf-8")

        url = f"{gateway_url}/api/v1/sessions/send"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()

    def _dispatch_webhook(self, event: str, run_id: str, **kwargs: Any) -> None:
        """POST a JSON payload to the configured webhook URL."""
        import hashlib as _hashlib
        import hmac as _hmac

        url = self._config.get("webhook_url", "")
        secret = self._config.get("webhook_secret", "")

        payload = {"event": event, "run_id": run_id, **kwargs}
        data = json.dumps(payload, default=str).encode("utf-8")

        headers: dict = {"Content-Type": "application/json"}
        if secret:
            sig = _hmac.new(secret.encode(), data, _hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={sig}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "NotificationDispatcher":
        """Build a dispatcher from environment variables.

        Environment variables:
            NOTIFY_OPENCLAW_ENABLED       — "1", "true", "yes" to enable
            NOTIFY_OPENCLAW_GATEWAY_URL   — gateway base URL (falls back to OPENCLAW_GATEWAY_URL)
            NOTIFY_OPENCLAW_GATEWAY_TOKEN — bearer token
            NOTIFY_OPENCLAW_SESSION       — target session (default: agent:main:main)
            NOTIFY_WEBHOOK_ENABLED        — "1", "true", "yes" to enable
            NOTIFY_WEBHOOK_URL            — https://… webhook endpoint
            NOTIFY_WEBHOOK_SECRET         — HMAC signing secret
        """

        def _bool(val: str | None) -> bool:
            return str(val or "").strip().lower() in ("1", "true", "yes")

        openclaw_gateway_url = os.environ.get(
            "NOTIFY_OPENCLAW_GATEWAY_URL",
            os.environ.get("OPENCLAW_GATEWAY_URL", ""),
        )

        webhook_url = os.environ.get("NOTIFY_WEBHOOK_URL", "")
        if webhook_url and not webhook_url.startswith(("http://", "https://")):
            logger.warning(
                "NOTIFY_WEBHOOK_URL must start with http(s)://, ignoring: %s",
                webhook_url,
            )
            webhook_url = ""

        config = {
            "openclaw_enabled": _bool(os.environ.get("NOTIFY_OPENCLAW_ENABLED")),
            "openclaw_gateway_url": openclaw_gateway_url,
            "openclaw_gateway_token": os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN", ""),
            "openclaw_session": os.environ.get("NOTIFY_OPENCLAW_SESSION", "agent:main:main"),
            "webhook_enabled": _bool(os.environ.get("NOTIFY_WEBHOOK_ENABLED")),
            "webhook_url": webhook_url,
            "webhook_secret": os.environ.get("NOTIFY_WEBHOOK_SECRET", ""),
        }
        return cls(config)
