"""Daemon notification-suppression helper (Issue #660).

Extracted verbatim from :mod:`orchestration_engine.daemon` (wave a of #1034);
the public surface is re-exported by the package facade, so callers continue to
import this name from ``orchestration_engine.daemon``.
"""

# ruff: noqa: E501

import logging
import os

logger = logging.getLogger(__name__)


def _apply_daemon_notification_suppression() -> None:
    """Suppress OpenClaw notifications for the daemon process.

    Force-clears ``NOTIFY_OPENCLAW_ENABLED`` in the current process environment
    so that every subsequent :meth:`~orchestration_engine.notifications.NotificationDispatcher.from_env`
    call within the daemon produces a dispatcher with the OpenClaw backend
    disabled.

    This prevents daemon pipeline events (``human_review``, ``auto_merge``)
    from triggering ``sessions_send`` calls to the Claude Code / TUI session,
    which was causing rogue agent spawning that interfered with running
    pipelines.

    Telegram and Webhook backends are unaffected — those notify humans, not AI
    agents.

    **Opt-in:** set ``NOTIFY_OPENCLAW_DAEMON_ENABLED=1`` (or ``true`` / ``yes``)
    to re-enable OpenClaw notifications from daemon runs if explicitly desired.

    Any failure to apply the suppression is logged as a WARNING and silently
    swallowed so the daemon continues operating (non-fatal).
    """
    daemon_flag = os.environ.get("NOTIFY_OPENCLAW_DAEMON_ENABLED", "")
    if str(daemon_flag).strip().lower() in ("1", "true", "yes"):
        logger.info(
            "NOTIFY_OPENCLAW_DAEMON_ENABLED=%r — OpenClaw notifications retained "
            "for daemon process",
            daemon_flag,
        )
        return

    try:
        os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""
        logger.info(
            "OpenClaw notifications suppressed for daemon process "
            "(set NOTIFY_OPENCLAW_DAEMON_ENABLED=1 to re-enable)"
        )
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        logger.warning("Failed to suppress OpenClaw notifications (non-fatal): %s", exc)
