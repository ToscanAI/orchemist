"""FastAPI REST API for the Orchestration Engine (Issue #257).

Provides a versioned JSON REST API backed by the same daemon-based
async execution infrastructure used by ``orch launch``.  This is
separate from ``web/app.py`` (browser UI) — it targets programmatic
consumers such as CI/CD pipelines, OpenClaw, and external scripts.

All dependencies (fastapi, uvicorn) are optional extras — import this
module only after confirming they are installed.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml

logger = logging.getLogger(__name__)


def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB used by async runs."""
    default_dir = Path.home() / ".orchestration-engine"
    default_dir.mkdir(exist_ok=True)
    return str(default_dir / "engine.db")


def _verify_github_signature(secret: str, payload_bytes: bytes, sig_header: Optional[str]) -> bool:
    """Verify an HMAC-SHA256 GitHub-style webhook signature.

    GitHub sends ``X-Hub-Signature-256: sha256=<hex_digest>``.  This function
    recomputes the HMAC-SHA256 of *payload_bytes* using *secret* and compares
    it to the digest in *sig_header* using a constant-time comparison to
    prevent timing attacks.

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


def _apply_input_map(payload: Dict[str, Any], input_map: Dict[str, Any]) -> Dict[str, Any]:
    """Transform a webhook payload dict into pipeline input vars using *input_map*.

    Each key in *input_map* becomes an input variable.  Values that start with
    ``"$."`` are treated as simple dot-path expressions into *payload*.  Other
    values are used as literals.

    Example::

        payload   = {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main"}
        input_map = {"repo": "$.repository.full_name", "branch": "$.ref", "env": "prod"}
        # result: {"repo": "org/repo", "branch": "refs/heads/main", "env": "prod"}

    Args:
        payload: Parsed webhook JSON body.
        input_map: Dict mapping pipeline variable names to payload paths or
            literal values.

    Returns:
        Dict of pipeline input variables.  Missing paths produce ``None``.
    """
    result: Dict[str, Any] = {}
    for var_name, path_or_literal in input_map.items():
        if isinstance(path_or_literal, str) and path_or_literal.startswith("$."):
            # Simple dot-path resolution: "$.a.b.c" → payload["a"]["b"]["c"]
            parts = path_or_literal[2:].split(".")
            value: Any = payload
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            result[var_name] = value
        else:
            result[var_name] = path_or_literal
    return result


def _check_rate_limit(trigger_id: str, rate_limit: int, db: Any) -> bool:
    """Check whether a webhook trigger has exceeded its per-minute rate limit.

    Args:
        trigger_id: The trigger identifier to check.
        rate_limit: Maximum allowed invocations per minute (0 = unlimited).
        db: :class:`~orchestration_engine.db.Database` instance.

    Returns:
        ``True`` when the rate limit is exceeded (caller should return 429).
        ``False`` when the invocation is allowed.
    """
    if rate_limit == 0:
        return False  # Unlimited
    since = datetime.now() - timedelta(seconds=60)
    count = db.count_webhook_invocations_since(trigger_id, since)
    return count >= rate_limit


class SlidingWindowRateLimiter:
    """A named, unit-testable sliding-window rate limiter for webhook triggers.

    Uses a 60-second sliding window backed by the ``webhook_invocations``
    table in the DB.  Delegates to the same logic as ``_check_rate_limit()``
    but wraps it in a proper class so it can be tested and extended
    independently.

    Example::

        limiter = SlidingWindowRateLimiter(db)
        if limiter.check(trigger_id, rate_limit):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    """

    def __init__(self, db: Any) -> None:
        """Initialise with a DB instance.

        Args:
            db: :class:`~orchestration_engine.db.Database` instance used to
                query invocation counts and record new invocations.
        """
        self._db = db

    def check(self, trigger_id: str, rate_limit: int) -> bool:
        """Return ``True`` if the rate limit is exceeded, ``False`` otherwise.

        Args:
            trigger_id: The trigger identifier to check.
            rate_limit: Maximum allowed invocations per minute (0 = unlimited).

        Returns:
            ``True`` when the caller should block the request (429).
            ``False`` when the invocation is allowed.
        """
        return _check_rate_limit(trigger_id, rate_limit, self._db)


def create_api_app(db_path: Optional[str] = None) -> "FastAPI":  # noqa: F821 (type hint only)
    """Create and return the REST API FastAPI application.

    Args:
        db_path: Path to the SQLite DB for pipeline_runs.  Defaults to the
                 same persistent DB used by ``orch launch``.

    Returns:
        Configured ``FastAPI`` instance.
    """
    import asyncio

    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    from sse_starlette.sse import EventSourceResponse

    from orchestration_engine import __version__
    from orchestration_engine.db import Database, TERMINAL_STATUSES
    from orchestration_engine.templates import TemplateEngine, TemplateNotFoundError
    from orchestration_engine.webhooks import InputMapper, TriggerMatcher

    effective_db_path = db_path or _get_persistent_db_path()

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
    # Pydantic request/response models
    # ------------------------------------------------------------------

    class LaunchRequest(BaseModel):
        """Body for POST /api/v1/runs — launch a new pipeline run."""

        template: str
        """Template name (resolved from search paths) or path to a .yaml file."""

        mode: Literal["standalone", "openclaw", "dry-run"] = "dry-run"
        """Execution mode passed to the daemon subprocess."""

        input: Dict[str, Any] = {}
        """Initial pipeline input (equivalent to ``--input`` / ``--input-file``)."""

        output_dir: Optional[str] = None
        """Directory to write phase outputs.  Auto-generated when omitted."""

        gateway_url: Optional[str] = None
        """OpenClaw gateway URL (openclaw mode).  Falls back to OPENCLAW_GATEWAY_URL."""

        skip_scoring: bool = False
        """Skip auto-scoring even if the template declares a scenario."""

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

    class TemplateCreateRequest(BaseModel):
        """Body for POST /api/v1/templates — create a new template."""

        content: str
        """Raw YAML content of the template.  Must include id, name, and at
        least one phase."""

        source: Literal["user", "project"] = "user"
        """Where to write the template.  ``'user'`` targets ``~/.orch/templates/``
        (default); ``'project'`` targets ``./templates/`` in the server's CWD.
        Bundled templates can never be written via the API."""

        overwrite: bool = False
        """Allow overwriting an existing template with the same ID.  Defaults to
        ``False`` — the request will fail with 409 when the file already exists
        and this is not set."""

    class TemplateValidateRequest(BaseModel):
        """Body for POST /api/v1/templates/validate — dry-run validate only."""

        content: str
        """Raw YAML content to validate without writing to disk."""

        extended: bool = True
        """Also run ``validate_template_extended()`` for deeper linting warnings.
        Defaults to ``True``."""

    class TemplateWriteResponse(BaseModel):
        """Response body returned after a successful create or update."""

        id: str
        name: str
        version: str
        path: str
        source: str
        phases_count: int
        created: bool
        """``True`` when a new file was written; ``False`` when an existing file
        was overwritten (update)."""

    # ------------------------------------------------------------------
    # Pydantic models — Trigger CRUD
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Helper — build RunResponse dict from a DB row
    # ------------------------------------------------------------------

    def _run_to_dict(run: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB pipeline_runs row dict to a RunResponse-compatible dict."""
        # completed_phases is stored as a JSON string in the DB
        completed_phases_raw = run.get("completed_phases", "[]")
        if isinstance(completed_phases_raw, str):
            try:
                completed_phases = json.loads(completed_phases_raw)
            except (json.JSONDecodeError, TypeError):
                completed_phases = []
        else:
            completed_phases = completed_phases_raw or []

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
            "parent_run_id": run.get("parent_run_id"),       # Issue #330.3: chaining parent
            "chain_depth": int(run.get("chain_depth") or 0), # Issue #330.3: chaining depth
            "review_reason": run.get("review_reason"),         # Issue #331.4: review queue
            "reviewed_at": run.get("reviewed_at"),             # Issue #331.4: review queue
            "reviewed_by": run.get("reviewed_by"),             # Issue #331.4: review queue
        }

    # ------------------------------------------------------------------
    # Helper — classify and resolve writable template paths
    # ------------------------------------------------------------------

    def _template_source(engine: "TemplateEngine", path: Path) -> str:  # type: ignore[name-defined]
        """Return the source label for an absolute template *path*.

        Compares *path* against each directory in ``engine.get_search_paths()``
        and returns the label of the first matching directory.  Falls back to
        ``"unknown"`` when the path does not belong to any search directory.

        Args:
            engine: A :class:`TemplateEngine` instance whose search paths are
                    used for comparison.
            path: Absolute path to the template file.

        Returns:
            One of ``"user"``, ``"project"``, ``"bundled"``, ``"custom"``, or
            ``"unknown"``.
        """
        resolved = path.resolve()
        for directory, label in engine.get_search_paths():
            try:
                resolved.relative_to(directory.resolve())
                return label
            except ValueError:
                continue
        return "unknown"

    def _writable_template_path(
        engine: "TemplateEngine",  # type: ignore[name-defined]
        template_id: str,
        source: str,
    ) -> Path:
        """Return the filesystem path where a template should be written.

        Only ``"user"`` and ``"project"`` sources are accepted.  Attempting to
        write to a ``"bundled"`` or ``"custom"`` source raises a 403.

        Args:
            engine: :class:`TemplateEngine` instance providing the directory
                    locations.
            template_id: The ``id`` field parsed from the template YAML.  Used
                         as the file stem (e.g. ``my-template`` →
                         ``my-template.yaml``).
            source: One of ``"user"`` or ``"project"``.

        Returns:
            :class:`Path` to ``<directory>/<template_id>.yaml``.  The parent
            directory is created if it does not exist.

        Raises:
            HTTPException(403): When *source* is ``"bundled"`` or ``"custom"``.
            HTTPException(400): When *source* is not a recognised value.
        """
        if source == "user":
            directory = engine._user_dir
        elif source == "project":
            directory = engine._project_dir
        else:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Source '{source}' is read-only via the API.  "
                    "Only 'user' and 'project' templates may be written."
                ),
            )

        # Sanitize template_id to prevent path traversal attacks.
        # IDs must start with an alphanumeric character and contain only
        # alphanumeric characters, hyphens, dots, and underscores.
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', template_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid template id '{template_id}'.  "
                    "IDs must contain only alphanumeric characters, hyphens, dots, and underscores."
                ),
            )

        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / f"{template_id}.yaml"
        # Double-check that the resolved destination is still inside the
        # target directory (defence-in-depth against symlink attacks).
        if dest.resolve().parent != directory.resolve():
            raise HTTPException(
                status_code=422,
                detail="Invalid template id (path traversal detected)",
            )
        return dest

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
            return p

        engine = TemplateEngine()

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
        """
        run_id = str(uuid.uuid4())[:8]
        if output_dir_override:
            output_dir = Path(output_dir_override)
        else:
            output_dir = Path(
                f"./output/{re.sub(r'[^\\w\\-]', '_', template.id)}"
                f"-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{run_id}"
            )
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
    async def webhook_health() -> JSONResponse:
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
        _KNOWN_REGRESSION_TEMPLATE_IDS = {
            "regression-pipeline-v1",
            "regression-fix-pipeline-v1",
        }
        _REGRESSION_WEBHOOK_PATH_SUFFIX = "/api/v1/webhooks/"

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
            except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
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

    # ---- Templates ---------------------------------------------------

    @app.get("/api/v1/templates")
    async def list_templates_api() -> JSONResponse:
        """List all discoverable pipeline templates.

        Returns a JSON array of template summaries.
        """
        engine = TemplateEngine()
        raw = engine.list_templates()
        result = []
        for t in raw:
            try:
                tpl = engine.load_template(Path(t["path"]))
                phases_summary = [
                    {
                        "id": p.id,
                        "name": p.name,
                        "model_tier": p.model_tier,
                        "thinking_level": p.thinking_level,
                        "depends_on": p.depends_on,
                    }
                    for p in tpl.phases
                ]
                config_schema = tpl.config_schema or {}
            except Exception:
                phases_summary = []
                config_schema = {}

            result.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "version": t["version"],
                    "phases_count": t["phases"],
                    "description": t.get("description", ""),
                    "source": t.get("source", ""),
                    "phases": phases_summary,
                    "config_schema": config_schema,
                }
            )
        return JSONResponse(result)

    @app.get("/api/v1/templates/{name}")
    async def get_template_api(name: str) -> JSONResponse:
        """Return detail for a single template by name or ID.

        Raises 404 when the template is not found.
        """
        engine = TemplateEngine()

        # Try by file stem, then by template id field.
        template = None
        try:
            path = engine.resolve_template(name)
            template = engine.load_template(path)
        except (TemplateNotFoundError, FileNotFoundError):
            # Scan by id
            for entry in engine.list_templates():
                if entry["id"] == name:
                    try:
                        template = engine.load_template(Path(entry["path"]))
                    except Exception:
                        pass
                    break

        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")

        phases_data = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "model_tier": p.model_tier,
                "thinking_level": p.thinking_level,
                "depends_on": p.depends_on,
                "task_type": p.task_type,
            }
            for p in template.phases
        ]

        return JSONResponse(
            {
                "id": template.id,
                "name": template.name,
                "version": template.version,
                "description": template.description,
                "author": template.author,
                "tags": template.tags,
                "phases": phases_data,
                "example_input": template.example_input,
                "config_schema": template.config_schema or {},
            }
        )

    @app.post("/api/v1/templates/validate")
    async def validate_template_api(req: TemplateValidateRequest) -> JSONResponse:
        """Validate a template body without writing it to disk.

        Parses the submitted YAML and runs the engine's validation logic,
        returning a structured list of errors and (optionally) warnings.

        Request body (JSON):
            content (str): Raw YAML content to validate.
            extended (bool): Also run extended linting.  Default ``True``.

        Returns:
            200 with ``{"valid": true/false, "errors": [...], "warnings": [...]}``
            422 when the content cannot be parsed as YAML or is structurally
                invalid (missing required top-level fields).
        """
        engine = TemplateEngine()

        # 1. Parse YAML
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)], "warnings": []},
            )

        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Template must be a YAML mapping",
                    "errors": ["Template content must be a YAML mapping (dict)"],
                    "warnings": [],
                },
            )

        # 2. Load via engine (uses a temporary file approach via load_template)
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tmp:
            tmp.write(req.content)
            tmp_path = Path(tmp.name)

        try:
            try:
                template = engine.load_template(tmp_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Template load error",
                        "errors": [str(exc)],
                        "warnings": [],
                    },
                )

            # 3. Basic validation
            errors = engine.validate_template(template)

            # 4. Extended validation — returns (errors, warnings) tuple.
            # raw (parsed above) is passed as raw_data per the method signature.
            ext_errors: List[str] = []
            warnings: List[str] = []
            if req.extended:
                try:
                    ext_errors, warnings = engine.validate_template_extended(template, raw)
                except Exception as exc:
                    warnings = [f"Extended validation error: {exc}"]

            # Merge structural + extended errors for the final verdict.
            all_errors = errors + ext_errors

        finally:
            tmp_path.unlink(missing_ok=True)

        return JSONResponse(
            {
                "valid": len(all_errors) == 0,
                "errors": all_errors,
                "warnings": warnings,
            }
        )

    @app.post("/api/v1/templates", status_code=201)
    async def create_template(req: TemplateCreateRequest) -> JSONResponse:
        """Create a new pipeline template by writing it to the templates directory.

        Parses and validates the submitted YAML content, then writes it to the
        appropriate templates directory (user or project) based on ``source``.
        Bundled templates are never written via the API.

        Request body (JSON):
            content (str): Raw YAML content of the new template.
            source (str): ``"user"`` (default) or ``"project"``.
            overwrite (bool): Allow replacing an existing file.  Default ``False``.

        Returns:
            201 with a :class:`TemplateWriteResponse`-shaped JSON object.
            409 when the template already exists and ``overwrite`` is ``False``.
            422 when the content fails validation.
        """
        engine = TemplateEngine()

        # 1. Parse YAML
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)]},
            )

        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422,
                detail={"message": "Template must be a YAML mapping"},
            )

        # 2. Load and validate
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tmp:
            tmp.write(req.content)
            tmp_path = Path(tmp.name)

        try:
            try:
                template = engine.load_template(tmp_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template load error", "errors": [str(exc)]},
                )

            errors = engine.validate_template(template)
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template validation failed", "errors": errors},
                )

            # 2b. Extended validation — returns (errors, warnings) tuple.
            # ``raw`` is the parsed YAML dict captured at the top of this handler.
            ext_errors: List[str] = []
            warnings: List[str] = []
            try:
                ext_errors, warnings = engine.validate_template_extended(template, raw)
            except Exception as exc:
                ext_errors = [f"Extended validation error: {exc}"]

            if ext_errors:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Template extended validation failed",
                        "errors": ext_errors,
                    },
                )

        finally:
            tmp_path.unlink(missing_ok=True)

        # 3. Determine destination path
        dest = _writable_template_path(engine, template.id, req.source)

        # 4. Conflict check
        if dest.exists() and not req.overwrite:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Template '{template.id}' already exists at '{dest}'.  "
                    "Set overwrite=true to replace it."
                ),
            )

        created = not dest.exists()

        # 5. Write
        dest.write_text(req.content, encoding="utf-8")

        # Surface extended-validation warnings in the response (additive,
        # non-breaking — consumers that don't expect the field can ignore it).
        response_data = TemplateWriteResponse(
            id=template.id,
            name=template.name,
            version=template.version,
            path=str(dest.resolve()),
            source=req.source,
            phases_count=len(template.phases),
            created=created,
        ).model_dump()
        response_data["warnings"] = warnings
        return JSONResponse(response_data, status_code=201)

    @app.put("/api/v1/templates/{name}")
    async def update_template(name: str, req: TemplateCreateRequest) -> JSONResponse:
        """Update an existing user-owned pipeline template.

        Resolves the template by *name*, validates the new content, and
        overwrites the file in place.  Only ``"user"`` and ``"project"`` source
        templates may be updated via the API; bundled templates return 403.

        Path parameter:
            name (str): Template name (file stem) or template ID.

        Request body (JSON):
            content (str): New YAML content.
            source: Ignored for PUT (the destination is the existing file's path).
            overwrite: Ignored for PUT (update always overwrites).

        Returns:
            200 with a :class:`TemplateWriteResponse`-shaped JSON object.
            403 when the resolved template is bundled or custom (read-only).
            404 when the template is not found.
            422 when the new content fails validation.
        """
        engine = TemplateEngine()

        # 1. Resolve the existing template to get its path
        existing_path = _resolve_template(name)

        # 2. Check source — only user templates are mutable via the API.
        # Project and bundled templates are read-only (project templates are
        # typically version-controlled; bundled templates are package assets).
        source = _template_source(engine, existing_path)
        if source != "user":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Template '{name}' is a {source} template and cannot be "
                    "modified via the API.  Only 'user' templates are writable."
                ),
            )

        # 3. Parse YAML — capture raw_data for extended validation below.
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)]},
            )

        # 4. Load and validate new content
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tmp:
            tmp.write(req.content)
            tmp_path = Path(tmp.name)

        try:
            try:
                template = engine.load_template(tmp_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template load error", "errors": [str(exc)]},
                )

            errors = engine.validate_template(template)
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template validation failed", "errors": errors},
                )

            # 4b. Extended validation — returns (errors, warnings) tuple.
            # ``raw`` is the parsed YAML dict captured above (not discarded as before).
            ext_errors: List[str] = []
            warnings: List[str] = []
            try:
                ext_errors, warnings = engine.validate_template_extended(template, raw)
            except Exception as exc:
                ext_errors = [f"Extended validation error: {exc}"]

            if ext_errors:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Template extended validation failed",
                        "errors": ext_errors,
                    },
                )

        finally:
            tmp_path.unlink(missing_ok=True)

        # 5. Overwrite existing file
        existing_path.write_text(req.content, encoding="utf-8")

        # Surface extended-validation warnings in the response (additive,
        # non-breaking — consumers that don't expect the field can ignore it).
        response_data = TemplateWriteResponse(
            id=template.id,
            name=template.name,
            version=template.version,
            path=str(existing_path.resolve()),
            source=source,
            phases_count=len(template.phases),
            created=False,
        ).model_dump()
        response_data["warnings"] = warnings
        return JSONResponse(response_data)

    @app.delete("/api/v1/templates/{name}", status_code=204)
    async def delete_template_api(name: str) -> Response:
        """Delete a user-owned pipeline template.

        Resolves the template by *name* or ID, checks that it belongs to the
        ``"user"`` source (project, bundled, and custom templates are
        protected), and removes the file from disk.

        Path parameter:
            name (str): Template name (file stem) or template ID.

        Returns:
            204 No Content on success (empty body — HTTP 204 must have no body).
            403 when the template is bundled or custom.
            404 when the template is not found.
        """
        engine = TemplateEngine()

        # 1. Resolve path
        existing_path = _resolve_template(name)

        # 2. Protect all non-user templates.
        # Only user-owned templates may be deleted via the API.  Project and
        # bundled templates are read-only (bundled = package assets; project =
        # version-controlled repo files).
        source = _template_source(engine, existing_path)
        if source != "user":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Template '{name}' is a {source} template and cannot be "
                    "deleted via the API.  Only 'user' templates are writable."
                ),
            )

        # 3. Delete
        existing_path.unlink()

        # HTTP 204 No Content — must return an empty body.
        return Response(status_code=204)

    # ---- Pipeline Runs -----------------------------------------------

    @app.post("/api/v1/runs", status_code=201)
    async def launch_run(req: LaunchRequest) -> JSONResponse:
        """Launch a new pipeline run in the background.

        Equivalent to ``orch launch`` — spawns a daemon subprocess and returns
        immediately with the run record.  Poll ``GET /api/v1/runs/{run_id}``
        to track progress.

        Returns:
            201 with the new run record (same shape as GET /api/v1/runs/{run_id}).
        """
        # 1. Resolve and validate template
        template_file = _resolve_template(req.template)

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        errors = engine.validate_template(template)
        if req.skip_scoring:
            errors = [e for e in errors if "require a scenario" not in e]
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "Template has validation errors", "errors": errors},
            )

        # 2. Launch via shared helper (DB row + daemon spawn)
        db = Database(Path(effective_db_path))
        effective_gw_url = req.gateway_url or os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=req.input,
            mode=req.mode,
            gateway_url=effective_gw_url,
            db=db,
            skip_scoring=req.skip_scoring,
            output_dir_override=req.output_dir,
        )

        # 3. Return the created run record
        return JSONResponse(run_dict, status_code=201)

    @app.post("/api/v1/webhooks/{trigger_id}")
    async def handle_webhook(trigger_id: str, request: Request) -> JSONResponse:
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
                k: v for k, v in input_map.items()
                if not (isinstance(v, str) and v.startswith("{{payload."))
            }
            template_map = {
                k: v for k, v in input_map.items()
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
        except Exception as exc:
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
        from orchestration_engine.webhooks import TriggerConfig, TriggerValidationError

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

        import sqlite3
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
    async def update_trigger_endpoint(
        trigger_id: str, body: TriggerUpdateRequest
    ) -> JSONResponse:
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
        from orchestration_engine.webhooks import TriggerValidationError

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
            from orchestration_engine.webhooks import TriggerConfig
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

    @app.get("/api/v1/runs")
    async def list_runs(
        status: Optional[str] = None,
        template_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """List pipeline runs with optional filtering and pagination.

        Query parameters:
            status: Filter by run status (pending, running, success, failed, cancelled, crashed).
            template_id: Filter by template ID.
            limit: Maximum number of results (default 20, max 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array and ``total`` count.
        """
        # Clamp limit/offset to avoid surprising SQLite behaviour with
        # negative values (negative LIMIT means "no limit"; negative OFFSET
        # is treated as 0 by SQLite but is semantically wrong).
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        runs = db.list_pipeline_runs_filtered(
            status=status,
            template_id=template_id,
            limit=limit,
            offset=offset,
        )
        # Get total count (without pagination)
        total = db.count_pipeline_runs(status=status, template_id=template_id)
        items = [_run_to_dict(r) for r in runs]
        return JSONResponse({"items": items, "total": total, "limit": limit, "offset": offset})

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        """Return the current state of a pipeline run.

        Also performs a liveness check on the daemon PID: if the process is
        no longer alive but the run is still ``running``, the status is updated
        to ``crashed`` in the DB before returning.

        Raises 404 when the run ID is not found.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        # PID liveness check
        if run.get("status") == "running" and run.get("pid"):
            try:
                from orchestration_engine.daemon import is_process_alive

                if not is_process_alive(run["pid"]):
                    db.update_pipeline_run(run_id, status="crashed")
                    run["status"] = "crashed"
            except Exception:
                pass

        return JSONResponse(_run_to_dict(run))

    @app.get("/api/v1/runs/{run_id}/children")
    async def get_run_children(run_id: str) -> JSONResponse:
        """Return all child pipeline runs spawned by a parent run.

        Queries ``pipeline_runs WHERE parent_run_id = run_id`` ordered by
        ``created_at ASC``.

        Returns::

            {
                "run_id": "<parent-run-id>",
                "children": [<RunResponse-shaped dicts>, ...]
            }

        Raises 404 when the parent run ID is not found.
        """  # Issue #330.3: children REST API
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        children = db.list_pipeline_run_children(run_id)
        return JSONResponse({
            "run_id": run_id,
            "children": [_run_to_dict(c) for c in children],
        })

    @app.get("/api/v1/runs/{run_id}/logs")
    async def get_run_logs(run_id: str) -> JSONResponse:
        """Return the daemon log file contents for a pipeline run.

        Returns ``{"run_id": ..., "log": "..."}`` where ``log`` is the full
        text of the ``.orch-daemon.log`` file.

        Raises 404 when the run ID or log file is not found.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        log_path = Path(run["output_dir"]) / ".orch-daemon.log"
        if not log_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Log file not found for run '{run_id}' (run may not have started yet)",
            )

        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"run_id": run_id, "log": log_text})

    @app.get("/api/v1/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request) -> EventSourceResponse:
        """Stream live phase-transition events for a pipeline run via SSE.

        Connects to the ``pipeline_run_events`` table and emits fine-grained
        events as the daemon writes them.  The stream ends automatically once
        the run reaches a terminal state (``success``, ``failed``,
        ``cancelled``, ``crashed``, ``scoring_failed``, ``pending_review``,
        ``rejected``) **and** all buffered events have been delivered.

        **Event types** (``event`` field):

        * ``phase_started`` — daemon has begun executing the phase.
        * ``phase_completed`` — phase finished; ``data`` includes
          ``tokens_consumed``, ``cost_usd``, and ``state``.
        * ``status_changed`` — run-level status transition (emitted once on
          terminal state: ``success``, ``failed``, ``cancelled``, ``crashed``,
          ``scoring_failed``, ``pending_review``, ``rejected``).
        * ``error`` — run not found or unexpected failure.

        **Data** is a JSON object with at minimum ``run_id`` and ``phase_id``
        (``null`` for run-level events).

        **Polling interval:** 1 second.  Clients that disconnect trigger a
        clean server-side shutdown of the generator.

        Raises 404 when the run ID is not found (returned as an SSE
        ``error`` event rather than an HTTP error so the EventSource protocol
        stays clean).
        """
        _TERMINAL_STATES = TERMINAL_STATUSES
        _POLL_INTERVAL = 1.0  # seconds between DB polls

        db = Database(Path(effective_db_path))

        # Validate run exists before opening the stream.
        run = db.get_pipeline_run(run_id)
        if run is None:
            async def _not_found():
                yield {
                    "event": "error",
                    "data": json.dumps({"error": f"Run '{run_id}' not found"}),
                }
            return EventSourceResponse(_not_found())

        async def _event_generator():
            last_event_id = 0
            emitted_terminal = False

            while True:
                # Respect client disconnect
                if await request.is_disconnected():
                    break

                # Fetch new events since the last one delivered
                events = db.list_pipeline_run_events(
                    run_id, after_id=last_event_id
                )
                for evt in events:
                    last_event_id = evt["id"]
                    payload = {
                        "run_id": run_id,
                        "phase_id": evt.get("phase_id"),
                        "tokens_consumed": evt.get("tokens_consumed"),
                        "cost_usd": evt.get("cost_usd"),
                        "state": evt.get("state"),
                        "created_at": (
                            evt["created_at"].isoformat()
                            if hasattr(evt.get("created_at"), "isoformat")
                            else evt.get("created_at")
                        ),
                    }
                    yield {
                        "event": evt["event_type"],
                        "data": json.dumps(payload),
                        "id": str(evt["id"]),
                    }

                # Check current run status for terminal state
                current_run = db.get_pipeline_run(run_id)
                if current_run and current_run.get("status") in _TERMINAL_STATES:
                    if not emitted_terminal:
                        emitted_terminal = True
                        terminal_payload = {
                            "run_id": run_id,
                            "phase_id": None,
                            "status": current_run["status"],
                            "completed_at": current_run.get("completed_at"),
                            "error_message": current_run.get("error_message"),
                        }
                        yield {
                            "event": "status_changed",
                            "data": json.dumps(terminal_payload),
                        }
                    # Drain any remaining events before closing
                    if not events:
                        break

                await asyncio.sleep(_POLL_INTERVAL)

        return EventSourceResponse(_event_generator())

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

    class ApproveRequest(BaseModel):
        """Body for POST /api/v1/reviews/{run_id}/approve."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored with the review record."""

        note: Optional[str] = None
        """Optional approval note stored in ``review_reason``."""

    class RejectRequest(BaseModel):
        """Body for POST /api/v1/reviews/{run_id}/reject."""

        reason: str
        """Mandatory rejection reason stored in ``review_reason``."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored with the review record."""

    # ------------------------------------------------------------------
    # Helper — build ReviewResponse dict from an enriched pending-review row
    # ------------------------------------------------------------------

    def _review_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert an enriched pending-review DB row to a ReviewResponse dict."""
        return {
            "run_id": row.get("run_id", ""),
            "template_id": row.get("template_id", ""),
            "status": row.get("status", ""),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
            "review_reason": row.get("review_reason"),
            "reviewed_at": row.get("reviewed_at"),
            "reviewed_by": row.get("reviewed_by"),
            "confidence_score": row.get("confidence_score"),
            "tier_name": row.get("tier_name"),
            "action": row.get("action"),
            "justification": row.get("justification"),
        }

    # ------------------------------------------------------------------
    # Review Queue endpoints (Issue #331.4)
    # ------------------------------------------------------------------

    @app.get("/api/v1/reviews")
    async def list_reviews(
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """List pipeline runs currently awaiting human review.

        Returns enriched run records that include confidence score, tier,
        action, and justification from the associated routing decision.

        Query parameters:
            limit:  Maximum number of results (default 20, max 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array, ``total`` count, ``limit``,
            and ``offset`` fields.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        items = db.list_pending_reviews(limit=limit, offset=offset)
        total = db.count_pending_reviews()
        return JSONResponse(
            {
                "items": [_review_row_to_dict(r) for r in items],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.post("/api/v1/reviews/{run_id}/approve", status_code=200)
    async def approve_review(run_id: str, body: ApproveRequest) -> JSONResponse:
        """Approve a pending-review pipeline run.

        Transitions the run from ``pending_review`` to ``success`` and records
        the reviewer identity and optional note.

        Path parameter:
            run_id: Pipeline run identifier.

        Request body (JSON):
            reviewed_by (str, optional): Operator identifier.
            note (str, optional): Approval note.

        Returns:
            - **200** with the updated run dict on success.
            - **404** when the run is not found.
            - **409** when the run is not in ``pending_review`` status.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )
        if run.get("status") != "pending_review":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is in status '{run.get('status')}', "
                    "not 'pending_review'. Only pending_review runs can be approved."
                ),
            )
        ok = db.approve_pipeline_run(
            run_id=run_id,
            reviewed_by=body.reviewed_by,
            note=body.note,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"Could not approve run '{run_id}'",
            )
        run = db.get_pipeline_run(run_id)
        return JSONResponse(_run_to_dict(run))

    @app.post("/api/v1/reviews/{run_id}/reject", status_code=200)
    async def reject_review(run_id: str, body: RejectRequest) -> JSONResponse:
        """Reject a pending-review pipeline run.

        Transitions the run from ``pending_review`` to ``rejected`` and records
        the rejection reason and reviewer identity.

        Path parameter:
            run_id: Pipeline run identifier.

        Request body (JSON):
            reason (str): Mandatory rejection reason.
            reviewed_by (str, optional): Operator identifier.

        Returns:
            - **200** with the updated run dict on success.
            - **404** when the run is not found.
            - **409** when the run is not in ``pending_review`` status.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )
        if run.get("status") != "pending_review":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is in status '{run.get('status')}', "
                    "not 'pending_review'. Only pending_review runs can be rejected."
                ),
            )
        ok = db.reject_pipeline_run(
            run_id=run_id,
            reason=body.reason,
            reviewed_by=body.reviewed_by,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"Could not reject run '{run_id}'",
            )
        run = db.get_pipeline_run(run_id)
        return JSONResponse(_run_to_dict(run))

    @app.delete("/api/v1/runs/{run_id}", status_code=200)
    async def cancel_run(run_id: str) -> JSONResponse:
        """Cancel a running or pending pipeline run.

        Sends SIGTERM to the daemon process (if any) and updates the run
        status to ``cancelled`` in the DB.

        Returns:
            200 with ``{"run_id": ..., "cancelled": true}`` on success.
            404 when the run ID is not found.
            409 when the run is already in a terminal state.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        terminal_states = TERMINAL_STATUSES
        if run.get("status") in terminal_states:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is already in terminal state '{run['status']}' "
                    "and cannot be cancelled"
                ),
            )

        cancelled = db.cancel_pipeline_run(run_id)
        return JSONResponse({"run_id": run_id, "cancelled": cancelled})

    # ------------------------------------------------------------------
    # Cost API endpoints (Issue #5.2.3)
    # ------------------------------------------------------------------

    _VALID_GROUP_BY = {"day", "template", "model"}
    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    @app.get("/api/v1/costs/summary")
    async def cost_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """Return aggregated cost data grouped by day, template, or model.

        Query parameters:
            start_date: Optional ISO date ``YYYY-MM-DD`` (inclusive lower bound).
            end_date:   Optional ISO date ``YYYY-MM-DD`` (inclusive upper bound).
            group_by:   Grouping dimension — ``day`` (default), ``template``, or
                        ``model``.
            limit:      Page size (default 20, max 100).
            offset:     Number of items to skip (default 0).

        Returns:
            200 with ``{"items": [...], "total": int, "limit": int, "offset": int}``.
            400 when ``group_by`` is invalid or date strings are malformed.
        """
        if group_by not in _VALID_GROUP_BY:
            raise HTTPException(
                status_code=400,
                detail=f"group_by must be one of {sorted(_VALID_GROUP_BY)}, got {group_by!r}",
            )
        if start_date is not None and not _DATE_RE.match(start_date):
            raise HTTPException(
                status_code=400,
                detail=f"start_date must be YYYY-MM-DD, got {start_date!r}",
            )
        if end_date is not None and not _DATE_RE.match(end_date):
            raise HTTPException(
                status_code=400,
                detail=f"end_date must be YYYY-MM-DD, got {end_date!r}",
            )
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        db = Database(Path(effective_db_path))
        items = db.get_cost_summary(start_date, end_date, group_by, limit, offset)
        total = db.count_cost_summary(start_date, end_date, group_by)
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/costs/run/{run_id}")
    async def cost_run_breakdown(run_id: str) -> JSONResponse:
        """Return per-phase cost records for a specific pipeline run.

        Path parameters:
            run_id: The pipeline run identifier.

        Returns:
            200 with ``{"run_id": str, "items": [...], "total_cost": float,
            "total_input_tokens": int, "total_output_tokens": int}``.
            404 when no cost records exist for the given ``run_id``.
        """
        db = Database(Path(effective_db_path))
        items = db.get_run_costs(run_id)
        if not items:
            raise HTTPException(
                status_code=404,
                detail=f"No cost records found for run '{run_id}'",
            )
        total_cost = sum(row.get("cost_usd", 0.0) or 0.0 for row in items)
        total_input = sum(row.get("input_tokens", 0) or 0 for row in items)
        total_output = sum(row.get("output_tokens", 0) or 0 for row in items)
        return JSONResponse(
            {
                "run_id": run_id,
                "items": items,
                "total_cost": total_cost,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
            }
        )

    # ------------------------------------------------------------------
    # Trust profile API endpoints (Issue #4.2.4)
    # ------------------------------------------------------------------

    class TrustOverrideRequest(BaseModel):
        """Body for PUT /api/v1/trust/profiles/{profile_id} — manual override."""

        trust_score: float
        """New trust score to set, in [0.0, 1.0]."""

        reason: str
        """Human-readable justification for the manual override."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored in the audit log."""

    @app.get("/api/v1/trust/profiles")
    async def list_trust_profiles(
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """List all trust profiles, ordered by id ASC.

        Query parameters:
            limit:  Maximum number of results (default 100, max 500).
            offset: Number of rows to skip for pagination (default 0).

        Returns:
            JSON object with ``items`` array, ``total`` count, ``limit``,
            and ``offset`` fields.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        all_profiles = db.list_trust_profiles()
        total = len(all_profiles)
        items = all_profiles[offset: offset + limit]
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/trust/profiles/{profile_id}")
    async def get_trust_profile_by_id(profile_id: int) -> JSONResponse:
        """Return a single trust profile by its integer primary key.

        Args:
            profile_id: Integer primary key of the trust profile row.

        Returns:
            200 with the trust profile dict.
            404 when no profile matches the given id.
        """
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )
        return JSONResponse(profile)

    @app.put("/api/v1/trust/profiles/{profile_id}")
    async def override_trust_profile(
        profile_id: int,
        body: TrustOverrideRequest,
    ) -> JSONResponse:
        """Manually override the trust score for a profile.

        Validates that ``trust_score`` is in ``[0.0, 1.0]``, updates the DB
        row, re-derives the ``auto_merge_threshold``, and logs a
        ``trust_adjustments`` entry with reason ``"manual_override"``.

        Args:
            profile_id: Integer primary key of the trust profile to update.
            body:       ``TrustOverrideRequest`` with the new score and reason.

        Returns:
            200 with the updated trust profile dict.
            404 when no profile matches the given id.
            422 when ``trust_score`` is outside ``[0.0, 1.0]``.
        """
        if not (0.0 <= body.trust_score <= 1.0):
            raise HTTPException(
                status_code=422,
                detail=f"trust_score must be in [0.0, 1.0], got {body.trust_score!r}",
            )
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )

        from ..trust import TrustCalibrator

        old_score = float(profile["trust_score"])
        new_score = body.trust_score
        delta = new_score - old_score

        # Re-derive auto_merge_threshold
        calibrator = TrustCalibrator(
            repo=profile["repo"],
            template_id=profile["template_id"],
            task_type=profile["task_type"],
        )
        successful_merges = int(profile.get("successful_merges", 0))
        new_threshold = calibrator.compute_threshold(new_score, successful_merges)

        now_iso = datetime.now(timezone.utc).isoformat()
        updated: Dict[str, Any] = {
            "repo":                   profile["repo"],
            "template_id":            profile["template_id"],
            "task_type":              profile["task_type"],
            "auto_merge_threshold":   new_threshold,
            "human_review_threshold": float(profile["human_review_threshold"]),
            "trust_score":            new_score,
            "total_runs":             int(profile["total_runs"]),
            "successful_merges":      successful_merges,
            "regressions":            int(profile["regressions"]),
            "reverted_prs":           int(profile["reverted_prs"]),
            "last_run_at":            profile.get("last_run_at"),
            "created_at":             profile["created_at"],
            "updated_at":             now_iso,
        }
        db.upsert_trust_profile(updated)

        # Build audit note (include reviewer if supplied)
        audit_reason = "manual_override"
        if body.reviewed_by:
            audit_reason = f"manual_override:{body.reviewed_by}"

        db.insert_trust_adjustment({
            "profile_id":   profile_id,
            "delta":        delta,
            "reason":       audit_reason,
            "run_id":       None,
            "score_before": old_score,
            "score_after":  new_score,
            "created_at":   now_iso,
        })

        refreshed = db.get_trust_profile_by_id(profile_id)
        return JSONResponse(refreshed)

    @app.get("/api/v1/trust/adjustments")
    async def list_trust_adjustments(
        profile_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """Return the trust adjustment audit log for a profile.

        Query parameters:
            profile_id: **(required)** Integer primary key of the trust profile.
            limit:      Maximum number of results (default 100, max 500).
            offset:     Number of rows to skip for pagination (default 0).

        Returns:
            200 with ``{"items": [...], "total": int, "limit": int, "offset": int}``.
            404 when no profile matches ``profile_id``.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )
        items = db.list_trust_adjustments(profile_id=profile_id, limit=limit, offset=offset)
        return JSONResponse(
            {
                "items": items,
                "total": len(items),
                "limit": limit,
                "offset": offset,
            }
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
        db_path = (
            os.environ.get("NOTIFY_TELEGRAM_CALLBACK_DB_PATH", "")
            or effective_db_path
        )
        gateway_url = os.environ.get(
            "NOTIFY_OPENCLAW_GATEWAY_URL",
            os.environ.get("OPENCLAW_GATEWAY_URL", ""),
        )
        gateway_token = os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN", "")
        bot_token = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "")

        from orchestration_engine.notifications import TelegramCallbackHandler

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
            logger.warning(
                "Telegram callback handler returned non-ok result: %s", result
            )
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GitHub Issues Webhook (Issue #5.1.3)
    # ------------------------------------------------------------------

    @app.post("/api/v1/github/issues", status_code=202)
    async def handle_github_issues(request: Request) -> JSONResponse:
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
        from orchestration_engine.issue_automation import (
            IssueAutomation,
            IssueClassifier,
            TemplateSelector,
            InputExtractor,
            post_github_comment,
        )
        from orchestration_engine.notifications import NotificationDispatcher

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
        from orchestration_engine.config import get_global_config
        cfg = get_global_config()
        if cfg.github_app and cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(
                cfg.github_app.webhook_secret, _body_bytes, sig_header
            ):
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
            issue_labels = [
                lbl.get("name", "") for lbl in (issue.get("labels") or [])
            ]
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
        classifier = IssueClassifier()   # stub mode; replace executor via subclass/config
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
        issue_label_names = [
            lbl.get("name", "") for lbl in (issue.get("labels") or [])
        ]

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
    async def handle_github_issues_pipeline_ready(request: Request) -> JSONResponse:
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
        from orchestration_engine.issue_automation import (
            generate_pipeline_input,
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
        from orchestration_engine.config import get_global_config
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
        try:
            template_file = _resolve_template("coding-pipeline-v1")
        except HTTPException:
            raise HTTPException(
                status_code=400,
                detail="Template 'coding-pipeline-v1' not found",
            )

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:
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
            f"🤖 **Orchemist** detected `pipeline-ready` label and launched the coding pipeline.\n\n"
            f"**Branch:** `{branch_name}`\n"
            f"**Run ID:** `{run_id}`\n\n"
            f"Progress can be tracked via `orch status {run_id}`."
        )
        post_github_comment(repo=repo, issue_number=issue_number, body=comment_body)

        return JSONResponse(
            {"status": "accepted", "run_id": run_id, "branch_name": branch_name},
            status_code=202,
        )

    return app
