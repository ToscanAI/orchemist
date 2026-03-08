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
from datetime import datetime
from typing import Any, List, Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


def _is_quiet_hours(
    tz: str = "Europe/Vienna",
    quiet_start: int = 23,
    quiet_end: int = 8,
) -> bool:
    """Return True if the current local hour falls within the quiet window.

    The quiet window spans from *quiet_start* (inclusive, 0–23) through to
    *quiet_end* (exclusive, 0–23).  The default window is 23:00–08:00 Vienna
    time — i.e. ``hour >= 23 or hour < 8``.

    Args:
        tz:          IANA timezone name.  Requires Python ≥3.9 ``zoneinfo`` or
                     ``pytz`` as a fallback.
        quiet_start: First hour of the quiet window (default 23).
        quiet_end:   First hour *after* the quiet window ends (default 8).

    Returns:
        ``True`` when notifications should be suppressed; ``False`` otherwise.
    """
    try:
        from zoneinfo import ZoneInfo
        local_now = datetime.now(tz=ZoneInfo(tz))
    except ImportError:
        try:
            import pytz  # type: ignore
            local_now = datetime.now(tz=pytz.timezone(tz))
        except ImportError:
            # Cannot determine timezone — default to allow notifications
            return False
    hour = local_now.hour
    if quiet_start > quiet_end:
        # Wraps midnight: e.g. 23–8 → quiet when hour>=23 or hour<8
        return hour >= quiet_start or hour < quiet_end
    else:
        return quiet_start <= hour < quiet_end


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


class TelegramNotifier(BaseNotifier):
    """Notifier that sends a message to a Telegram chat via the Bot API.

    Requires a bot token (``NOTIFY_TELEGRAM_BOT_TOKEN``) and a chat ID
    (``NOTIFY_TELEGRAM_CHAT_ID``).  Messages are sent as Markdown via the
    ``sendMessage`` endpoint and optionally include an inline keyboard for
    ``human_review`` events.

    Args:
        bot_token: Telegram Bot API token (``123456:ABC-DEF...``).
        chat_id:   Target chat or channel ID (negative for groups/channels).
        timeout:   HTTP request timeout in seconds.  Default ``10``.
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def dispatch(
        self,
        event: str,
        run_id: str,
        inline_keyboard: Optional[List[List[dict]]] = None,
        **kwargs: Any,
    ) -> None:
        """Send a Telegram message for the given *event*.

        For ``human_review`` events the message includes run context (issue
        number, score, confidence, summary) and an optional *inline_keyboard*
        with [Approve], [Reject], and [View PR] buttons.

        Args:
            event:           Pipeline lifecycle event name.
            run_id:          Pipeline run identifier.
            inline_keyboard: Optional list-of-lists of Telegram
                             ``InlineKeyboardButton`` dicts.  When provided the
                             ``reply_markup`` field is added to the API payload.
            **kwargs:        Additional context (score, tier, issue_number, …).
        """
        event_emoji = {
            "auto_merge": "✅",
            "human_review": "👀",
            "reject": "❌",
        }.get(event, "🔔")

        if event == "human_review":
            issue_number = kwargs.pop("issue_number", None)
            summary = kwargs.pop("summary", "")
            confidence = kwargs.pop("confidence", "")
            score = kwargs.pop("score", None)
            tier = kwargs.pop("tier", "")
            pr_url = kwargs.pop("pr_url", "")
            justification = kwargs.pop("justification", "")

            lines = [
                f"{event_emoji} *Orchestration Engine* — `human_review`",
                f"run\\_id: `{run_id}`",
            ]
            if issue_number:
                lines.append(f"issue: *#{issue_number}*")
            if tier:
                lines.append(f"tier: `{tier}`")
            if score is not None:
                lines.append(f"score: `{score:.4f}`")
            if confidence:
                lines.append(f"confidence: `{confidence}`")
            if summary:
                safe_summary = summary.replace("`", "'")[:120]
                lines.append(f"summary: _{safe_summary}_")
            if justification:
                safe_just = justification.replace("`", "'")[:200]
                lines.append(f"justification: {safe_just}")
            # Remaining extra kwargs
            for k, v in kwargs.items():
                lines.append(f"  {k}: {v}")
            text = "\n".join(lines)
        else:
            extra_lines = "\n".join(f"  {k}: {v}" for k, v in kwargs.items())
            text = (
                f"{event_emoji} *Orchestration Engine* — `{event}`\n"
                f"run\\_id: `{run_id}`"
            )
            if extra_lines:
                text += f"\n{extra_lines}"

        url = self.BASE_URL.format(token=self.bot_token)
        message_payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if inline_keyboard is not None:
            message_payload["reply_markup"] = {
                "inline_keyboard": inline_keyboard,
            }

        payload = json.dumps(message_payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            _ = resp.read()


class TelegramCallbackHandler:
    """Handle Telegram inline keyboard callback queries for HITL review actions.

    When a user taps [Approve] or [Reject] on the Telegram notification the Bot
    API fires a ``callback_query`` update.  This class parses the
    ``callback_data`` field (format ``"approve:<run_id>"`` or
    ``"reject:<run_id>"``), updates the pipeline run status via the local
    :class:`~orchestration_engine.db.Database`, and optionally sends a
    confirmation message back to the Telegram chat and the OpenClaw session.

    Args:
        db_path:       Path to the SQLite database file.
        gateway_url:   OpenClaw gateway base URL (for session confirmation).
        gateway_token: OpenClaw bearer token.
        bot_token:     Telegram Bot API token used to answer callback queries.
        chat_id:       Telegram chat ID to send confirmation messages.
    """

    def __init__(
        self,
        db_path: str,
        gateway_url: str,
        gateway_token: str,
        bot_token: str,
        chat_id: str,
        timeout: int = 10,
    ) -> None:
        self.db_path = db_path
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def handle_update(self, update: dict) -> dict:
        """Process a Telegram update dict that contains a callback query.

        Parses ``callback_data`` in the format ``"approve:<run_id>"`` or
        ``"reject:<run_id>"``, delegates to the appropriate DB method, and
        returns a result dict with ``{"ok": True, "action": ..., "run_id": ...}``.

        Args:
            update: Parsed Telegram ``Update`` JSON object.

        Returns:
            Result dict.  On unknown actions or missing data returns
            ``{"ok": False, "error": "..."}``
        """
        callback_query = update.get("callback_query", {})
        if not callback_query:
            return {"ok": False, "error": "No callback_query in update"}

        callback_data: str = callback_query.get("data", "")
        from_user = callback_query.get("from", {})
        reviewer = from_user.get("username") or from_user.get("first_name") or "telegram"

        if not callback_data or ":" not in callback_data:
            return {"ok": False, "error": f"Unrecognised callback_data: {callback_data!r}"}

        action, _, run_id = callback_data.partition(":")
        action = action.strip().lower()
        run_id = run_id.strip()

        if not run_id:
            return {"ok": False, "error": "Empty run_id in callback_data"}

        from pathlib import Path
        from orchestration_engine.db import Database

        db = Database(Path(self.db_path))

        if action == "approve":
            ok = db.approve_pipeline_run(
                run_id=run_id,
                reviewed_by=f"telegram:{reviewer}",
                note="Approved via Telegram inline button",
            )
            verb = "Approved"
        elif action == "reject":
            ok = db.reject_pipeline_run(
                run_id=run_id,
                reason="Rejected via Telegram inline button",
                reviewed_by=f"telegram:{reviewer}",
            )
            verb = "Rejected"
        else:
            return {"ok": False, "error": f"Unknown action: {action!r}"}

        if ok:
            conf_text = f"✅ {verb} run `{run_id}` by @{reviewer}"
            self._send_telegram_message(conf_text)
            self._notify_openclaw(conf_text, run_id, action)
        else:
            logger.warning(
                "TelegramCallbackHandler: %s run '%s' failed — run may not be in "
                "pending_review state.",
                action, run_id,
            )

        return {"ok": ok, "action": action, "run_id": run_id, "updated": ok}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_telegram_message(self, text: str) -> None:
        """Send a plain text message to the configured Telegram chat."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except Exception as exc:
            logger.warning("TelegramCallbackHandler: confirmation send failed: %s", exc)

    def _notify_openclaw(self, text: str, run_id: str, action: str) -> None:
        """Post a confirmation message to the OpenClaw main session."""
        if not self.gateway_url or not self.gateway_token:
            return
        gateway_url = self.gateway_url.rstrip("/")
        payload = json.dumps(
            {
                "session": "agent:main:main",
                "message": (
                    f"🔔 HITL Review via Telegram — run `{run_id}` was *{action}d* "
                    f"from Telegram."
                ),
            }
        ).encode("utf-8")
        url = f"{gateway_url}/api/v1/sessions/send"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.gateway_token}",
        }
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except Exception as exc:
            logger.warning("TelegramCallbackHandler: OpenClaw notify failed: %s", exc)


class NotificationDispatcher:
    """Dispatches pipeline lifecycle events to configured backends.

    Supports three optional backends (openclaw, webhook, telegram) controlled
    via a ``_config`` dict.  Always emits a WARNING-level log entry regardless
    of which backends are enabled.

    For ``human_review`` events the Telegram backend sends an enriched message
    with inline keyboard buttons ([Approve], [Reject], [View PR]) and respects
    a configurable quiet-hours window (default 23:00–08:00 Europe/Vienna).

    Usage::

        # From environment variables
        d = NotificationDispatcher.from_env()
        d.dispatch(
            event="human_review",
            run_id="abc123",
            score=0.72,
            issue_number=429,
            summary="Implement HITL Telegram notifications",
            confidence="medium",
            pr_url="https://github.com/org/repo/pull/42",
        )

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
            "telegram_enabled": False,
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            # Quiet-hours gate (Issue #429.5)
            "quiet_hours_enabled": True,
            "quiet_hours_start": 23,
            "quiet_hours_end": 8,
            "quiet_hours_tz": "Europe/Vienna",
            # Callback handler DB path (Issue #429.5)
            "telegram_callback_db_path": "",
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

        For ``human_review`` events the following extra kwargs are recognised
        and forwarded to the Telegram backend to build an enriched notification:

        Args:
            event:        Pipeline lifecycle event name.
            run_id:       Pipeline run identifier.
            issue_number: GitHub issue number linked to this run (optional).
            summary:      One-line summary from the last completed phase output,
                          truncated to 120 chars (optional).
            confidence:   Confidence level string e.g. ``"medium"`` (optional).
            pr_url:       URL of the associated pull request (optional).
            **kwargs:     Any other context fields (score, tier, …).
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

        if self._config.get("telegram_enabled"):
            try:
                self._dispatch_telegram(event=event, run_id=run_id, **kwargs)
            except Exception as exc:
                logger.warning("Telegram notification failed (swallowed): %s", exc)

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

    def _dispatch_telegram(self, event: str, run_id: str, **kwargs: Any) -> None:
        """Send a message to Telegram via the configured bot token and chat ID.

        For ``human_review`` events:
        - Quiet-hours check: if currently in the quiet window the notification
          is suppressed with a WARNING log entry.
        - An inline keyboard with [Approve], [Reject], and optionally [View PR]
          buttons is attached.

        All other events are forwarded to :class:`TelegramNotifier` without an
        inline keyboard.
        """
        bot_token = self._config.get("telegram_bot_token", "")
        chat_id = self._config.get("telegram_chat_id", "")
        if not bot_token or not chat_id:
            logger.warning(
                "Telegram notification skipped: missing bot_token or chat_id."
            )
            return

        # Quiet-hours gate
        if self._config.get("quiet_hours_enabled", True) and event == "human_review":
            tz = self._config.get("quiet_hours_tz", "Europe/Vienna")
            q_start = int(self._config.get("quiet_hours_start", 23))
            q_end = int(self._config.get("quiet_hours_end", 8))
            if _is_quiet_hours(tz=tz, quiet_start=q_start, quiet_end=q_end):
                logger.warning(
                    "Telegram HITL notification suppressed for run '%s': quiet hours "
                    "(%02d:00–%02d:00 %s).",
                    run_id, q_start, q_end, tz,
                )
                return

        # Build inline keyboard for human_review events
        inline_keyboard: Optional[List[List[dict]]] = None
        if event == "human_review":
            pr_url = kwargs.get("pr_url", "")
            approve_btn = {
                "text": "✅ Approve",
                "callback_data": f"approve:{run_id}",
            }
            reject_btn = {
                "text": "❌ Reject",
                "callback_data": f"reject:{run_id}",
            }
            keyboard_rows: List[List[dict]] = [[approve_btn, reject_btn]]
            if pr_url:
                keyboard_rows.append([{"text": "🔗 View PR", "url": pr_url}])
            inline_keyboard = keyboard_rows

        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
        notifier.dispatch(
            event=event,
            run_id=run_id,
            inline_keyboard=inline_keyboard,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "NotificationDispatcher":
        """Build a dispatcher from environment variables.

        Environment variables:
            NOTIFY_OPENCLAW_ENABLED            — "1", "true", "yes" to enable
            NOTIFY_OPENCLAW_GATEWAY_URL        — gateway base URL (falls back to OPENCLAW_GATEWAY_URL)
            NOTIFY_OPENCLAW_GATEWAY_TOKEN      — bearer token
            NOTIFY_OPENCLAW_SESSION            — target session (default: agent:main:main)
            NOTIFY_WEBHOOK_ENABLED             — "1", "true", "yes" to enable
            NOTIFY_WEBHOOK_URL                 — https://… webhook endpoint
            NOTIFY_WEBHOOK_SECRET              — HMAC signing secret
            NOTIFY_TELEGRAM_ENABLED            — "1", "true", "yes" to enable
            NOTIFY_TELEGRAM_BOT_TOKEN          — Telegram Bot API token
            NOTIFY_TELEGRAM_CHAT_ID            — Target chat/channel ID
            NOTIFY_QUIET_HOURS_ENABLED         — "1", "true", "yes" to enable (default: enabled)
            NOTIFY_QUIET_HOURS_START           — Start of quiet window, 0–23 (default: 23)
            NOTIFY_QUIET_HOURS_END             — End of quiet window, 0–23 (default: 8)
            NOTIFY_TELEGRAM_CALLBACK_DB_PATH   — Path to SQLite DB for callback handler
        """

        def _bool(val: str | None, default: bool = False) -> bool:
            if val is None:
                return default
            return str(val).strip().lower() in ("1", "true", "yes")

        def _int(val: str | None, default: int) -> int:
            try:
                return int(val) if val is not None else default
            except (TypeError, ValueError):
                return default

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

        # quiet_hours_enabled defaults to True when env var is absent
        quiet_env = os.environ.get("NOTIFY_QUIET_HOURS_ENABLED")
        quiet_hours_enabled = _bool(quiet_env, default=True)

        config = {
            "openclaw_enabled": _bool(os.environ.get("NOTIFY_OPENCLAW_ENABLED")),
            "openclaw_gateway_url": openclaw_gateway_url,
            "openclaw_gateway_token": os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN", ""),
            "openclaw_session": os.environ.get("NOTIFY_OPENCLAW_SESSION", "agent:main:main"),
            "webhook_enabled": _bool(os.environ.get("NOTIFY_WEBHOOK_ENABLED")),
            "webhook_url": webhook_url,
            "webhook_secret": os.environ.get("NOTIFY_WEBHOOK_SECRET", ""),
            "telegram_enabled": _bool(os.environ.get("NOTIFY_TELEGRAM_ENABLED")),
            "telegram_bot_token": os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", ""),
            # Quiet hours (Issue #429.5)
            "quiet_hours_enabled": quiet_hours_enabled,
            "quiet_hours_start": _int(os.environ.get("NOTIFY_QUIET_HOURS_START"), 23),
            "quiet_hours_end": _int(os.environ.get("NOTIFY_QUIET_HOURS_END"), 8),
            "quiet_hours_tz": os.environ.get("NOTIFY_QUIET_HOURS_TZ", "Europe/Vienna"),
            # Telegram callback DB path (Issue #429.5)
            "telegram_callback_db_path": os.environ.get(
                "NOTIFY_TELEGRAM_CALLBACK_DB_PATH", ""
            ),
        }
        return cls(config)
