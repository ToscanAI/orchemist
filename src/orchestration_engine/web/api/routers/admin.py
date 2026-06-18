"""Admin-console + SSE-metrics route group for the REST API (Issue #942, sub-issue 952d).

Holds the admin-state read/write/audit routes plus the SSE connection-metrics
probe, extracted *verbatim* from ``create_api_app`` via the register-function
pattern (see :mod:`orchestration_engine.web.api.routers`):

* ``GET /api/v1/admin/state``            (aggregate admin-console read state)
* ``PUT /api/v1/admin/feature-flags``    (persist a feature-flag patch, atomic)
* ``GET /api/v1/admin/audit-log``        (append-only admin mutation audit log)
* ``GET /api/v1/sse/metrics``            (SSE connection counts + limits, #841)

These form a cohesive group: all four back the harness Admin Console. The
``/sse/metrics`` probe is surfaced by the same console (and by ops dashboards),
so it rides with the admin routes and closes over the SAME ``_sse_limiter``
instance the SSE stream handler uses, preserving the admit/release accounting.

The admin-doc helpers (``_ADMIN_DEFAULTS``, ``_ADMIN_KNOWN_FLAGS``,
``_strict_coerce_bool``, ``_coerce_admin_doc``, ``_merge_feature_flags_with_passthrough``)
live at the package's module scope (``admin_flags`` / ``schemas``); they are
received here as keyword arguments — the same objects the inline closures used.
The lazy in-closure ``from ... import feature_flags`` becomes the equivalent
absolute ``from orchestration_engine import feature_flags`` (the identical module
the inline import resolved to). ``Database``, ``JSONResponse``, ``HTTPException``,
``Request``, the per-call ``effective_db_path`` value, the shared ``_sse_limiter``
instance, and the module ``logger`` are all received as keyword arguments.
"""

from pathlib import Path
from typing import Any, Dict


def register_admin_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Request: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    effective_db_path: str,
    _ADMIN_DEFAULTS: Dict[str, Any],  # noqa: N803 (matches the module-scope constant name)
    _ADMIN_KNOWN_FLAGS: Any,  # noqa: N803
    _strict_coerce_bool: Any,
    _coerce_admin_doc: Any,
    _merge_feature_flags_with_passthrough: Any,
    _sse_limiter: Any,
    logger: Any,
) -> None:
    """Register the admin-console + SSE-metrics route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, query params, strict bool coercion, atomic
    tempfile+os.replace write, forward-compat passthrough, audit-log append,
    response shapes and error handling.

    Concurrency model (no asyncio.Lock — round-3 simplification):
    - PUT /admin/feature-flags has zero ``await`` points inside its
      read-modify-write critical section, so FastAPI's single-event-loop
      scheduling already serialises it.
    - ``os.replace()`` is atomic on POSIX, so the on-disk file is always
      well-formed (last-writer-wins for cross-process writers).
    """

    @app.get("/api/v1/admin/state")
    async def get_admin_state() -> JSONResponse:
        """Aggregate admin-console read state.

        Returns the operator-set values (or canonical defaults) regardless
        of how malformed the on-disk JSON is. Defensive against:
        - missing file
        - unparseable JSON
        - top-level value that isn't a dict (e.g. a string, list, number)
        - nested ``feature_flags`` / ``modes`` keys that aren't dicts
        - flag values that aren't bools (coerced via `_strict_coerce_bool`)
        - unknown extra keys (preserved under ``"extra"``)
        """
        import json as _json  # noqa: PLC0415

        from orchestration_engine import feature_flags as _ff  # noqa: PLC0415

        admin_path = _ff._admin_json_path()  # honours ORCH_ADMIN_PATH (#840)
        raw_loaded: Any = None
        source = "default"
        if admin_path.exists():
            try:
                raw_loaded = _json.loads(admin_path.read_text())
                source = "file"
            except (OSError, ValueError):
                raw_loaded = None
        merged = _coerce_admin_doc(raw_loaded)
        # Preserve any unknown top-level keys for forward-compat (the next
        # release may add new admin settings; downgraded engines shouldn't
        # silently drop them on read).
        extra: Dict[str, Any] = {}
        if isinstance(raw_loaded, dict):
            extra = {
                k: v
                for k, v in raw_loaded.items()
                if k not in {"autonomy_level", "feature_flags", "modes"}
            }
        return JSONResponse(
            {
                **merged,
                "extra": extra,
                "source": source,
                "path": str(admin_path),
            }
        )

    @app.put("/api/v1/admin/feature-flags")
    async def update_feature_flags(request: Request) -> JSONResponse:  # noqa: C901
        """Persist a feature-flag patch to `admin.json` with atomic write.

        Body: ``{"phase0_hard_gate": true, ...}`` — any subset of the known
        flags. Strict bool coercion: accepts Python/JSON bools, 0/1, and
        the strings ``"true"``/``"false"`` (case-insensitive); anything
        else is rejected with 400.

        Concurrency: the handler body has zero ``await`` points inside its
        read-modify-write critical section, so FastAPI's single-event-loop
        scheduling already serialises it (a coroutine without ``await``
        cannot be preempted by asyncio). For the cross-process case,
        ``os.replace()`` atomicity gives last-writer-wins on the on-disk
        file — acceptable for operator-preferred admin flags.

        Atomicity: tempfile + ``os.replace`` ensures the on-disk file is
        never half-written. Tempfile cleanup uses ``Path.unlink(missing_ok=True)``
        inside a guarded ``finally`` so the original exception is preserved
        even if cleanup itself fails.

        Forward-compat: unknown nested keys inside ``feature_flags`` on
        disk (e.g. a flag from a future beta build) are preserved verbatim
        through ``_merge_feature_flags_with_passthrough``. Known flags are
        always normalised to ``bool``.
        """
        import json as _json  # noqa: PLC0415
        import os as _os  # noqa: PLC0415
        import tempfile as _tempfile  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        bad_keys = set(body.keys()) - _ADMIN_KNOWN_FLAGS
        if bad_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown flag(s): {sorted(bad_keys)}. Known: {sorted(_ADMIN_KNOWN_FLAGS)}",
            )
        # Reject any value that isn't an explicit canonical boolean. The
        # strict-coerce helper returns None on unrecognised input → 400.
        patch: Dict[str, bool] = {}
        for k, v in body.items():
            coerced = _strict_coerce_bool(v)
            if coerced is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Flag {k!r} expects a boolean (or 0/1, or 'true'/'false') — "
                        f"got {type(v).__name__}: {v!r}"
                    ),
                )
            patch[k] = coerced

        from orchestration_engine import feature_flags as _ff  # noqa: PLC0415

        admin_path = _ff._admin_json_path()  # honours ORCH_ADMIN_PATH (#840)
        admin_dir = admin_path.parent

        admin_dir.mkdir(parents=True, exist_ok=True)
        current: Dict[str, Any] = {}
        if admin_path.exists():
            try:
                loaded = _json.loads(admin_path.read_text())
                if isinstance(loaded, dict):
                    current = loaded
            except (OSError, ValueError):
                current = {}
        # Canonicalise the merged feature_flags BEFORE writing so disk and
        # response always agree (round-3 fix), while preserving unknown
        # nested keys for forward-compat (round-4 fix). The previous
        # canonicalise-via-_coerce_admin_doc silently dropped any flag a
        # forward-compat operator (or beta build) had set on disk that
        # wasn't in _ADMIN_KNOWN_FLAGS.
        existing_flags = current.get("feature_flags")
        disk_flags: Dict[str, Any] = (
            dict(existing_flags) if isinstance(existing_flags, dict) else {}
        )
        before_canonical: Dict[str, Any] = {
            k: disk_flags.get(k, _ADMIN_DEFAULTS["feature_flags"][k]) for k in _ADMIN_KNOWN_FLAGS
        }
        disk_flags.update(patch)
        merged_flags = _merge_feature_flags_with_passthrough(disk_flags)
        current["feature_flags"] = merged_flags
        # The response only exposes known flags (the AdminState TS contract);
        # unknown ones live on disk for the future engine to discover.
        canonical_flags = {k: merged_flags[k] for k in _ADMIN_KNOWN_FLAGS}

        # Atomic write: tempfile in the same directory + os.replace.
        # No asyncio.Lock — see _ADMIN_DEFAULTS / concurrency-model comment
        # above for why this handler is already serialised by the event loop.
        fd, tmp = _tempfile.mkstemp(prefix="admin.", suffix=".tmp", dir=str(admin_dir))
        try:
            with _os.fdopen(fd, "w") as fh:
                _json.dump(current, fh, indent=2)
            _os.replace(tmp, admin_path)
            tmp = None  # ownership transferred to the destination
        finally:
            # Clean up the tempfile if the replace failed. Path.unlink
            # with missing_ok=True avoids the TOCTOU window of the prior
            # `if os.path.exists(): os.unlink()` pattern and won't mask
            # the original exception with its own.
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
        # Append-only audit log (#838) — record only the keys that
        # ACTUALLY changed value, so audit rows are scannable.
        changed_keys = sorted(k for k, v in canonical_flags.items() if before_canonical.get(k) != v)
        if changed_keys:
            try:
                db = Database(Path(effective_db_path))
                db.append_admin_audit(
                    action="update_feature_flags",
                    target=",".join(changed_keys),
                    before={k: before_canonical[k] for k in changed_keys},
                    after={k: canonical_flags[k] for k in changed_keys},
                )
            except Exception as _exc:  # noqa: BLE001 — audit log is best-effort
                logger.warning("admin audit-log append failed: %s", _exc)
        return JSONResponse(
            {
                "feature_flags": canonical_flags,
                "path": str(admin_path),
            }
        )

    @app.get("/api/v1/admin/audit-log")
    async def get_admin_audit_log(limit: int = 100, offset: int = 0) -> JSONResponse:
        """Return the admin-state audit log (#838).

        Append-only record of every mutation made via the admin API. Each
        row carries the before/after JSON, the action verb, the target
        surface, and the OS pid of the FastAPI worker that served the
        request (best-effort attribution — the engine has no per-user
        auth today).

        Query params:
            limit (int): Max rows to return (default 100, max 1000).
            offset (int): Skip this many newest rows (default 0).

        Response::

            {
                "rows": [
                    {
                        "id": 42,
                        "action": "update_feature_flags",
                        "target": "phase0_hard_gate,dialogue_phase",
                        "before": {...},
                        "after": {...},
                        "source_pid": 12345,
                        "created_at": "2026-05-25 18:31:12"
                    },
                    ...
                ],
                "limit": 100,
                "offset": 0
            }
        """
        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=400,
                detail="limit must be between 1 and 1000",
            )
        if offset < 0:
            raise HTTPException(
                status_code=400,
                detail="offset must be >= 0",
            )
        db = Database(Path(effective_db_path))
        rows = db.list_admin_audit(limit=limit, offset=offset)
        return JSONResponse(
            {
                "rows": rows,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/sse/metrics")
    async def get_sse_metrics() -> JSONResponse:
        """Return current SSE connection counts and limits (#841).

        Surfaced by the harness Admin Console + by ops dashboards. The
        ``active_total`` and ``active_per_ip`` snapshots are read under
        the lock; the limits are re-read from env vars on every call
        (operator may have re-tuned live).
        """
        return JSONResponse(_sse_limiter.metrics())
