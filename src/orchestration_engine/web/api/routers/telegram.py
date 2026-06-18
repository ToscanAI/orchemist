"""Telegram HITL callback route group for the REST API (Issue #942, sub-issue 952d).

Holds the single Telegram Bot API webhook route, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``POST /api/v1/telegram/callback``   (HITL inline-keyboard approve/reject, Issue #429.5)

The ``hmac`` / ``json`` / ``os`` modules the closure references are imported at
module scope here (the same standard-library modules ``_app`` imported); the
lazy in-closure ``from orchestration_engine.notifications import TelegramCallbackHandler``
is preserved exactly (already absolute — the same module the inline import
resolved to, so ``patch("orchestration_engine.notifications.TelegramCallbackHandler")``
still intercepts at call time). ``JSONResponse``, ``HTTPException``, ``Request``,
the per-call ``effective_db_path`` value, and the module ``logger`` are received
as keyword arguments — the same objects the inline closure used.
"""

import hmac
import json
import os
from typing import Any


def register_telegram_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Request: Any,  # noqa: N803
    effective_db_path: str,
    logger: Any,
) -> None:
    """Register the Telegram HITL callback route group onto *app*.

    Mirrors the inline definition that previously lived in ``create_api_app``
    — identical path, verb, status code, secret-token verification, JSON
    parsing, handler construction, response shape and error handling.
    """

    @app.post("/api/v1/telegram/callback", status_code=200)
    async def telegram_callback(request: Request) -> JSONResponse:
        """Handle Telegram Bot API webhook updates for HITL inline keyboard actions.

        Telegram delivers a ``callback_query`` update when the user taps an
        [Approve] or [Reject] button on a ``human_review`` notification.  This
        endpoint verifies the ``X-Telegram-Bot-Api-Secret-Token`` header,
        delegates to :class:`~orchestration_engine.notifications.TelegramCallbackHandler`,
        and returns an ``{"ok": true}`` response to Telegram.

        Security:
            The ``NOTIFY_TELEGRAM_WEBHOOK_SECRET`` environment variable must be
            set.  Requests without a matching secret token header are rejected
            with HTTP 403.

        Returns:
            - **200** with ``{"ok": true, "action": ..., "run_id": ...}`` on success.
            - **403** when the secret token header is missing or invalid.
            - **400** when the request body is not valid JSON or the callback
              data cannot be parsed.
        """
        # Verify the shared secret Telegram sends in the header
        webhook_secret = os.environ.get("NOTIFY_TELEGRAM_WEBHOOK_SECRET", "")
        if webhook_secret:
            token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(token_header, webhook_secret):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Telegram-Bot-Api-Secret-Token header",
                )
        else:
            logger.warning(
                "NOTIFY_TELEGRAM_WEBHOOK_SECRET is not set — "
                "/api/v1/telegram/callback is accepting unauthenticated requests. "
                "Set the env var to enable webhook signature verification."
            )

        try:
            body_bytes = await request.body()
            update = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        # Resolve DB path: env var → config → persistent default
        db_path = os.environ.get("NOTIFY_TELEGRAM_CALLBACK_DB_PATH", "") or effective_db_path
        gateway_url = os.environ.get(
            "NOTIFY_OPENCLAW_GATEWAY_URL",
            os.environ.get("OPENCLAW_GATEWAY_URL", ""),
        )
        gateway_token = os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN", "")
        bot_token = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "")

        from orchestration_engine.notifications import TelegramCallbackHandler  # noqa: PLC0415

        handler = TelegramCallbackHandler(
            db_path=db_path,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            bot_token=bot_token,
            chat_id=chat_id,
        )

        result = handler.handle_update(update)
        if not result.get("ok"):
            # Log the error but return 200 to prevent Telegram from retrying
            logger.warning("Telegram callback handler returned non-ok result: %s", result)
        return JSONResponse(result)
