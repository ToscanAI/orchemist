"""FastAPI web application for the Orchestration Engine web UI.

Provides REST endpoints for template listing, pipeline execution, and
Server-Sent Events (SSE) for live progress streaming.

All dependencies (fastapi, uvicorn, sse-starlette) are optional — import this
module only after confirming they are installed (see cli.py serve command).
"""

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

# Lazily imported heavy objects so the module can be imported in tests without
# starting a real server.
_HTML_PATH = Path(__file__).parent / "templates" / "index.html"


def _resolve_template_by_name_or_id(engine, name: str):
    """Resolve a template by stem name OR by YAML ``id`` field.

    ``TemplateEngine.resolve_template`` only resolves by file stem (the filename
    without extension).  When callers pass a template ``id`` that differs from
    the file stem (e.g. "content-pipeline-v23" from a file named
    "content-pipeline.yaml"), this helper falls back to scanning all templates
    and matching on the ``id`` field.

    Returns:
        ``PipelineTemplate`` instance or ``None`` when not found.
    """
    from orchestration_engine.templates import TemplateNotFoundError

    # 1. Try file-stem resolution (fast path).
    try:
        path = engine.resolve_template(name)
        return engine.load_template(path)
    except (TemplateNotFoundError, FileNotFoundError):
        pass

    # 2. Fallback: scan all templates and match by id.
    for entry in engine.list_templates():
        if entry["id"] == name:
            try:
                from pathlib import Path as _Path
                return engine.load_template(_Path(entry["path"]))
            except Exception:
                return None

    return None


def create_app():  # noqa: C901
    """Create and return the FastAPI application.

    Returns:
        FastAPI application instance.
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    from sse_starlette.sse import EventSourceResponse

    from orchestration_engine import __version__
    from orchestration_engine.templates import TemplateEngine, TemplateNotFoundError

    app = FastAPI(
        title="Orchestration Engine Web UI",
        version=__version__,
        description="Local web server for running orchestration pipelines in the browser.",
    )

    # CORS — allow all origins for local development.
    # Fix 3: Drop allow_credentials (not needed for a local dev tool without cookies).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # In-memory run registry: run_id → run state dict
    _active_runs: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Pydantic models                                                      #
    # ------------------------------------------------------------------ #

    class RunRequest(BaseModel):
        template: str
        # Fix 4: Validate mode with Literal to reject unknown values (returns 422).
        mode: Literal["dry-run", "standalone", "openclaw"] = "dry-run"
        input: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Routes                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def serve_spa() -> HTMLResponse:
        """Serve the single-page HTML application."""
        html = _HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        """Return server health status."""
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/api/templates")
    async def list_templates() -> JSONResponse:
        """List all discoverable pipeline templates."""
        engine = TemplateEngine()
        raw_templates = engine.list_templates()
        # Rename 'phases' count key to 'phases_count' for the UI, and add
        # category, author, and basic phases info for the template selector UI.
        result = []
        for t in raw_templates:
            # Load the full template object to get category, author, and phases.
            try:
                from pathlib import Path as _Path
                tpl = engine.load_template(_Path(t["path"]))
                category = tpl.category or (
                    tpl.phases[0].task_type if tpl.phases else "general"
                )
                author = tpl.author or ""
                phases_summary = [
                    {
                        "id": p.id,
                        "name": p.name,
                        "model_tier": p.model_tier,
                    }
                    for p in tpl.phases
                ]
            except Exception:
                category = "general"
                author = ""
                phases_summary = []

            result.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "version": t["version"],
                    "phases_count": t["phases"],
                    "description": t.get("description", ""),
                    "source": t.get("source", ""),
                    "category": category,
                    "author": author,
                    "phases": phases_summary,
                }
            )
        return JSONResponse(result)

    @app.get("/api/templates/{name}")
    async def get_template(name: str) -> JSONResponse:
        """Return detail for a single template including phase list."""
        engine = TemplateEngine()
        template = _resolve_template_by_name_or_id(engine, name)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")

        phases_data: List[Dict[str, Any]] = []
        for phase in template.phases:
            phases_data.append(
                {
                    "id": phase.id,
                    "name": phase.name,
                    "description": phase.description,
                    "model_tier": phase.model_tier,
                    "thinking_level": phase.thinking_level,
                    "depends_on": phase.depends_on,
                    "task_type": phase.task_type,
                }
            )

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

    @app.post("/api/run")
    async def start_run(req: RunRequest) -> JSONResponse:
        """Start a pipeline run and return a run_id for SSE polling."""
        # Fix 2: Cleanup completed runs older than 1 hour to prevent unbounded growth.
        cutoff = time.time() - 3600
        to_remove = [
            rid for rid, r in _active_runs.items()
            if r.get("completed_at", 0) < cutoff and r["status"] == "completed"
        ]
        for rid in to_remove:
            del _active_runs[rid]

        # Validate template exists
        engine = TemplateEngine()
        template = _resolve_template_by_name_or_id(engine, req.template)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{req.template}' not found")

        run_id = str(uuid.uuid4())

        # Fix 1: Add an asyncio.Queue for thread-safe SSE event delivery.
        run_state: Dict[str, Any] = {
            "run_id": run_id,
            "template": req.template,
            "mode": req.mode,
            "input": req.input,
            "status": "starting",
            "phases_completed": [],
            "phases_failed": [],
            "events": [],           # list of event dicts for SSE replay / history
            "event_queue": asyncio.Queue(),  # Fix 1: thread-safe delivery channel
            "done": False,
            "error": None,
        }
        _active_runs[run_id] = run_state

        # Launch execution in a background asyncio task
        asyncio.create_task(_execute_pipeline(run_id, template, req.mode, req.input, _active_runs))

        return JSONResponse({"run_id": run_id})

    @app.get("/api/run/{run_id}/status")
    async def run_status_sse(run_id: str, request: Request) -> EventSourceResponse:
        """SSE stream delivering phase-completion events for a run."""
        if run_id not in _active_runs:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        async def event_generator() -> AsyncGenerator[Dict[str, str], None]:
            run = _active_runs[run_id]
            queue: asyncio.Queue = run["event_queue"]

            # Fix 1: Consume from the asyncio.Queue instead of polling a list.
            while True:
                if await request.is_disconnected():
                    break

                try:
                    # Wait up to 0.5 s so we can re-check disconnect status.
                    event_data = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield {"data": event_data}
                    queue.task_done()
                except asyncio.TimeoutError:
                    # No new event yet — check if we're done.
                    if run["done"] and queue.empty():
                        break

        return EventSourceResponse(event_generator())

    return app


# ------------------------------------------------------------------ #
# Background execution helper                                          #
# ------------------------------------------------------------------ #

async def _execute_pipeline(
    run_id: str,
    template: Any,
    mode: str,
    initial_input: Dict[str, Any],
    active_runs: Dict[str, Any],
) -> None:
    """Execute a pipeline template in the background, pushing SSE events."""
    import json as _json
    from orchestration_engine.pipeline_runner import PipelineRunner
    from orchestration_engine.sequencer import PhaseSequencer

    run = active_runs[run_id]
    run["status"] = "running"

    # Fix 5: Use asyncio.get_running_loop() (replaces deprecated get_event_loop).
    loop = asyncio.get_running_loop()

    def _push_event(event_type: str, payload: Dict[str, Any]) -> None:
        """Push an event to both the history list and the SSE queue (thread-safe)."""
        payload["type"] = event_type
        serialized = _json.dumps(payload)
        # Append to history list AND enqueue for live SSE consumers.
        # Both mutations happen via call_soon_threadsafe when called from a thread.
        run["events"].append(serialized)
        loop.call_soon_threadsafe(run["event_queue"].put_nowait, serialized)

    _push_event("start", {"run_id": run_id, "template": template.id, "mode": mode})

    # Per-phase timing: phase_id → start time (float)
    _phase_start_times: Dict[str, float] = {}
    # Running totals
    _total_tokens_in: int = 0
    _total_tokens_out: int = 0
    _total_cost: float = 0.0
    _pipeline_start_time: float = time.time()

    def on_phase_start(phase_id: str, phase: Any, wave_index: int) -> None:
        """Fires just before a phase begins executing."""
        _phase_start_times[phase_id] = time.time()
        payload = _json.dumps({
            "type": "phase_start",
            "phase_id": phase_id,
            "phase_name": getattr(phase, "name", phase_id),
            "model_tier": getattr(phase, "model_tier", ""),
            "wave": wave_index,
        })
        run["events"].append(payload)
        loop.call_soon_threadsafe(run["event_queue"].put_nowait, payload)

    def on_phase_complete(phase_id: str, phase_result: dict) -> None:
        # Fix 1: This callback runs in a thread-pool thread; use call_soon_threadsafe
        # to safely deliver events to the asyncio event loop.
        nonlocal _total_tokens_in, _total_tokens_out, _total_cost

        state = phase_result.get("state", "unknown")
        state_val = state.value if hasattr(state, "value") else str(state)

        tokens_consumed = int(phase_result.get("tokens_consumed", 0) or 0)
        cost = float(phase_result.get("cost_usd", 0) or 0)
        # Split tokens: use tokens_in/tokens_out if available, else split evenly
        tokens_in = int(phase_result.get("tokens_in", tokens_consumed // 2) or 0)
        tokens_out = int(phase_result.get("tokens_out", tokens_consumed - tokens_in) or 0)

        elapsed = 0.0
        if phase_id in _phase_start_times:
            elapsed = round(time.time() - _phase_start_times[phase_id], 2)

        # Accumulate totals
        _total_tokens_in += tokens_in
        _total_tokens_out += tokens_out
        _total_cost += cost

        if state_val in ("failed", "permanently_failed"):
            run["phases_failed"].append(phase_id)
            payload = _json.dumps({
                "type": "phase_error",
                "phase_id": phase_id,
                "phase_name": phase_id,
                "status": state_val,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost,
                "elapsed_seconds": elapsed,
                "error_message": str(phase_result.get("errors", [{}])[0]) if phase_result.get("errors") else state_val,
            })
            run["events"].append(payload)
            loop.call_soon_threadsafe(run["event_queue"].put_nowait, payload)
        else:
            run["phases_completed"].append(phase_id)
            payload = _json.dumps({
                "type": "phase_complete",
                "phase_id": phase_id,
                "phase_name": phase_id,
                "status": state_val,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost,
                "elapsed_seconds": elapsed,
            })
            run["events"].append(payload)
            loop.call_soon_threadsafe(run["event_queue"].put_nowait, payload)

    try:
        def _run_sync() -> dict:
            if mode == "standalone":
                runner_ctx = PipelineRunner.standalone()
            elif mode == "openclaw":
                runner_ctx = PipelineRunner.openclaw()
            else:
                runner_ctx = PipelineRunner.dry_run()

            with runner_ctx as runner:
                sequencer = PhaseSequencer(
                    template,
                    runner,
                    config=initial_input,
                    on_phase_complete=on_phase_complete,
                    on_phase_start=on_phase_start,
                )
                return sequencer.execute(initial_input)

        result = await loop.run_in_executor(None, _run_sync)

        total_elapsed = round(time.time() - _pipeline_start_time, 2)
        total_phases = len(template.phases)
        completed = len(run["phases_completed"])
        failed = len(run["phases_failed"])

        if result.get("aborted"):
            run["status"] = "aborted"
            _push_event("aborted", {"failed_phase": result.get("failed_phase", "unknown")})
            _push_event("pipeline_complete", {
                "status": "aborted",
                "total_phases": total_phases,
                "completed": completed,
                "failed": failed,
                "total_tokens": _total_tokens_in + _total_tokens_out,
                "total_tokens_in": _total_tokens_in,
                "total_tokens_out": _total_tokens_out,
                "total_cost": round(_total_cost, 6),
                "total_elapsed": total_elapsed,
            })
        else:
            run["status"] = "completed"
            _push_event("complete", {"phases": completed})
            _push_event("pipeline_complete", {
                "status": "completed",
                "total_phases": total_phases,
                "completed": completed,
                "failed": failed,
                "total_tokens": _total_tokens_in + _total_tokens_out,
                "total_tokens_in": _total_tokens_in,
                "total_tokens_out": _total_tokens_out,
                "total_cost": round(_total_cost, 6),
                "total_elapsed": total_elapsed,
            })

    except Exception as exc:
        logger.exception("Pipeline run %s failed: %s", run_id, exc)
        run["status"] = "error"
        run["error"] = str(exc)
        _push_event("error", {"message": str(exc)})
        total_elapsed = round(time.time() - _pipeline_start_time, 2)
        _push_event("pipeline_complete", {
            "status": "error",
            "total_phases": len(template.phases),
            "completed": len(run["phases_completed"]),
            "failed": len(run["phases_failed"]),
            "total_tokens": _total_tokens_in + _total_tokens_out,
            "total_tokens_in": _total_tokens_in,
            "total_tokens_out": _total_tokens_out,
            "total_cost": round(_total_cost, 6),
            "total_elapsed": total_elapsed,
        })
    finally:
        # Fix 2: Record completion timestamp for TTL-based cleanup.
        run["completed_at"] = time.time()
        run["done"] = True
