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
from datetime import datetime, timedelta
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
    from orchestration_engine.db import Database
    from orchestration_engine.templates import TemplateEngine, TemplateNotFoundError

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

        # 5. Rate-limit check
        rate_limit = trigger_row.get("rate_limit", 0)
        if _check_rate_limit(trigger_id, rate_limit, db):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit of {rate_limit} req/min exceeded for trigger '{trigger_id}'",
            )

        # 6. Parse payload JSON
        try:
            payload: Dict[str, Any] = json.loads(payload_bytes) if payload_bytes else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}

        # 7. Apply input_map to transform payload → pipeline input
        input_map = trigger_row.get("input_map") or {}
        input_data = _apply_input_map(payload, input_map) if input_map else dict(payload)

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
        ``cancelled``, ``crashed``, ``scoring_failed``) **and** all buffered
        events have been delivered.

        **Event types** (``event`` field):

        * ``phase_started`` — daemon has begun executing the phase.
        * ``phase_completed`` — phase finished; ``data`` includes
          ``tokens_consumed``, ``cost_usd``, and ``state``.
        * ``status_changed`` — run-level status transition (emitted once on
          terminal state: ``success``, ``failed``, ``cancelled``, ``crashed``,
          ``scoring_failed``).
        * ``error`` — run not found or unexpected failure.

        **Data** is a JSON object with at minimum ``run_id`` and ``phase_id``
        (``null`` for run-level events).

        **Polling interval:** 1 second.  Clients that disconnect trigger a
        clean server-side shutdown of the generator.

        Raises 404 when the run ID is not found (returned as an SSE
        ``error`` event rather than an HTTP error so the EventSource protocol
        stays clean).
        """
        _TERMINAL_STATES = {"success", "failed", "cancelled", "crashed", "scoring_failed"}
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

        terminal_states = {"success", "failed", "cancelled", "crashed", "scoring_failed"}
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

    return app
