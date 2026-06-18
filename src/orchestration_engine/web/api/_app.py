"""FastAPI REST API app factory for the Orchestration Engine (Issue #257).

This module holds :func:`create_api_app`, the FastAPI app factory. It is part of
the ``web/api/`` package created by the facade-preserving decomposition of the
former ``web/api.py`` god-module (Issue #942, sub-issue 952a). The closure-free
module-level members were extracted into sibling modules and are imported below;
the public surface is re-exported from the package
:mod:`orchestration_engine.web.api` facade.

Route extraction is **complete** (Issue #942, sub-issues 952b/952c/952d): every
inner ``@app.<verb>`` route closure has been moved *verbatim* into a per-group
module under ``routers/`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`). ``create_api_app`` is now purely:
build the app, wire its dependencies + shared helpers, then call each
``register_*_routes(app, ...)`` function — it contains ZERO ``@app.<verb>``
decorators. Each register function receives, as explicit keyword arguments, every
object its closures reference from the factory scope (framework classes, the
``Database`` / ``TemplateEngine`` classes, the per-call ``effective_db_path``
value, the shared helpers, the ``_SSE_LIMITER`` instance, and the module
``logger``), so the transformation is provably behaviour-neutral.

All dependencies (fastapi, uvicorn) are optional extras — import this module
only after confirming they are installed.
"""

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestration_engine.db import parse_json_list
from orchestration_engine.env_utils import env_int

from .admin_flags import _ADMIN_DEFAULTS, _ADMIN_KNOWN_FLAGS
from .deps import _get_persistent_db_path
from .routers.admin import register_admin_routes
from .routers.costs import register_cost_routes
from .routers.gates import register_gate_routes
from .routers.github import register_github_routes
from .routers.health import register_health_routes
from .routers.issues import register_issue_routes
from .routers.phases import register_phase_routes
from .routers.regressions import register_regression_routes
from .routers.reviews import register_review_routes
from .routers.runs import register_run_routes
from .routers.telegram import register_telegram_routes
from .routers.templates import register_template_routes
from .routers.triggers import register_trigger_routes
from .routers.trust import register_trust_routes
from .schemas import (
    _apply_input_map,
    _coerce_admin_doc,
    _merge_feature_flags_with_passthrough,
    _strict_coerce_bool,
)
from .security import SlidingWindowRateLimiter, _verify_github_signature
from .sse import _SSE_LIMITER

logger = logging.getLogger(__name__)


def create_api_app(  # noqa: C901
    db_path: Optional[str] = None,
    user_templates_dir: Optional["Path"] = None,  # type: ignore[name-defined]
) -> "FastAPI":  # noqa: F821 (type hint only)
    """Create and return the REST API FastAPI application.

    Args:
        db_path: Path to the SQLite DB for pipeline_runs.  Defaults to the
                 same persistent DB used by ``orch launch``.
        user_templates_dir: Override the user templates directory used by
                            :class:`~orchestration_engine.templates.TemplateEngine`.
                            Useful in tests to isolate the user template store.
                            Defaults to ``~/.orch/templates/``.

    Returns:
        Configured ``FastAPI`` instance.
    """
    import asyncio  # noqa: PLC0415

    from fastapi import FastAPI, HTTPException, Request, Response  # noqa: PLC0415
    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from pydantic import BaseModel  # noqa: PLC0415
    from sse_starlette.sse import EventSourceResponse  # noqa: PLC0415

    from orchestration_engine import __version__  # noqa: PLC0415
    from orchestration_engine.db import TERMINAL_STATUSES, Database  # noqa: PLC0415
    from orchestration_engine.templates import (  # noqa: PLC0415
        TemplateEngine,
        TemplateNotFoundError,
    )
    from orchestration_engine.timestamps import (  # noqa: PLC0415
        normalize_ts as _normalize_ts,
    )
    from orchestration_engine.timestamps import (  # noqa: PLC0415
        now_utc as _now_utc,
    )
    from orchestration_engine.webhooks import InputMapper, TriggerMatcher  # noqa: PLC0415

    effective_db_path = db_path or _get_persistent_db_path()

    # Capture user_templates_dir so route handlers can create properly configured engines.
    _user_templates_dir = user_templates_dir

    def _make_engine() -> "TemplateEngine":  # type: ignore[name-defined]
        """Create a TemplateEngine with the configured user templates directory."""
        if _user_templates_dir is not None:
            return TemplateEngine(user_dir=_user_templates_dir)
        return TemplateEngine()

    app = FastAPI(
        title="Orchestration Engine REST API",
        version=__version__,
        description=(
            "Programmatic JSON REST API for the Orchestration Engine.  "
            "Backed by the same daemon-based async execution used by ``orch launch``."
        ),
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # CORS — wide-open for local/CI use; tighten in production deployments.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Startup sweep (#754) — reap zombie pipeline runs left over from a
    # previous engine crash so the ORCH_MAX_DAEMONS=8 cap (#839) is
    # accurate from the first request. Sweep errors are logged but
    # NEVER block server start — the API must come up even if the DB
    # is briefly unhealthy.
    # ------------------------------------------------------------------
    @app.on_event("startup")
    def _startup_sweep_zombie_runs() -> None:
        try:
            _db = Database(Path(effective_db_path))
            _swept = _db.sweep_zombie_runs()
            if _swept >= 1:
                logger.info(
                    "Startup sweep: marked %d zombie pipeline run(s) as crashed "
                    "(daemons died without updating status; #754)",
                    _swept,
                )
            else:
                logger.debug("Startup sweep: marked 0 zombie pipeline runs (clean state; #754)")
        except Exception as _exc:  # pragma: no cover — last-resort guard  # noqa: BLE001
            logger.error(
                "Startup sweep failed (server will still start): %s: %s",
                type(_exc).__name__,
                _exc,
            )

    # ------------------------------------------------------------------
    # Pydantic request/response models
    # ------------------------------------------------------------------

    class RunResponse(BaseModel):
        """Serialised pipeline run record returned by the API."""

        run_id: str
        template_id: str
        template_path: str
        mode: str
        status: str
        current_phase: Optional[str]
        completed_phases: List[str]
        pid: Optional[int]
        output_dir: str
        error_message: Optional[str]
        gateway_url: Optional[str]
        skip_scoring: bool
        scoring_status: Optional[str]
        scoring_score: Optional[float]
        started_at: Optional[str]
        completed_at: Optional[str]
        created_at: Optional[str]
        parent_run_id: Optional[str] = None
        chain_depth: int = 0
        review_reason: Optional[str] = None
        reviewed_at: Optional[str] = None
        reviewed_by: Optional[str] = None

    # ------------------------------------------------------------------
    # Pydantic models — Trigger CRUD
    # ------------------------------------------------------------------
    # ``TriggerCreateRequest`` / ``TriggerUpdateRequest`` (request bodies for the
    # trigger CRUD routes) moved with those routes into
    # ``routers/triggers.py::register_trigger_routes`` (Issue #942, 952c).
    # ``TriggerResponse`` (the response model referenced in route docstrings)
    # stays here alongside the other response models.

    class TriggerResponse(BaseModel):
        """Serialised trigger record returned by the API.

        Note:
            The ``secret`` field is always redacted to ``'***'`` in responses.
            Secrets are write-only.
        """

        id: str
        template_id: str
        mode: str
        secret: Optional[str]
        """Always ``'***'`` when a secret is configured, ``None`` otherwise."""
        rate_limit: int
        input_map: Dict[str, Any]
        filters: List[Dict[str, Any]]
        enabled: bool
        created_at: Optional[str]

    # ``_trigger_to_response`` (redact-secret → TriggerResponse-dict helper) moved
    # with the trigger CRUD + webhook routes into
    # ``routers/triggers.py::register_trigger_routes`` (Issue #942, 952c) — it is
    # used only by those routes.

    # ------------------------------------------------------------------
    # Helper — build RunResponse dict from a DB row
    # ------------------------------------------------------------------

    def _run_to_dict(run: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB pipeline_runs row dict to a RunResponse-compatible dict."""
        # completed_phases is stored as a JSON string in the DB — defer to
        # the canonical parser in db.py (Issue #866).
        completed_phases = parse_json_list(run.get("completed_phases"))

        return {
            "run_id": run["run_id"],
            "template_id": run.get("template_id", ""),
            "template_path": run.get("template_path", ""),
            "mode": run.get("mode", ""),
            "status": run.get("status", ""),
            "current_phase": run.get("current_phase"),
            "completed_phases": completed_phases,
            "pid": run.get("pid"),
            "output_dir": run.get("output_dir", ""),
            "error_message": run.get("error_message"),
            "gateway_url": run.get("gateway_url"),
            "skip_scoring": bool(run.get("skip_scoring", 0)),
            "scoring_status": run.get("scoring_status"),
            "scoring_score": run.get("scoring_score"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "created_at": run.get("created_at"),
            "parent_run_id": run.get("parent_run_id"),  # Issue #330.3: chaining parent
            "chain_depth": int(run.get("chain_depth") or 0),  # Issue #330.3: chaining depth
            "review_reason": run.get("review_reason"),  # Issue #331.4: review queue
            "reviewed_at": run.get("reviewed_at"),  # Issue #331.4: review queue
            "reviewed_by": run.get("reviewed_by"),  # Issue #331.4: review queue
        }

    # ------------------------------------------------------------------
    # Helper — classify and resolve writable template paths
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helper — resolve template name or path
    # ------------------------------------------------------------------

    def _resolve_template(name_or_path: str) -> Path:
        """Resolve a template name or file path to an absolute Path.

        Resolution strategy (mirrors cli.py ``_resolve_template_arg``):
        1. Direct file path (has .yaml/.yml extension or path separators).
        2. File-stem resolution via ``TemplateEngine.resolve_template``.
        3. Template-ID scan (handles cases where the YAML ``id`` field
           differs from the file stem, e.g. ``content-pipeline-v24`` in
           ``templates/content-pipeline.yaml``).

        Raises HTTPException(404) when not found.
        """
        looks_like_path = (
            name_or_path.endswith(".yaml")
            or name_or_path.endswith(".yml")
            or os.sep in name_or_path
            or "/" in name_or_path
        )

        if looks_like_path:
            p = Path(name_or_path)
            if not p.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"Template file not found: {name_or_path}",
                )
            # Sandbox check: ensure path is within allowed template directories
            engine = _make_engine()
            resolved = p.resolve()
            allowed = any(
                resolved.is_relative_to(d.resolve()) for d, _ in engine.get_search_paths()
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Path outside template directories",
                )
            return p

        engine = _make_engine()

        # 1. File-stem resolution (fast path)
        try:
            return engine.resolve_template(name_or_path)
        except TemplateNotFoundError:
            pass

        # 2. Scan all templates and match by template ID
        for entry in engine.list_templates():
            if entry["id"] == name_or_path:
                return Path(entry["path"])

        raise HTTPException(
            status_code=404,
            detail=f"Template '{name_or_path}' not found",
        )

    # ------------------------------------------------------------------
    # Helper — launch a pipeline run (extracted for reuse by webhook route)
    # ------------------------------------------------------------------

    def _launch_pipeline_from_trigger(
        template_file: Path,
        template: Any,
        input_data: Dict[str, Any],
        mode: str,
        gateway_url: Optional[str],
        db: Any,
        skip_scoring: bool = False,
        output_dir_override: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Core pipeline launch logic: DB row + daemon spawn + return run dict.

        Encapsulates the shared logic used by both ``POST /api/v1/runs`` and
        ``POST /api/v1/webhooks/{trigger_id}`` so neither handler duplicates
        it.

        Args:
            template_file: Resolved absolute path to the template YAML.
            template: Loaded and validated template object.
            input_data: Pipeline input variables dict.
            mode: Execution mode (``'standalone'``, ``'openclaw'``,
                ``'dry-run'``).
            gateway_url: OpenClaw gateway URL (or ``None``).
            db: Open :class:`~orchestration_engine.db.Database` instance.
            skip_scoring: Skip auto-scoring.  Default ``False``.
            output_dir_override: Optional explicit output directory path.

        Returns:
            A :class:`RunResponse`-shaped dict for the newly created run.

        Raises:
            HTTPException(429): When ``ORCH_MAX_DAEMONS`` active runs are
                already executing (#839 backpressure). The launcher does
                NOT spawn another daemon process while this many are
                already in flight — unbounded concurrent daemons trip
                SQLite WAL contention and produce zombie runs (#754).
        """
        # ── Backpressure check (#839) ────────────────────────────────
        # Default cap matches the empirical safe ceiling for SQLite WAL
        # on a 4-core dev laptop; production deployments raise it via
        # the env var. A value of 0 disables the cap entirely (legacy
        # behaviour). We read the env var on every launch so an operator
        # can tune live without restarting the server.
        _max_daemons = env_int(os.environ.get("ORCH_MAX_DAEMONS"), 8)
        if _max_daemons > 0:
            _active = db.count_active_pipeline_runs()
            if _active >= _max_daemons:
                logger.warning(
                    "Launch rejected (#839 backpressure): %d active runs "
                    ">= ORCH_MAX_DAEMONS=%d. Wait for in-flight runs to "
                    "complete or raise the cap.",
                    _active,
                    _max_daemons,
                )
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Backpressure: {_active} pipeline runs already active "
                        f"(cap ORCH_MAX_DAEMONS={_max_daemons}). Wait for "
                        f"in-flight runs to complete or raise the cap."
                    ),
                    headers={"Retry-After": "30"},
                )

        run_id = str(uuid.uuid4())[:8]
        if output_dir_override:
            output_dir = Path(output_dir_override)
        else:
            _safe_id = re.sub(r"[^\w\-]", "_", template.id)
            _ts = _now_utc().strftime("%Y%m%d-%H%M%S")
            output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")
        output_dir.mkdir(parents=True, exist_ok=True)

        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": str(template_file.resolve()),
                "template_id": template.id,
                "input_json": json.dumps(input_data),
                "mode": mode,
                "output_dir": str(output_dir.resolve()),
                "gateway_url": gateway_url,
                "skip_scoring": int(skip_scoring),
                "status": "pending",
            }
        )

        log_file_path = output_dir / ".orch-daemon.log"
        daemon_env = {**os.environ, **(extra_env or {})}
        with open(str(log_file_path), "a") as log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "orchestration_engine.daemon",
                    run_id,
                    effective_db_path,
                ],
                start_new_session=True,
                stdout=log_fh,
                stderr=log_fh,
                env=daemon_env,
            )

        db.update_pipeline_run(run_id, pid=proc.pid)

        run = db.get_pipeline_run(run_id)
        return _run_to_dict(run)

    # ------------------------------------------------------------------
    # Routes — every group is registered via the register-function pattern
    # (Issue #942, 952b/952c/952d). No ``@app.<verb>`` closures remain inline
    # in this factory; each ``register_*_routes`` call receives the exact
    # objects its moved closures referenced from this scope.
    # ------------------------------------------------------------------

    # ---- Health probes (Issue #942, 952d: extracted to routers/) ------------
    # The server-health + regression-webhook-health closures moved verbatim into
    # routers/health.py via the register-function pattern; __version__ and the
    # Database class are passed in (the closures open their own Database on
    # effective_db_path), so behaviour is identical.
    register_health_routes(
        app,
        JSONResponse=JSONResponse,
        Database=Database,
        __version__=__version__,
        effective_db_path=effective_db_path,
    )

    # ---- Templates + Phases (Issue #942, 952b: extracted to routers/) -------
    # Route closures moved verbatim into routers/templates.py + routers/phases.py
    # via the register-function pattern; every object the closures referenced
    # from this factory scope is passed explicitly so behaviour is identical.
    register_template_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Response=Response,
        TemplateEngine=TemplateEngine,
        TemplateNotFoundError=TemplateNotFoundError,
        _make_engine=_make_engine,
        _resolve_template=_resolve_template,
    )
    register_phase_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        TemplateNotFoundError=TemplateNotFoundError,
        _make_engine=_make_engine,
    )

    # ---- Pipeline Runs + SSE stream (Issue #942, 952b: extracted to routers/) -
    # POST/GET/children/logs/artifacts/phase0/dialogue/stream/delete moved
    # verbatim into routers/runs.py via the register-function pattern. They are
    # all registered here (where POST /api/v1/runs used to be defined); the
    # webhook/trigger/review/cost/... routes that historically interleaved with
    # them stay inline below. All route paths are mutually distinct (no literal
    # vs path-param shadowing at any depth+method), so registration order is
    # behaviour-neutral here; the SSE stream closes over the SAME _SSE_LIMITER
    # instance, preserving the admit/release accounting exactly.
    register_run_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Request=Request,
        EventSourceResponse=EventSourceResponse,
        Database=Database,
        TERMINAL_STATUSES=TERMINAL_STATUSES,
        TemplateEngine=TemplateEngine,
        asyncio=asyncio,
        effective_db_path=effective_db_path,
        _normalize_ts=_normalize_ts,
        _resolve_template=_resolve_template,
        _run_to_dict=_run_to_dict,
        _launch_pipeline_from_trigger=_launch_pipeline_from_trigger,
        _sse_limiter=_SSE_LIMITER,
    )

    # ---- Triggers + webhooks (Issue #942, 952c: extracted to routers/) -------
    # The incoming-webhook receiver + the trigger CRUD closures moved verbatim
    # into routers/triggers.py via the register-function pattern; every object
    # the closures referenced from this factory scope (framework classes, the
    # Database/TemplateEngine classes, the webhook helpers, the per-call
    # effective_db_path, and the shared _resolve_template /
    # _launch_pipeline_from_trigger helpers) is passed explicitly so behaviour is
    # identical. The TriggerCreateRequest/TriggerUpdateRequest bodies and the
    # _trigger_to_response helper moved with them.
    register_trigger_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Request=Request,
        Response=Response,
        Database=Database,
        TemplateEngine=TemplateEngine,
        InputMapper=InputMapper,
        TriggerMatcher=TriggerMatcher,
        SlidingWindowRateLimiter=SlidingWindowRateLimiter,
        _verify_github_signature=_verify_github_signature,
        _apply_input_map=_apply_input_map,
        effective_db_path=effective_db_path,
        _resolve_template=_resolve_template,
        _launch_pipeline_from_trigger=_launch_pipeline_from_trigger,
    )

    # ---- Harness aggregate read endpoints (Issue #942, 952d: extracted to routers/)
    # The /regressions, /stale-findings, /trust-profiles and /decisions read
    # closures (items 4, 6, 7 from the post-0.10 audit) moved verbatim into
    # routers/regressions.py via the register-function pattern. They are a
    # cohesive group: pure DB reads with pagination caps that back the harness
    # Fleet Dashboard + Trust & Gates side-panel cards. The Database class and
    # per-call effective_db_path are passed in; the _normalize_row helper (used
    # ONLY by these routes) moved with them and is imported there directly from
    # ``orchestration_engine.timestamps`` — the same module this factory imported
    # it from (#876). The /decisions endpoint reuses the `review_outcomes` table
    # (each row IS a decision: APPROVE / REQUEST_CHANGES by a specific reviewer at
    # a specific time) so no new table is needed.
    register_regression_routes(
        app,
        JSONResponse=JSONResponse,
        Database=Database,
        effective_db_path=effective_db_path,
    )

    # ---- Admin console + SSE metrics (Issue #942, 952d: extracted to routers/)
    # The /admin/state, /admin/feature-flags, /admin/audit-log and /sse/metrics
    # closures moved verbatim into routers/admin.py via the register-function
    # pattern. The admin-doc helpers (_ADMIN_DEFAULTS, _ADMIN_KNOWN_FLAGS,
    # _strict_coerce_bool, _coerce_admin_doc, _merge_feature_flags_with_passthrough)
    # live at this package's module scope (admin_flags/schemas) so they can be
    # unit-tested directly; they are passed in here — the same objects the inline
    # closures referenced. The /sse/metrics probe rides with the admin routes
    # (same Admin Console surface) and closes over the SAME _SSE_LIMITER instance,
    # preserving the admit/release accounting. The lazy in-closure
    # ``from ... import feature_flags`` becomes the equivalent absolute import in
    # the router (identical module). ``logger`` is passed so the audit-append
    # warning records carry the same logger name as before.
    #
    # Concurrency model (no asyncio.Lock — round-3 simplification):
    # - PUT /admin/feature-flags has zero `await` points inside its
    #   read-modify-write critical section, so FastAPI's single-event-loop
    #   scheduling already serialises it.
    # - `os.replace()` is atomic on POSIX, so the on-disk file is always
    #   well-formed (last-writer-wins for cross-process writers).
    register_admin_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Request=Request,
        Database=Database,
        effective_db_path=effective_db_path,
        _ADMIN_DEFAULTS=_ADMIN_DEFAULTS,
        _ADMIN_KNOWN_FLAGS=_ADMIN_KNOWN_FLAGS,
        _strict_coerce_bool=_strict_coerce_bool,
        _coerce_admin_doc=_coerce_admin_doc,
        _merge_feature_flags_with_passthrough=_merge_feature_flags_with_passthrough,
        _sse_limiter=_SSE_LIMITER,
        logger=logger,
    )

    # ------------------------------------------------------------------
    # Pydantic models — Review Queue (Issue #331.4)
    # ------------------------------------------------------------------

    class ReviewResponse(BaseModel):
        """Serialised pending-review run record (pipeline run + routing decision)."""

        run_id: str
        template_id: str
        status: str
        created_at: Optional[str]
        completed_at: Optional[str]
        review_reason: Optional[str]
        reviewed_at: Optional[str]
        reviewed_by: Optional[str]
        confidence_score: Optional[float]
        tier_name: Optional[str]
        action: Optional[str]
        justification: Optional[str]

    # ``ApproveRequest`` / ``RejectRequest`` (request bodies) and the
    # ``_review_row_to_dict`` helper moved with the review-queue routes into
    # ``routers/reviews.py::register_review_routes`` (Issue #942, 952c) — they are
    # used only by those routes.  ``ReviewResponse`` (referenced in route
    # docstrings) stays here alongside the other response models.

    # ------------------------------------------------------------------
    # Review Queue endpoints (Issue #331.4)
    # ------------------------------------------------------------------

    # ---- Review queue (Issue #942, 952c: extracted to routers/) -------------
    # The list/approve/reject closures moved verbatim into routers/reviews.py via
    # the register-function pattern; the shared _run_to_dict helper is passed in
    # (it is also used by routes that stay here and in routers/runs.py). The
    # ApproveRequest/RejectRequest bodies and the _review_row_to_dict helper moved
    # with them.
    register_review_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Database=Database,
        effective_db_path=effective_db_path,
        _run_to_dict=_run_to_dict,
    )

    # ------------------------------------------------------------------
    # Cost API endpoints (Issue #5.2.3)
    # ------------------------------------------------------------------

    # The summary/run-breakdown closures (and the _VALID_GROUP_BY set + _DATE_RE
    # regex they validate against) moved verbatim into routers/costs.py via the
    # register-function pattern (Issue #942, 952c).
    register_cost_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Database=Database,
        effective_db_path=effective_db_path,
    )

    # ------------------------------------------------------------------
    # Trust profile API endpoints (Issue #4.2.4)
    # ------------------------------------------------------------------

    # The list/detail/override/adjustments closures (and the TrustOverrideRequest
    # body for the override route) moved verbatim into routers/trust.py via the
    # register-function pattern (Issue #942, 952c).
    register_trust_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Database=Database,
        effective_db_path=effective_db_path,
    )

    # ---- Telegram HITL callback (Issue #942, 952d: extracted to routers/) ----
    # The /telegram/callback closure (Issue #429.5) moved verbatim into
    # routers/telegram.py via the register-function pattern; the per-call
    # effective_db_path and the module logger are passed in, and the lazy
    # in-closure ``from orchestration_engine.notifications import
    # TelegramCallbackHandler`` is preserved exactly (so the test patch on that
    # module path still intercepts at call time).
    register_telegram_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Request=Request,
        effective_db_path=effective_db_path,
        logger=logger,
    )

    # ---- GitHub Issues webhooks (Issue #942, 952d: extracted to routers/) ----
    # The /github/issues (Issue #5.1.3) and /github/issues/pipeline-ready
    # (Issue #511) closures moved verbatim into routers/github.py via the
    # register-function pattern. Every lazy in-closure import (issue_automation
    # helpers, NotificationDispatcher, get_global_config) is preserved exactly,
    # spelled absolutely against the identical module so the test patches still
    # intercept at call time; generate_pipeline_input (no test patches it) is
    # imported at that module's scope. The Database/TemplateEngine classes, the
    # per-call effective_db_path, the shared _verify_github_signature /
    # _resolve_template / _launch_pipeline_from_trigger helpers, and the module
    # logger are passed in — the same objects the inline closures used.
    register_github_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        Request=Request,
        Database=Database,
        TemplateEngine=TemplateEngine,
        effective_db_path=effective_db_path,
        _verify_github_signature=_verify_github_signature,
        _resolve_template=_resolve_template,
        _launch_pipeline_from_trigger=_launch_pipeline_from_trigger,
        logger=logger,
    )

    # ---- Direct issue launch (Issue #942, 952d: extracted to routers/) -------
    # The /issues/launch closure (Issue #632) moved verbatim into
    # routers/issues.py via the register-function pattern; the IssueLaunchRequest
    # body moved with it (defined inside the register fn with a lazy pydantic
    # import, preserving the optional [api] extra contract). The shared
    # _resolve_template helper is passed in; generate_pipeline_input is imported
    # at that module's scope (no test patches it).
    register_issue_routes(
        app,
        JSONResponse=JSONResponse,
        HTTPException=HTTPException,
        _resolve_template=_resolve_template,
    )

    # ---- Merge Gate endpoints (Issue #942, 952c: extracted to routers/) ------
    # The list/detail/approve/reject closures (and the GateApproveRequest /
    # GateRejectRequest bodies) moved verbatim into routers/gates.py via the
    # register-function pattern. These routes operate on gate state via
    # GitContext (no Database), so register_gate_routes takes only the framework
    # HTTPException it raises.
    register_gate_routes(
        app,
        HTTPException=HTTPException,
    )

    return app
