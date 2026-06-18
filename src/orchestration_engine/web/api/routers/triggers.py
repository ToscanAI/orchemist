"""Triggers + webhooks route group for the REST API (Issue #942, sub-issue 952c).

Holds the incoming-webhook receiver plus the trigger CRUD routes, extracted
*verbatim* from ``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``POST   /api/v1/webhooks/{trigger_id}``   (receive webhook, fire pipeline)
* ``POST   /api/v1/triggers``                (create)
* ``GET    /api/v1/triggers``                (list)
* ``GET    /api/v1/triggers/{trigger_id}``   (detail)
* ``PUT    /api/v1/triggers/{trigger_id}``   (update)
* ``DELETE /api/v1/triggers/{trigger_id}``   (delete)

``TriggerCreateRequest`` / ``TriggerUpdateRequest`` (used only by the trigger
CRUD routes) move with the routes into ``register_trigger_routes``; they are
defined inside the register function with a lazy ``from pydantic import
BaseModel`` so importing this module does NOT eagerly pull ``pydantic``
(preserving the optional ``[api]`` extra contract), exactly as the inline
definitions did inside ``create_api_app``.

``_trigger_to_response`` is used only by these routes, so it moves into
``register_trigger_routes`` as a nested local — verbatim.  The shared
factory-local helpers ``_resolve_template`` and ``_launch_pipeline_from_trigger``
are also used by routes that remain in ``_app.py`` (and ``routers/runs.py``), so
they stay there and are received here as keyword arguments — the same objects the
inline closures used.  The lazy in-closure imports (``TriggerConfig``,
``TriggerValidationError``, ``sqlite3``) stay exactly where they were.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def register_trigger_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Request: Any,  # noqa: N803
    Response: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    TemplateEngine: Any,  # noqa: N803
    InputMapper: Any,  # noqa: N803
    TriggerMatcher: Any,  # noqa: N803
    SlidingWindowRateLimiter: Any,  # noqa: N803
    _verify_github_signature: Any,
    _apply_input_map: Any,
    effective_db_path: str,
    _resolve_template: Any,
    _launch_pipeline_from_trigger: Any,
) -> None:
    """Register the triggers + webhooks route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, status codes, params, signature verification,
    rate-limiting, filter/input-map evaluation, response shapes and error
    handling.

    ``TriggerCreateRequest`` / ``TriggerUpdateRequest`` are defined here (not at
    module scope) with a lazy ``from pydantic import BaseModel`` so importing this
    module does NOT eagerly pull ``pydantic`` — preserving the optional ``[api]``
    extra contract, exactly as the inline definitions did inside
    ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class TriggerCreateRequest(BaseModel):
        """Body for POST /api/v1/triggers — create a new webhook trigger."""

        id: Optional[str] = None
        """Trigger identifier.  When omitted a unique ID is generated
        automatically (``trig-<12 hex chars>``)."""

        template_id: str
        """ID of the pipeline template to run when this trigger fires."""

        mode: str = "async"
        """Execution mode: ``'sync'``, ``'async'``, or ``'fire_and_forget'``."""

        secret: Optional[str] = None
        """Optional shared HMAC secret for request verification (write-only —
        never returned in responses)."""

        rate_limit: int = 0
        """Maximum requests per minute (0 = unlimited)."""

        input_map: Dict[str, Any] = {}
        """Maps webhook payload fields to pipeline input variables."""

        filters: List[Dict[str, Any]] = []
        """List of filter conditions evaluated against incoming payloads."""

        enabled: bool = True
        """Whether this trigger is active.  Disabled triggers silently skip."""

    class TriggerUpdateRequest(BaseModel):
        """Body for PUT /api/v1/triggers/{id} — update an existing trigger.

        All fields are optional; only provided fields are updated.
        """

        mode: Optional[str] = None
        secret: Optional[str] = None
        rate_limit: Optional[int] = None
        input_map: Optional[Dict[str, Any]] = None
        filters: Optional[List[Dict[str, Any]]] = None
        enabled: Optional[bool] = None

    # ------------------------------------------------------------------
    # Helper — redact secret and build TriggerResponse dict from a DB row
    # ------------------------------------------------------------------

    def _trigger_to_response(row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB trigger row dict to a TriggerResponse-compatible dict.

        The ``secret`` field is redacted to ``'***'`` when set, to enforce
        write-only semantics.

        Args:
            row: A trigger row dict as returned by ``db.get_trigger()`` or
                 ``db.list_triggers()``.

        Returns:
            A dict safe to serialise as a ``TriggerResponse``.
        """
        return {
            "id": row["id"],
            "template_id": row["template_id"],
            "mode": row.get("mode", "async"),
            "secret": "***" if row.get("secret") else None,
            "rate_limit": row.get("rate_limit", 0),
            "input_map": row.get("input_map") or {},
            "filters": row.get("filters") or [],
            "enabled": bool(row.get("enabled", True)),
            "created_at": row.get("created_at"),
        }

    @app.post("/api/v1/webhooks/{trigger_id}")
    async def handle_webhook(trigger_id: str, request: Request) -> JSONResponse:  # noqa: C901
        """Receive an incoming webhook and fire the associated pipeline.

        Looks up the trigger configuration, verifies the HMAC-SHA256 signature
        (when a ``secret`` is configured), enforces the per-trigger rate limit,
        applies the ``input_map`` to transform the webhook payload into pipeline
        input vars, and launches the pipeline.

        Path parameter:
            trigger_id (str): Trigger identifier (must match a row in the
                ``triggers`` table).

        Request headers:
            X-Hub-Signature-256: Optional HMAC-SHA256 signature header
                (required when the trigger has a ``secret`` configured).

        Returns:
            - **200** when the trigger is disabled (no pipeline launched).
            - **201** when the pipeline was launched in ``async`` or ``sync``
              mode.
            - **200** when the trigger mode is ``fire_and_forget``.
            - **403** when the signature is invalid or missing (and a secret
              is configured).
            - **404** when the trigger is not found.
            - **429** when the rate limit is exceeded.
        """
        db = Database(Path(effective_db_path))

        # 1. Look up trigger
        trigger_row = db.get_trigger(trigger_id)
        if trigger_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )

        # 2. Check enabled flag — disabled triggers silently accept but skip
        if not trigger_row.get("enabled", True):
            return JSONResponse(
                {"status": "skipped", "reason": "trigger_disabled"},
                status_code=200,
            )

        # 3. Read body bytes (must happen before signature verification)
        payload_bytes = await request.body()

        # 4. Verify GitHub HMAC-SHA256 signature (if secret configured)
        secret = trigger_row.get("secret")
        if secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(secret, payload_bytes, sig_header):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing webhook signature",
                )

        # 5. Rate-limit check (sliding-window, 60-second window)
        rate_limit = trigger_row.get("rate_limit", 0)
        limiter = SlidingWindowRateLimiter(db)
        if limiter.check(trigger_id, rate_limit):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit of {rate_limit} req/min exceeded for trigger '{trigger_id}'",
            )

        # 6. Parse payload JSON
        try:
            payload: Dict[str, Any] = json.loads(payload_bytes) if payload_bytes else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}

        # 6b. Evaluate trigger filters — if any filter doesn't match, skip silently
        filters = trigger_row.get("filters") or []
        if filters and not TriggerMatcher.matches(filters, payload):
            return JSONResponse(
                {"status": "skipped", "reason": "filter_mismatch"},
                status_code=200,
            )

        # 7. Apply input_map to transform payload → pipeline input
        # Values starting with "$." are resolved by _apply_input_map (dot-path).
        # Values of the form "{{payload.x.y}}" are resolved by InputMapper (template).
        # Other values are treated as literals.
        input_map = trigger_row.get("input_map") or {}
        if input_map:
            # First pass: resolve $.path expressions
            dot_path_map = {
                k: v
                for k, v in input_map.items()
                if not (isinstance(v, str) and v.startswith("{{payload."))
            }
            template_map = {
                k: v
                for k, v in input_map.items()
                if isinstance(v, str) and v.startswith("{{payload.")
            }
            input_data = _apply_input_map(payload, dot_path_map) if dot_path_map else {}
            # Second pass: resolve {{payload.*}} templates
            if template_map:
                input_data.update(InputMapper.apply(payload, template_map))
        else:
            input_data = dict(payload)

        # 8. Resolve and validate template
        template_id = trigger_row["template_id"]
        template_file = _resolve_template(template_id)

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        # 9. Record invocation (after all guards pass, before launching)
        db.record_webhook_invocation(trigger_id)

        # 10. Launch pipeline via shared helper
        trigger_mode = trigger_row.get("mode", "async")
        # Map trigger mode to daemon mode (fire_and_forget → standalone)
        daemon_mode_map = {
            "sync": "standalone",
            "async": "standalone",
            "fire_and_forget": "standalone",
        }
        daemon_mode = daemon_mode_map.get(trigger_mode, "standalone")

        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=input_data,
            mode=daemon_mode,
            gateway_url=gateway_url,
            db=db,
        )

        # fire_and_forget → respond 200 (accepted, no run details)
        if trigger_mode == "fire_and_forget":
            return JSONResponse(
                {"status": "accepted", "run_id": run_dict["run_id"]},
                status_code=200,
            )

        return JSONResponse(run_dict, status_code=201)

    # ------------------------------------------------------------------
    # Trigger CRUD endpoints
    # ------------------------------------------------------------------

    @app.post("/api/v1/triggers", status_code=201)
    async def create_trigger(body: TriggerCreateRequest) -> JSONResponse:
        """Create a new webhook trigger.

        Args:
            body: ``TriggerCreateRequest`` JSON body.

        Returns:
            - **201** with a ``TriggerResponse`` JSON object on success.
            - **400** when the trigger config fails validation.
            - **409** when a trigger with the same ``id`` already exists.
        """
        from orchestration_engine.webhooks import (  # noqa: PLC0415
            TriggerConfig,
            TriggerValidationError,
        )

        db = Database(Path(effective_db_path))

        trigger_id = body.id or TriggerConfig.generate_id()

        try:
            cfg = TriggerConfig(
                id=trigger_id,
                template_id=body.template_id,
                mode=body.mode,
                secret=body.secret,
                rate_limit=body.rate_limit,
                input_map=body.input_map,
                filters=body.filters,
                enabled=body.enabled,
            )
        except TriggerValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        import sqlite3  # noqa: PLC0415

        try:
            db.create_trigger(cfg.to_dict())
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"Trigger with id '{trigger_id}' already exists",
            )

        row = db.get_trigger(trigger_id)
        return JSONResponse(_trigger_to_response(row), status_code=201)

    @app.get("/api/v1/triggers")
    async def list_triggers_endpoint(
        template_id: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """List webhook triggers with optional filtering and pagination.

        Query parameters:
            template_id: Filter by template id.
            mode: Filter by execution mode.
            enabled: ``'true'`` / ``'false'`` to filter by enabled state.
            limit: Maximum number of results (default 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array.
        """
        db = Database(Path(effective_db_path))

        # Parse the enabled query param (string) into Optional[bool]
        enabled_filter: Optional[bool] = None
        if enabled is not None:
            if enabled.lower() == "true":
                enabled_filter = True
            elif enabled.lower() == "false":
                enabled_filter = False
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Query param 'enabled' must be 'true' or 'false'",
                )

        rows = db.list_triggers(
            template_id=template_id,
            mode=mode,
            enabled=enabled_filter,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"items": [_trigger_to_response(r) for r in rows]})

    @app.get("/api/v1/triggers/{trigger_id}")
    async def get_trigger_endpoint(trigger_id: str) -> JSONResponse:
        """Get a single webhook trigger by id.

        Path parameter:
            trigger_id: Trigger identifier.

        Returns:
            - **200** with a ``TriggerResponse`` JSON object.
            - **404** when the trigger is not found.
        """
        db = Database(Path(effective_db_path))
        row = db.get_trigger(trigger_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )
        return JSONResponse(_trigger_to_response(row))

    @app.put("/api/v1/triggers/{trigger_id}")
    async def update_trigger_endpoint(trigger_id: str, body: TriggerUpdateRequest) -> JSONResponse:
        """Update an existing webhook trigger.

        Only fields that are explicitly provided in the request body are
        updated; omitted fields retain their current values.

        Path parameter:
            trigger_id: Trigger identifier.

        Args:
            body: ``TriggerUpdateRequest`` JSON body.

        Returns:
            - **200** with the updated ``TriggerResponse`` JSON object.
            - **400** when validation fails.
            - **404** when the trigger is not found.
        """
        from orchestration_engine.webhooks import TriggerValidationError  # noqa: PLC0415

        db = Database(Path(effective_db_path))

        # Verify the trigger exists
        existing = db.get_trigger(trigger_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )

        # Build kwargs with only the fields that were provided
        update_kwargs: Dict[str, Any] = {}
        if body.mode is not None:
            update_kwargs["mode"] = body.mode
        if body.secret is not None:
            update_kwargs["secret"] = body.secret
        if body.rate_limit is not None:
            update_kwargs["rate_limit"] = body.rate_limit
        if body.input_map is not None:
            update_kwargs["input_map"] = body.input_map
        if body.filters is not None:
            update_kwargs["filters"] = body.filters
        if body.enabled is not None:
            update_kwargs["enabled"] = body.enabled

        if update_kwargs:
            # Validate the merged result before writing
            from orchestration_engine.webhooks import TriggerConfig  # noqa: PLC0415

            merged = {**existing, **update_kwargs}
            try:
                TriggerConfig.from_dict(merged)
            except TriggerValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            db.update_trigger(trigger_id, **update_kwargs)

        row = db.get_trigger(trigger_id)
        return JSONResponse(_trigger_to_response(row))

    @app.delete("/api/v1/triggers/{trigger_id}", status_code=204)
    async def delete_trigger_endpoint(trigger_id: str) -> Response:
        """Delete a webhook trigger.

        Path parameter:
            trigger_id: Trigger identifier.

        Returns:
            - **204** (no content) on success.
            - **404** when the trigger is not found.
        """
        db = Database(Path(effective_db_path))
        deleted = db.delete_trigger(trigger_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )
        return Response(status_code=204)
