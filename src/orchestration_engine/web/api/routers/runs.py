"""Pipeline-runs route group for the REST API (Issue #942, sub-issue 952b).

Holds the run lifecycle + artifact + SSE-stream routes, extracted *verbatim*
from ``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``POST   /api/v1/runs``                              (launch)
* ``GET    /api/v1/runs``                              (list)
* ``GET    /api/v1/runs/{run_id}``                     (detail)
* ``GET    /api/v1/runs/{run_id}/children``
* ``GET    /api/v1/runs/{run_id}/logs``
* ``GET    /api/v1/runs/{run_id}/artifacts``
* ``GET    /api/v1/runs/{run_id}/artifacts/{filename}``
* ``GET    /api/v1/runs/{run_id}/phase0``
* ``GET    /api/v1/runs/{run_id}/dialogue``
* ``GET    /api/v1/runs/{run_id}/stream``              (SSE)
* ``DELETE /api/v1/runs/{run_id}``                     (cancel)

``LaunchRequest`` (used only by ``POST /api/v1/runs``) moves with the routes
into ``register_run_routes``; it is defined inside the register function with a
lazy ``from pydantic import BaseModel`` so importing this module does NOT eagerly
pull ``pydantic`` (preserving the optional ``[api]`` extra contract).

The three artifact helper closures (``_resolve_output_dir``, ``_read_artifact``)
and the ``_ARTIFACT_MAX_BYTES`` constant are used only by these routes, so they
move into ``register_run_routes`` as nested locals — verbatim — keeping their
closure over ``HTTPException`` / ``_ARTIFACT_MAX_BYTES``.

The ``stream`` route closes over the shared ``_SSE_LIMITER`` instance (received
as ``_sse_limiter``); the admit/release accounting is preserved exactly so the
``ORCH_MAX_SSE_*`` caps stay accurate.  The factory-local helpers
``_run_to_dict``, ``_launch_pipeline_from_trigger`` and ``_resolve_template`` are
shared with routes that remain in ``_app.py`` (reviews / webhooks / github /
issues), so they stay there and are received here as keyword arguments — the
same objects the inline closures used.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple


def register_run_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Request: Any,  # noqa: N803
    EventSourceResponse: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    TERMINAL_STATUSES: Any,  # noqa: N803
    TemplateEngine: Any,  # noqa: N803
    asyncio: Any,
    effective_db_path: str,
    _normalize_ts: Any,
    _resolve_template: Any,
    _run_to_dict: Any,
    _launch_pipeline_from_trigger: Any,
    _sse_limiter: Any,
) -> None:
    """Register the pipeline-runs route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, status codes, params, validation, response
    shapes, error handling, and (for the stream route) SSE connection-limit
    accounting.

    ``LaunchRequest`` is defined here (not at module scope) with a lazy
    ``from pydantic import BaseModel`` so importing this module does NOT eagerly
    pull ``pydantic`` — preserving the optional ``[api]`` extra contract, exactly
    as the inline definition did inside ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class LaunchRequest(BaseModel):
        """Body for POST /api/v1/runs — launch a new pipeline run.

        Accepts either ``template`` or ``template_id`` as the template identifier.
        ``template_id`` is an alias for ``template`` for API consumers that use
        the ``template_id`` naming convention.
        """

        template: Optional[str] = None
        """Template name (resolved from search paths) or path to a .yaml file."""

        template_id: Optional[str] = None
        """Alias for ``template``.  When provided, used as the template identifier."""

        mode: Literal["standalone", "openclaw", "openrouter", "dry-run"] = "dry-run"
        """Execution mode passed to the daemon subprocess."""

        input: Dict[str, Any] = {}
        """Initial pipeline input (equivalent to ``--input`` / ``--input-file``)."""

        output_dir: Optional[str] = None
        """Directory to write phase outputs.  Auto-generated when omitted."""

        gateway_url: Optional[str] = None
        """OpenClaw gateway URL (openclaw mode).  Falls back to OPENCLAW_GATEWAY_URL."""

        skip_scoring: bool = False
        """Skip auto-scoring even if the template declares a scenario."""

        executor: Optional[str] = None
        """Executor backend for standalone mode (api/claudecode/auto)."""

        api_key: Optional[str] = None
        """API key passed to daemon as env var. ANTHROPIC_API_KEY (standalone) or OPENROUTER_API_KEY (openrouter). Never persisted."""  # noqa: E501

        model_map: Optional[Dict[str, str]] = None
        """Custom model tier overrides for openrouter mode."""

        issue_number: Optional[int] = None
        """GitHub issue number to auto-fetch as pipeline input."""

        repo: Optional[str] = None
        """GitHub repository slug (owner/repo) for issue lookup."""

        @property
        def resolved_template(self) -> Optional[str]:
            """Return the effective template identifier (template or template_id)."""
            return self.template or self.template_id

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
        # Support both 'template' and 'template_id' fields (template_id is an alias)
        effective_template = req.resolved_template
        if not effective_template:
            raise HTTPException(
                status_code=422,
                detail="Either 'template' or 'template_id' must be provided",
            )
        template_file = _resolve_template(effective_template)

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        errors = engine.validate_template(template)
        if req.skip_scoring:
            errors = [e for e in errors if "require a scenario" not in e]
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "Template has validation errors", "errors": errors},
            )

        # 2. Prepare input data with executor and model map overrides
        launch_input = dict(req.input)
        if req.executor:
            launch_input["_executor_type"] = req.executor
        if req.model_map:
            launch_input["_model_map"] = req.model_map

        # Build extra env vars for API key (never persisted to DB)
        extra_env: Dict[str, str] = {}
        if req.api_key:
            if req.mode == "openrouter":
                extra_env["OPENROUTER_API_KEY"] = req.api_key
            else:
                extra_env["ANTHROPIC_API_KEY"] = req.api_key

        # 2b. Launch via shared helper (DB row + daemon spawn)
        db = Database(Path(effective_db_path))
        effective_gw_url = req.gateway_url or os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=launch_input,
            mode=req.mode,
            gateway_url=effective_gw_url,
            db=db,
            skip_scoring=req.skip_scoring,
            output_dir_override=req.output_dir,
            extra_env=extra_env or None,
        )

        # 3. Return the created run record
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
                from orchestration_engine.daemon import is_process_alive  # noqa: PLC0415

                if not is_process_alive(run["pid"]):
                    db.update_pipeline_run(run_id, status="crashed")
                    run["status"] = "crashed"
            except Exception:  # noqa: BLE001
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
        return JSONResponse(
            {
                "run_id": run_id,
                "children": [_run_to_dict(c) for c in children],
            }
        )

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

    # ── Run artifact endpoints (filed as harness gaps from PR #816) ──
    # The pipeline writes one file per phase under `output_dir`. The harness
    # needs to read these to render the Run Cockpit artifacts list, the
    # Phase 0 inventory card, and the Adversary Loop dialogue rounds.
    #
    # All three endpoints follow the same pattern:
    #   1. Resolve `output_dir` from the run record (404 if missing)
    #   2. Constrain the requested path within `output_dir` (defence against
    #      path traversal: `Path.resolve()` + `is_relative_to`)
    #   3. Cap any returned text at 1 MiB so a malformed artifact can't OOM
    #      the response (operators chasing a truly huge artifact can still
    #      ssh to the daemon host).

    # Maximum bytes returned for any single artifact body. The Phase 0
    # inventory + dialogue artifacts are typically 2-10 KB; spec / behavioral
    # / review markdown are 3-50 KB; even an aggressive run rarely exceeds
    # 200 KB total. 1 MiB is a comfortable ceiling.
    _ARTIFACT_MAX_BYTES = 1024 * 1024  # noqa: N806

    def _resolve_output_dir(run: Dict[str, Any]) -> Path:
        """Return the run's output_dir as a resolved absolute Path.

        Raises ``HTTPException(404)`` when ``output_dir`` is missing from
        the run record or does not exist on disk.
        """
        out = run.get("output_dir")
        if not out:
            raise HTTPException(status_code=404, detail="Run has no output_dir")
        out_path = Path(out).resolve()
        if not out_path.exists() or not out_path.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Run output_dir does not exist on disk: {out_path}",
            )
        return out_path

    def _read_artifact(out_dir: Path, filename: str) -> str:
        """Read *filename* from *out_dir* with path-traversal + size guards.

        Returns the file's text content (UTF-8, replacement on invalid bytes),
        truncated at ``_ARTIFACT_MAX_BYTES`` with an explicit truncation
        marker appended when over the limit.
        """
        # Constrain to a single path segment — no traversal, no nesting.
        # Even though we re-resolve below, this is a fast-fail that gives a
        # better error message than "file not found" for crafted inputs.
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid artifact filename")

        target = (out_dir / filename).resolve()
        try:
            target.relative_to(out_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail="Artifact path escapes output_dir")

        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found")

        raw = target.read_bytes()
        truncated = len(raw) > _ARTIFACT_MAX_BYTES
        if truncated:
            raw = raw[:_ARTIFACT_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[…truncated by API: artifact exceeded 1 MiB…]"
        return text

    @app.get("/api/v1/runs/{run_id}/artifacts")
    async def list_run_artifacts(run_id: str) -> JSONResponse:
        """List files in a run's output_dir.

        Returns ``{"run_id": ..., "output_dir": ..., "files": [...]}`` where
        each file entry has ``{name, size_bytes, mtime}``. Hidden files
        (those starting with ``.``) are excluded — the daemon's
        ``.orch-daemon.log`` is exposed via ``/logs`` instead.

        Files are sorted alphabetically by name (the conventional
        phase-ordered prefixes — ``0_existing_symbols.md``, ``1_spec.md``,
        etc. — sort naturally in pipeline order).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        files = []
        for entry in sorted(out_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or not entry.is_file():
                continue
            stat = entry.stat()
            files.append(
                {
                    "name": entry.name,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
        return JSONResponse(
            {
                "run_id": run_id,
                "output_dir": str(out_dir),
                "files": files,
            }
        )

    @app.get("/api/v1/runs/{run_id}/artifacts/{filename}")
    async def get_run_artifact(run_id: str, filename: str) -> JSONResponse:
        """Return the body of a single artifact file from a run's output_dir.

        Path-traversal guarded; body truncated at 1 MiB. The response shape
        is ``{"run_id": ..., "filename": ..., "size_bytes": ..., "content": ...}``
        — text only; binary files will be returned with replacement chars.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)
        content = _read_artifact(out_dir, filename)
        target = (out_dir / filename).resolve()
        return JSONResponse(
            {
                "run_id": run_id,
                "filename": filename,
                "size_bytes": target.stat().st_size,
                "content": content,
            }
        )

    @app.get("/api/v1/runs/{run_id}/phase0")
    async def get_run_phase0(run_id: str) -> JSONResponse:
        """Parse the Phase 0 existing-symbols inventory for a run.

        Looks for ``existing_symbols.md`` (or a phase-numbered variant) in
        the run's ``output_dir`` and extracts the four canonical sections
        defined in `coding-pipeline-standard.yaml` v4.2:

            1. UI primitives
            2. Project shared libraries
            3. Adjacent action / hook / route patterns
            4. Workspace barrels

        Plus the §5/§6 verdict-label entries when present.

        Returns ``{"run_id": ..., "sections": {"ui_primitives": {"count": N,
        "entries": [...]}, "shared_libs": {...}, "adjacent_patterns": {...},
        "workspace_barrels": {...}}, "verdicts": {"CONSUME": N, "EXTEND": N,
        "DIVERGENT": N, "NEW_OK": N}, "raw": "..."}``.

        Raises 404 if no Phase 0 artifact exists (the pipeline may have used
        ``coding-pipeline-skip-spec.yaml`` which has no Phase 0).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        # Try common filename variants (Phase 0 may be numbered, prefixed, etc.)
        candidates = [
            "existing_symbols.md",
            "0_existing_symbols.md",
            "phase0_existing_symbols.md",
            "existing_symbols_inventory.md",
        ]
        artifact: Optional[Path] = None
        for name in candidates:
            p = out_dir / name
            if p.exists() and p.is_file():
                artifact = p
                break
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail=f"No Phase 0 inventory artifact in {out_dir} (tried: {candidates})",
            )

        raw = artifact.read_text(encoding="utf-8", errors="replace")

        # Parse the four standard sections. Headings come from the v4.2 YAML
        # ("## 1. UI primitives", "## 2. Project shared libraries", etc.).
        # We split on the next "## " heading after each section's start.
        import re as _re  # noqa: PLC0415

        section_specs: List[Tuple[str, str]] = [
            ("ui_primitives", r"^##\s+1\.\s+UI primitives"),
            ("shared_libs", r"^##\s+2\.\s+Project shared libraries"),
            ("adjacent_patterns", r"^##\s+3\.\s+Adjacent"),
            ("workspace_barrels", r"^##\s+4\.\s+Workspace barrels"),
        ]
        sections: Dict[str, Dict[str, Any]] = {}
        for key, pattern in section_specs:
            m = _re.search(pattern, raw, _re.MULTILINE)
            if m is None:
                sections[key] = {"count": 0, "entries": []}
                continue
            start = m.end()
            next_h2 = _re.search(r"^##\s+\d", raw[start:], _re.MULTILINE)
            body = raw[start : (start + next_h2.start()) if next_h2 else len(raw)]
            # An "entry" is any bullet line (- something) or backtick-wrapped
            # symbol line. Empty stubs ("(empty — consumer did not provide…)")
            # produce zero entries.
            entries = []
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") and "(empty —" not in stripped:
                    entries.append(stripped[2:])
            sections[key] = {
                "count": len(entries),
                "entries": entries[:50],
            }  # cap per-section payload

        # Verdict label counts from §5/§6 (CONSUME / EXTEND / DIVERGENT / NEW-OK / BLOCKED)
        verdicts = {
            "CONSUME": len(_re.findall(r"\bCONSUME\b", raw)),
            "EXTEND": len(_re.findall(r"\bEXTEND\b", raw)),
            "DIVERGENT": len(_re.findall(r"\bDIVERGENT\b", raw)),
            "NEW_OK": len(_re.findall(r"\bNEW[-_]OK\b", raw)),
            "BLOCKED": len(_re.findall(r"\bBLOCKED\b", raw)),
        }

        return JSONResponse(
            {
                "run_id": run_id,
                "filename": artifact.name,
                "sections": sections,
                "verdicts": verdicts,
                "raw": (
                    raw
                    if len(raw) <= _ARTIFACT_MAX_BYTES
                    else raw[:_ARTIFACT_MAX_BYTES] + "\n[…truncated…]"
                ),
            }
        )

    @app.get("/api/v1/runs/{run_id}/dialogue")
    async def get_run_dialogue(run_id: str) -> JSONResponse:
        """Return the cross-model dialogue artifact for a run, if present.

        Looks for ``dialogue.md`` / ``dialogue_phase.md`` / ``spec-review-dialogue.md``
        in the run's ``output_dir``. The dialogue artifact is written by
        the Track B dialogue phase (PR #808) — only runs that used the
        ``coding-pipeline-with-dialogue`` template (or equivalent) will have one.

        Returns ``{"run_id": ..., "filename": ..., "rounds": [...], "raw": "..."}``.
        Each round entry is ``{"index": N, "side": "drafter" | "reviewer",
        "model": "...", "verdict": "...", "content": "...", "jaccard": float | null}``.

        Raises 404 when no dialogue artifact exists (most runs).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        candidates = [
            "dialogue.md",
            "dialogue_phase.md",
            "spec-review-dialogue.md",
            "spec_review_dialogue.md",
        ]
        artifact: Optional[Path] = None
        for name in candidates:
            p = out_dir / name
            if p.exists() and p.is_file():
                artifact = p
                break
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No dialogue artifact in {out_dir} — this run did not use a "
                    f"dialogue phase (Track B, PR #808). Tried: {candidates}"
                ),
            )

        raw = _read_artifact(out_dir, artifact.name)

        # Parse rounds. The dialogue phase writes one section per turn
        # with a heading like "## Round N · DRAFTER (model)" or
        # "## Round N · REVIEWER (model) · VERDICT".
        import re as _re  # noqa: PLC0415

        rounds: List[Dict[str, Any]] = []
        round_re = _re.compile(
            r"^##\s+Round\s+(?P<idx>\d+)\s*[·\-:]\s*"
            r"(?P<side>DRAFTER|REVIEWER)"
            r"(?:\s*\((?P<model>[^)]+)\))?"
            r"(?:\s*[·\-:]\s*(?P<verdict>APPROVE|REQUEST_CHANGES|REVISE|ABORT))?",
            _re.IGNORECASE | _re.MULTILINE,
        )
        matches = list(round_re.finditer(raw))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            content = raw[start:end].strip()
            # Extract jaccard from content body if present
            # Capture decimal Jaccard value, stopping before any trailing
            # sentence punctuation (e.g. ``Jaccard 0.93.`` → ``0.93``).
            jac_m = _re.search(r"[Jj]accard[^0-9]*(\d+(?:\.\d+)?)", content)
            rounds.append(
                {
                    "index": int(m.group("idx")),
                    "side": (m.group("side") or "").lower(),
                    "model": m.group("model"),
                    "verdict": (m.group("verdict") or "").lower() or None,
                    "content": content[:4096],  # cap per-round body
                    "jaccard": float(jac_m.group(1)) if jac_m else None,
                }
            )

        return JSONResponse(
            {
                "run_id": run_id,
                "filename": artifact.name,
                "rounds": rounds,
                "raw": raw,
            }
        )

    @app.get("/api/v1/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request) -> EventSourceResponse:  # noqa: C901
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
        * ``tool_call_started`` / ``tool_call_completed`` — reserved schema
          for tool-aware executors to emit per-tool-call progress. Schema:
          ``{run_id, phase_id, tool_name, tool_args | result_summary,
          iteration}``. NOT emitted by any executor today and NOT rendered
          by the harness frontend; the handler will forward them transparently
          when both sides land in a follow-up PR.
        * ``error`` — run not found or unexpected failure.

        **Data** is a JSON object with at minimum ``run_id`` and ``phase_id``
        (``null`` for run-level events).

        **Polling interval:** 1 second.  Clients that disconnect trigger a
        clean server-side shutdown of the generator.

        Raises 404 when the run ID is not found (returned as an SSE
        ``error`` event rather than an HTTP error so the EventSource protocol
        stays clean).
        """
        _TERMINAL_STATES = TERMINAL_STATUSES  # noqa: N806
        _POLL_INTERVAL = 1.0  # seconds between DB polls  # noqa: N806

        # ── SSE connection limits (#841) ─────────────────────────────
        # Check limits BEFORE opening the stream. On hit, return 429
        # with Retry-After so clients back off instead of reconnecting
        # tight-loop. Successful admit MUST be paired with a release in
        # the generator's finally block.
        client_ip = request.client.host if request.client else "unknown"
        admit_err = _sse_limiter.admit(client_ip)
        if admit_err is not None:
            raise HTTPException(
                status_code=429,
                detail=admit_err,
                headers={"Retry-After": "30"},
            )

        db = Database(Path(effective_db_path))

        # Validate run exists before opening the stream.
        run = db.get_pipeline_run(run_id)
        if run is None:
            # Release the slot we just admitted — the not-found stream
            # is a fast-fail and shouldn't count against the cap.
            _sse_limiter.release(client_ip)

            async def _not_found():
                yield {
                    "event": "error",
                    "data": json.dumps({"error": f"Run '{run_id}' not found"}),
                }

            return EventSourceResponse(_not_found())

        async def _event_generator():
            last_event_id = 0
            emitted_terminal = False

            try:
                while True:
                    # Respect client disconnect
                    if await request.is_disconnected():
                        break

                    # Fetch new events since the last one delivered
                    events = db.list_pipeline_run_events(run_id, after_id=last_event_id)
                    for evt in events:
                        last_event_id = evt["id"]
                        # Parse metadata JSON for enriched fields (#747)
                        meta = {}
                        raw_meta = evt.get("metadata_json")
                        if raw_meta:
                            try:
                                meta = (
                                    json.loads(raw_meta)
                                    if isinstance(raw_meta, str)
                                    else (raw_meta or {})
                                )
                            except (json.JSONDecodeError, TypeError):
                                pass

                        payload = {
                            "run_id": run_id,
                            "phase_id": evt.get("phase_id"),
                            "tokens_consumed": evt.get("tokens_consumed"),
                            "cost_usd": evt.get("cost_usd"),
                            "state": evt.get("state"),
                            "created_at": _normalize_ts(evt.get("created_at")),
                            # Enriched fields (#747)
                            "model_tier": meta.get("model_tier"),
                            "model_used": meta.get("model_used"),
                            "phase_name": meta.get("phase_name"),
                            "thinking_level": meta.get("thinking_level"),
                            "elapsed_seconds": meta.get("elapsed_seconds"),
                            "tokens_in": meta.get("tokens_in"),
                            "tokens_out": meta.get("tokens_out"),
                            "word_count": meta.get("word_count"),
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
            finally:
                # Always release the SSE slot — whether the loop exited
                # via client disconnect, terminal-state break, or any
                # other path (#841).
                _sse_limiter.release(client_ip)

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
