"""FastAPI REST API app factory for the Orchestration Engine (Issue #257).

This module holds :func:`create_api_app`, the FastAPI app factory, with ALL of
its inner route closures kept verbatim. It is part of the ``web/api/`` package
created by the facade-preserving decomposition of the former ``web/api.py``
god-module (Issue #942, sub-issue 952a). The closure-free module-level members
were extracted into sibling modules and are imported below; the public surface
is re-exported from the package :mod:`orchestration_engine.web.api` facade.

Route extraction (converting the inner ``@app.<verb>`` closures into
``APIRouter`` modules) is deliberately deferred to sub-issues 952b-d.

All dependencies (fastapi, uvicorn) are optional extras — import this module
only after confirming they are installed.
"""

import hmac
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
from orchestration_engine.issue_automation import generate_pipeline_input

from .admin_flags import _ADMIN_DEFAULTS, _ADMIN_KNOWN_FLAGS
from .deps import _get_persistent_db_path
from .routers.costs import register_cost_routes
from .routers.gates import register_gate_routes
from .routers.phases import register_phase_routes
from .routers.reviews import register_review_routes
from .routers.runs import register_run_routes
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
        normalize_row as _normalize_row,
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
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/v1/health")
    async def health() -> JSONResponse:
        """Return API server health status."""
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/api/v1/health/webhook")
    async def webhook_health() -> JSONResponse:  # noqa: C901
        """Return health status for the regression CI webhook trigger.

        Checks two things:

        1. **DB presence** — whether the regression trigger row exists in the
           ``triggers`` table.
        2. **GitHub presence** — (best-effort) whether the GitHub-side webhook
           still exists by calling ``gh api repos/<repo>/hooks`` and filtering
           by payload URL.  If the ``gh`` CLI is unavailable or the call fails,
           only the DB information is reported.

        The trigger ID is read from the ``REGRESSION_TRIGGER_ID`` environment
        variable (default ``"regression-ci-trigger"``).  As a fallback, the
        endpoint scans for any trigger whose ``template_id`` matches a known
        regression template ID (``regression-pipeline-v1``).

        Returns:
            JSON object with the following fields:

            ``trigger_registered`` (bool)
                ``True`` when the trigger row exists in the DB.
            ``trigger_id`` (str | null)
                The trigger ID that was checked (or ``null`` when not found
                via fallback scan).
            ``github_webhook_id`` (int | null)
                The GitHub webhook ID, or ``null`` when the GitHub check was
                skipped or the hook was not found.
            ``github_webhook_active`` (bool | null)
                Whether the GitHub webhook is marked active, or ``null``
                when the GitHub check was skipped.
            ``status`` (str)
                ``"ok"`` — trigger registered and GitHub hook active.
                ``"degraded"`` — trigger registered but GitHub check skipped or
                    hook not found.
                ``"error"`` — trigger is not registered in the DB.
        """
        _KNOWN_REGRESSION_TEMPLATE_IDS = {  # noqa: N806
            "regression-pipeline-v1",
            "regression-fix-pipeline-v1",
        }
        _REGRESSION_WEBHOOK_PATH_SUFFIX = "/api/v1/webhooks/"  # noqa: N806

        # 1. Determine which trigger ID to look up
        trigger_id = os.environ.get("REGRESSION_TRIGGER_ID", "regression-ci-trigger")
        db = Database(Path(effective_db_path))
        trigger_row = db.get_trigger(trigger_id)

        # Fallback: scan for any trigger whose template_id matches a known regression template
        if trigger_row is None:
            all_triggers = db.list_triggers(limit=200)
            for row in all_triggers:
                if row.get("template_id") in _KNOWN_REGRESSION_TEMPLATE_IDS:
                    trigger_row = row
                    trigger_id = row["id"]
                    break

        trigger_registered = trigger_row is not None
        found_trigger_id: Optional[str] = trigger_id if trigger_registered else None

        # 2. Attempt GitHub-side check (best-effort, graceful failure)
        github_webhook_id: Optional[int] = None
        github_webhook_active: Optional[bool] = None

        if trigger_registered:
            try:
                gh_result = subprocess.run(
                    ["gh", "api", "repos/ToscanAI/orchestration-engine/hooks"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if gh_result.returncode == 0 and gh_result.stdout.strip():
                    hooks = json.loads(gh_result.stdout)
                    if isinstance(hooks, list):
                        for hook in hooks:
                            hook_url = (hook.get("config") or {}).get("url", "")
                            if (
                                _REGRESSION_WEBHOOK_PATH_SUFFIX in hook_url
                                and trigger_id in hook_url
                            ):
                                github_webhook_id = hook.get("id")
                                github_webhook_active = bool(hook.get("active"))
                                break
            except (
                FileNotFoundError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                Exception,  # noqa: BLE001
            ):
                # gh unavailable or any other failure — skip GitHub check gracefully
                pass

        # 3. Determine overall status
        if not trigger_registered:
            overall_status = "error"
        elif github_webhook_id is not None and github_webhook_active:
            overall_status = "ok"
        else:
            # Trigger registered but GitHub check skipped or hook not found
            overall_status = "degraded"

        return JSONResponse(
            {
                "trigger_registered": trigger_registered,
                "trigger_id": found_trigger_id,
                "github_webhook_id": github_webhook_id,
                "github_webhook_active": github_webhook_active,
                "status": overall_status,
            }
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

    # ── Harness aggregate endpoints (items 4, 6, 7 from the post-0.10 audit) ──
    # These three close the read-side data gaps the harness was rendering as
    # demo content. They are pure DB reads with reasonable pagination caps —
    # no writes (with one exception, see /admin/feature-flags below).
    #
    # The trust + regression endpoints unblock the Trust & Gates side panel
    # and the Fleet Dashboard regression queue. The /decisions endpoint reuses
    # the `review_outcomes` table (each row IS a decision: APPROVE / REQUEST_CHANGES
    # by a specific reviewer at a specific time) so we don't need a new table.

    # ``_normalize_ts`` / ``_normalize_row`` live in ``orchestration_engine.timestamps``
    # (imported at the top of this function). The closure that previously held
    # both helpers was hoisted to a module so ``db.py`` could share the same
    # UTC-tagging logic (#876).

    @app.get("/api/v1/regressions")
    async def list_regressions_endpoint(
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        """List regression records (from `regressions` table, newest first).

        Backs the Fleet Dashboard "Regression queue" card. Returns the
        canonical engine shape with timestamps normalised to UTC `Z` strings.

        Optional ``status`` filter (e.g. ``'detected'``, ``'fixing'``,
        ``'resolved'``). Limit clamped to [1, 200].
        """
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        # NOTE on consistency: `_locked()` is a no-op for file-based DBs
        # (production); SQLite's default isolation gives us autocommit
        # snapshot semantics per statement. To make items + total truly
        # consistent we wrap both in an explicit BEGIN DEFERRED ... COMMIT
        # transaction so they share one snapshot. The transaction is
        # read-only, so contention with writers is minimal.
        base = "SELECT * FROM regressions"
        where: List[str] = []
        params: List[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        wclause = (" WHERE " + " AND ".join(where)) if where else ""
        # Stable secondary sort by id — `created_at` has 1-second resolution
        # and adjacent rows often share a timestamp, so without a tiebreaker
        # offset-based pagination skips or repeats rows.
        list_q = base + wclause + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        count_q = "SELECT COUNT(*) FROM regressions" + wclause
        with db._locked():
            conn = db.get_connection()
            try:
                conn.execute("BEGIN DEFERRED")
                rows = conn.execute(list_q, params + [limit, offset]).fetchall()
                total = conn.execute(count_q, params).fetchone()[0]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse(
            {
                "items": items,
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/stale-findings")
    async def list_stale_findings_endpoint() -> JSONResponse:
        """List stale-detection findings (ROADMAP §3.5).

        Returns an empty list until the stale scanner ships (it's the
        last open Phase-3 item in ROADMAP.md). The endpoint exists today
        so the harness Fleet Dashboard stale card can hit a real URL and
        get an empty list with a status marker, instead of hardcoding
        demo data forever.

        Response shape mirrors `/api/v1/regressions` for consistency.
        """
        return JSONResponse(
            {
                "items": [],
                "total": 0,
                "scan_status": "no_scanner_yet",
                "next_scan_at": None,
            }
        )

    @app.get("/api/v1/trust-profiles")
    async def list_trust_profiles_endpoint() -> JSONResponse:
        """Return all trust calibration profiles.

        Backs the Trust & Gates side panel. Profiles are keyed by
        (repo, template_id, task_type) per `trust.py` — the harness
        renders the key as a single composed label and the confidence
        as a bar relative to the threshold.

        Ordered ``last_run_at DESC NULLS LAST`` so the most-recently-active
        profiles surface first when the side panel slices the top N.

        No pagination — the active profile set is bounded by the number
        of (repo, template, task) tuples in use, typically O(10s).
        """
        db = Database(Path(effective_db_path))
        with db._locked():
            conn = db.get_connection()
            rows = conn.execute(
                "SELECT * FROM trust_profiles "
                "ORDER BY (last_run_at IS NULL), last_run_at DESC, id ASC"
            ).fetchall()
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse({"items": items, "total": len(items)})

    @app.get("/api/v1/decisions")
    async def list_decisions_endpoint(limit: int = 50, offset: int = 0) -> JSONResponse:
        """List recent review outcomes ('decisions') for the audit trail.

        Backs the Trust & Gates "Recent decisions" card. Each row is one
        APPROVE / REQUEST_CHANGES verdict on one run, recorded in the
        `review_outcomes` table by the engine when a reviewer phase
        completes.

        Returns the canonical `review_outcomes` row shape: ``review_id``,
        ``run_id``, ``phase_id``, ``reviewer_model``, ``verdict``,
        ``issues_found`` (list of dicts), ``fix_verified`` (0/1), and
        ``created_at`` (UTC `Z` string). Limit clamped to [1, 100].

        Ordered by ``created_at DESC, review_id DESC`` for stable pagination.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        # See `/regressions` for the same BEGIN DEFERRED rationale —
        # share one read snapshot across items + total.
        with db._locked():
            conn = db.get_connection()
            try:
                conn.execute("BEGIN DEFERRED")
                rows = conn.execute(
                    "SELECT * FROM review_outcomes "
                    "ORDER BY created_at DESC, review_id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM review_outcomes").fetchone()[0]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse(
            {
                "items": items,
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }
        )

    # ── Admin config defaults + helpers ─────────────────────────────────────
    # Helpers (_ADMIN_DEFAULTS, _strict_coerce_bool, _coerce_admin_doc,
    # _merge_feature_flags_with_passthrough) are now at module scope above
    # `create_api_app` so they can be tested directly. See the module-scope
    # docstrings for invariants.
    #
    # Concurrency model (no asyncio.Lock — round-3 simplification):
    # - PUT /admin/feature-flags has zero `await` points inside its
    #   read-modify-write critical section, so FastAPI's single-event-loop
    #   scheduling already serialises it.
    # - `os.replace()` is atomic on POSIX, so the on-disk file is always
    #   well-formed (last-writer-wins for cross-process writers).

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

        from ... import feature_flags as _ff  # noqa: PLC0415

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

        from ... import feature_flags as _ff  # noqa: PLC0415

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

    # ── SSE connection limits (#841) ─────────────────────────────────────
    # The limiter lives at module scope (see SseConnectionLimiter below)
    # so it's directly importable from tests and shared across requests
    # served by the same process. Limits are read from env vars on every
    # admit, so operators can re-tune live without restarting.
    _sse_limiter = _SSE_LIMITER  # alias for the closure-using stream handler

    @app.get("/api/v1/sse/metrics")
    async def get_sse_metrics() -> JSONResponse:
        """Return current SSE connection counts and limits (#841).

        Surfaced by the harness Admin Console + by ops dashboards. The
        ``active_total`` and ``active_per_ip`` snapshots are read under
        the lock; the limits are re-read from env vars on every call
        (operator may have re-tuned live).
        """
        return JSONResponse(_sse_limiter.metrics())

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

    # ------------------------------------------------------------------
    # Telegram HITL Callback Endpoint (Issue #429.5)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # GitHub Issues Webhook (Issue #5.1.3)
    # ------------------------------------------------------------------

    @app.post("/api/v1/github/issues", status_code=202)
    async def handle_github_issues(request: Request) -> JSONResponse:  # noqa: C901
        """Receive GitHub ``issues`` webhook events and launch pipelines automatically.

        Triggered when a GitHub issue is **opened** or **labeled** with the
        ``orchemist`` trigger label (configurable via the ``ISSUE_TRIGGER_LABEL``
        environment variable, default ``"orchemist"``).

        Flow:

        1. Validate that the ``X-GitHub-Event`` header is ``"issues"``.
        2. Filter for ``action == "opened"`` or ``action == "labeled"``.
        3. Check that the orchemist trigger label is present/applied.
        4. **Deduplication** — call
           :meth:`~orchestration_engine.db.Database.get_active_issue_run`;
           skip if an active pipeline run already exists for this issue.
        5. Classify, select template, extract inputs, and launch via
           :class:`~orchestration_engine.issue_automation.IssueAutomation`.
        6. Post a GitHub comment summarising the launched run via
           :func:`~orchestration_engine.issue_automation.post_github_comment`.

        Request headers:
            X-GitHub-Event (str): Must be ``"issues"`` (other values are ignored).

        Returns:
            - **200** when the event was ignored (wrong event type, wrong
              action, missing label, or duplicate run already active).
            - **202** when the pipeline was accepted and launched (or attempted).
            - **400** when the request body is not valid JSON or required
              payload fields are missing.
        """
        from orchestration_engine.issue_automation import (  # noqa: PLC0415
            InputExtractor,
            IssueAutomation,
            IssueClassifier,
            TemplateSelector,
            post_github_comment,
        )
        from orchestration_engine.notifications import NotificationDispatcher  # noqa: PLC0415

        # 1. Validate event type header
        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "issues":
            return JSONResponse(
                {"status": "ignored", "reason": "not_issues_event"},
                status_code=200,
            )

        # 1b. Read body bytes once — reused for signature verification and JSON parsing
        _body_bytes = await request.body()

        # 1c. GitHub App webhook signature verification (opt-in)
        from orchestration_engine.config import get_global_config  # noqa: PLC0415

        cfg = get_global_config()
        if cfg.github_app and cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(cfg.github_app.webhook_secret, _body_bytes, sig_header):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Hub-Signature-256 header",
                )
        else:
            logger.warning(
                "GitHub App webhook_secret is not configured — "
                "POST /api/v1/github/issues is accepting unauthenticated requests."
            )

        # 2. Parse JSON body (reuse already-read bytes)
        try:
            payload: Dict[str, Any] = json.loads(_body_bytes) if _body_bytes else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        action = payload.get("action", "")

        # 3. Filter for relevant actions
        if action not in ("opened", "labeled"):
            return JSONResponse(
                {"status": "ignored", "reason": f"action_{action}_not_relevant"},
                status_code=200,
            )

        # 4. Extract issue and repo from payload
        issue = payload.get("issue", {}) or {}
        issue_number = issue.get("number")
        if not issue_number:
            raise HTTPException(
                status_code=400,
                detail="Missing issue.number in webhook payload",
            )

        repo_data = payload.get("repository", {}) or {}
        repo = repo_data.get("full_name", "")
        if not repo:
            raise HTTPException(
                status_code=400,
                detail="Missing repository.full_name in webhook payload",
            )

        # 5. Check for orchemist trigger label
        trigger_label = os.environ.get("ISSUE_TRIGGER_LABEL", "orchemist")

        if action == "labeled":
            # For "labeled" action, the label that was just applied is in payload["label"]
            applied_label = (payload.get("label") or {}).get("name", "")
            if applied_label != trigger_label:
                return JSONResponse(
                    {"status": "ignored", "reason": "label_not_trigger"},
                    status_code=200,
                )
        elif action == "opened":
            # For "opened" action, check the issue already carries the trigger label
            issue_labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]
            if trigger_label not in issue_labels:
                return JSONResponse(
                    {"status": "ignored", "reason": "trigger_label_absent"},
                    status_code=200,
                )

        # 6. Deduplication — skip if there is already an active pipeline for this issue
        db = Database(Path(effective_db_path))
        active_run = db.get_active_issue_run(issue_number, repo)
        if active_run is not None:
            return JSONResponse(
                {
                    "status": "skipped",
                    "reason": "active_run_exists",
                    "run_id": active_run.get("run_id"),
                },
                status_code=200,
            )

        # 7. Build automation and process
        classifier = IssueClassifier()  # stub mode; replace executor via subclass/config
        selector = TemplateSelector()
        extractor = InputExtractor()
        try:
            confidence_threshold = float(
                os.environ.get("ISSUE_CLASSIFY_CONFIDENCE_THRESHOLD", "0.70")
            )
        except (TypeError, ValueError):
            confidence_threshold = 0.70
        dispatcher = NotificationDispatcher.from_env()
        automation = IssueAutomation(
            classifier=classifier,
            selector=selector,
            extractor=extractor,
            confidence_threshold=confidence_threshold,
            notification_dispatcher=dispatcher,
        )

        title = issue.get("title", "") or ""
        body_text = issue.get("body", "") or ""
        issue_label_names = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]

        engine_instance = TemplateEngine()
        gw_url = os.environ.get("OPENCLAW_GATEWAY_URL")

        result = automation.process(
            issue_number=issue_number,
            repo=repo,
            title=title,
            body=body_text,
            labels=issue_label_names,
            db=db,
            launcher=_launch_pipeline_from_trigger,
            template_resolver=_resolve_template,
            template_engine=engine_instance,
            mode="standalone",
            gateway_url=gw_url,
        )

        # 8. Post GitHub comment (best-effort — errors are logged, not raised)
        comment_body = result.get("comment_body", "")
        comment_url: Optional[str] = None
        if comment_body:
            comment_url = post_github_comment(
                repo=repo,
                issue_number=issue_number,
                body=comment_body,
            )
        result["comment_url"] = comment_url

        return JSONResponse(result, status_code=202)

    # ------------------------------------------------------------------
    # GitHub Issues — pipeline-ready label trigger (Issue #511)
    # ------------------------------------------------------------------

    @app.post("/api/v1/github/issues/pipeline-ready", status_code=202)
    async def handle_github_issues_pipeline_ready(request: Request) -> JSONResponse:  # noqa: C901
        """Receive GitHub ``issues`` webhook events for the ``pipeline-ready`` label.

        Triggered when a GitHub issue is labeled with ``pipeline-ready``.

        Flow:

        1. Validate that ``X-GitHub-Event`` is ``"issues"``.
        2. Filter for ``action == "labeled"`` and label == ``"pipeline-ready"``.
        3. Dedup — skip if an active pipeline run already exists for this issue.
        4. Launch ``coding-pipeline-v1`` via daemon infrastructure.
        5. Remove the ``pipeline-ready`` label (best-effort).
        6. Post a comment with the run ID.
        7. Return 202 with ``run_id`` and ``branch_name``.

        Returns:
            - **200** when the event was ignored (wrong type, action, or label,
              or a duplicate run already exists).
            - **202** when the pipeline was launched.
            - **400** when the request body is invalid.
        """
        from orchestration_engine.issue_automation import (  # noqa: PLC0415
            post_github_comment,
            remove_github_label,
        )

        # 1. Validate event type header
        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "issues":
            return JSONResponse(
                {"status": "ignored", "reason": "not_issues_event"},
                status_code=200,
            )

        # 2. Read body
        _body_bytes = await request.body()

        # Signature verification (opt-in, same as handle_github_issues)
        from orchestration_engine.config import get_global_config  # noqa: PLC0415

        _cfg = get_global_config()
        if _cfg.github_app and _cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(
                _cfg.github_app.webhook_secret, _body_bytes, sig_header
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Hub-Signature-256 header",
                )

        # 3. Parse JSON
        try:
            payload: Dict[str, Any] = json.loads(_body_bytes) if _body_bytes else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        action = payload.get("action", "")

        # 4. Only process labeled events
        if action != "labeled":
            return JSONResponse(
                {"status": "ignored", "reason": f"action_{action}_not_relevant"},
                status_code=200,
            )

        # 5. Check label is pipeline-ready
        applied_label = (payload.get("label") or {}).get("name", "")
        if applied_label != "pipeline-ready":
            return JSONResponse(
                {"status": "ignored", "reason": "label_not_pipeline_ready"},
                status_code=200,
            )

        # 6. Extract issue and repo
        issue = payload.get("issue", {}) or {}
        issue_number = issue.get("number")
        if not issue_number:
            raise HTTPException(
                status_code=400,
                detail="Missing issue.number in webhook payload",
            )

        repo_data = payload.get("repository", {}) or {}
        repo = repo_data.get("full_name", "")
        if not repo:
            raise HTTPException(
                status_code=400,
                detail="Missing repository.full_name in webhook payload",
            )

        # 7. Dedup — skip if active run exists for this issue
        db = Database(Path(effective_db_path))
        active_run = db.get_active_issue_run(issue_number, repo)
        if active_run is not None:
            return JSONResponse(
                {
                    "status": "skipped",
                    "reason": "active_run_exists",
                    "run_id": active_run.get("run_id"),
                },
                status_code=200,
            )

        # 8. Build pipeline input
        title = issue.get("title", "") or ""
        body_text = issue.get("body", "") or ""
        pipeline_input = generate_pipeline_input(
            issue_number=issue_number,
            title=title,
            body=body_text,
            repo=repo,
        )
        branch_name = pipeline_input["branch_name"]

        # 9. Resolve and load template
        _default_tpl = os.environ.get("ORCH_DEFAULT_TEMPLATE") or "coding-pipeline-standard"
        try:
            template_file = _resolve_template(_default_tpl)
        except HTTPException:
            raise HTTPException(
                status_code=400,
                detail=f"Default template '{_default_tpl}' not found. "
                f"Set ORCH_DEFAULT_TEMPLATE to an available template name.",
            )

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        # 10. Launch pipeline
        gw_url = os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=pipeline_input,
            mode="standalone",
            gateway_url=gw_url,
            db=db,
        )
        run_id = run_dict["run_id"]

        # 11. Remove pipeline-ready label (best-effort)
        remove_github_label(repo, issue_number, "pipeline-ready")

        # 12. Post comment with run ID (best-effort)
        comment_body = (
            f"🤖 **Orchemist** detected `pipeline-ready` label and launched the coding pipeline.\n\n"  # noqa: E501
            f"**Branch:** `{branch_name}`\n"
            f"**Run ID:** `{run_id}`\n\n"
            f"Progress can be tracked via `orch status {run_id}`."
        )
        post_github_comment(repo=repo, issue_number=issue_number, body=comment_body)

        return JSONResponse(
            {"status": "accepted", "run_id": run_id, "branch_name": branch_name},
            status_code=202,
        )

    # ------------------------------------------------------------------
    # Direct Issue Launch REST Endpoint (Issue #632)
    # ------------------------------------------------------------------

    class IssueLaunchRequest(BaseModel):
        """Request body for POST /api/v1/issues/launch."""

        issue_number: int
        repo: str
        title: str = ""
        body: str = ""

    @app.post("/api/v1/issues/launch", status_code=201)
    async def launch_issue_pipeline(req: IssueLaunchRequest) -> JSONResponse:
        """Launch a pipeline for a GitHub issue via direct REST call.

        This endpoint provides programmatic issue pipeline launch without
        requiring a GitHub webhook. It respects the ``ORCH_DEFAULT_TEMPLATE``
        environment variable for template selection.

        Request body (JSON):
            issue_number (int): GitHub issue number.
            repo (str): Repository full name (e.g. ``owner/repo``).
            title (str): Issue title (optional).
            body (str): Issue body (optional).

        Returns:
            201 with run ID and branch name on success.
            400 when the configured default template cannot be resolved.
        """
        # Resolve template — read env var at call time (not import time) so
        # that monkeypatch.setenv in tests takes effect.
        _default_tpl = os.environ.get("ORCH_DEFAULT_TEMPLATE") or "coding-pipeline-standard"
        try:
            _resolve_template(_default_tpl)
        except HTTPException:
            raise HTTPException(
                status_code=400,
                detail=f"Default template '{_default_tpl}' not found. "
                f"Set ORCH_DEFAULT_TEMPLATE to an available template name.",
            )

        pipeline_input = generate_pipeline_input(
            issue_number=req.issue_number,
            title=req.title,
            body=req.body,
            repo=req.repo,
        )
        branch_name = pipeline_input["branch_name"]

        return JSONResponse(
            {
                "status": "accepted",
                "branch_name": branch_name,
                "template_id": _default_tpl,
            },
            status_code=201,
        )

    # ------------------------------------------------------------------
    # Merge Gate endpoints (#743)
    # ------------------------------------------------------------------

    # The list/detail/approve/reject closures (and the GateApproveRequest /
    # GateRejectRequest bodies) moved verbatim into routers/gates.py via the
    # register-function pattern (Issue #942, 952c). These routes operate on gate
    # state via GitContext (no Database), so register_gate_routes takes only the
    # framework HTTPException it raises.
    register_gate_routes(
        app,
        HTTPException=HTTPException,
    )

    return app
