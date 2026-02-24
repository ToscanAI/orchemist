"""FastAPI web application for the Orchestration Engine web UI.

Provides REST endpoints for template listing, pipeline execution, and
Server-Sent Events (SSE) for live progress streaming.

All dependencies (fastapi, uvicorn, sse-starlette) are optional — import this
module only after confirming they are installed (see cli.py serve command).
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
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
        mode: str = "dry-run"
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
        templates = engine.list_templates()
        # Rename 'phases' count key to 'phases_count' for the UI
        result = []
        for t in templates:
            result.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "version": t["version"],
                    "phases_count": t["phases"],
                    "description": t.get("description", ""),
                    "source": t.get("source", ""),
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
            }
        )

    @app.post("/api/run")
    async def start_run(req: RunRequest) -> JSONResponse:
        """Start a pipeline run and return a run_id for SSE polling."""
        # Validate template exists
        engine = TemplateEngine()
        template = _resolve_template_by_name_or_id(engine, req.template)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{req.template}' not found")

        run_id = str(uuid.uuid4())

        run_state: Dict[str, Any] = {
            "run_id": run_id,
            "template": req.template,
            "mode": req.mode,
            "input": req.input,
            "status": "starting",
            "phases_completed": [],
            "phases_failed": [],
            "events": [],   # list of event dicts for SSE replay
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
            sent_index = 0
            while True:
                if await request.is_disconnected():
                    break

                events = run["events"]
                while sent_index < len(events):
                    yield {"data": events[sent_index]}
                    sent_index += 1

                if run["done"]:
                    break

                await asyncio.sleep(0.1)

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

    def _push_event(event_type: str, payload: Dict[str, Any]) -> None:
        payload["type"] = event_type
        run["events"].append(_json.dumps(payload))

    _push_event("start", {"run_id": run_id, "template": template.id, "mode": mode})

    def on_phase_complete(phase_id: str, phase_result: dict) -> None:
        state = phase_result.get("state", "unknown")
        state_val = state.value if hasattr(state, "value") else str(state)
        tokens = phase_result.get("tokens_consumed", 0)
        cost = float(phase_result.get("cost_usd", 0) or 0)

        if state_val in ("failed", "permanently_failed"):
            run["phases_failed"].append(phase_id)
            _push_event(
                "phase_failed",
                {
                    "phase_id": phase_id,
                    "state": state_val,
                    "tokens": tokens,
                    "cost_usd": cost,
                },
            )
        else:
            run["phases_completed"].append(phase_id)
            _push_event(
                "phase_complete",
                {
                    "phase_id": phase_id,
                    "state": state_val,
                    "tokens": tokens,
                    "cost_usd": cost,
                },
            )

    try:
        # Build the runner — all modes run synchronously in a thread pool to
        # avoid blocking the asyncio event loop.
        loop = asyncio.get_event_loop()

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
                )
                return sequencer.execute(initial_input)

        result = await loop.run_in_executor(None, _run_sync)

        if result.get("aborted"):
            run["status"] = "aborted"
            _push_event("aborted", {"failed_phase": result.get("failed_phase", "unknown")})
        else:
            run["status"] = "complete"
            _push_event("complete", {"phases": len(run["phases_completed"])})

    except Exception as exc:
        logger.exception("Pipeline run %s failed: %s", run_id, exc)
        run["status"] = "error"
        run["error"] = str(exc)
        _push_event("error", {"message": str(exc)})
    finally:
        run["done"] = True
