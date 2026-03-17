"""MCP tool definitions for the Orchestration Engine.

Implements three core MCP tools (orchemist_launch, orchemist_status,
orchemist_logs) exposed to IDE integrations via the Model Context Protocol.

Tools are registered on a FastMCP instance via the ``register_tools(mcp)``
function, which is called from ``server.py`` after constructing the FastMCP
instance.  This pattern avoids module-level singletons and circular imports.
"""

import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from orchestration_engine.db import Database
from orchestration_engine.templates import TemplateEngine, TemplateNotFoundError

logger = logging.getLogger(__name__)

_VALID_MODES = {"dry-run", "standalone", "openclaw"}


def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB, mirroring cli.py."""
    default_dir = Path.home() / ".orchestration-engine"
    default_dir.mkdir(parents=True, exist_ok=True)
    return str(default_dir / "engine.db")


def _parse_json_list(val: Any) -> list:
    """Safely parse completed_phases from DB (may be JSON string, list, or None)."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


def register_tools(mcp) -> None:
    """Register all three MCP tools on the given FastMCP instance.

    Args:
        mcp: A ``FastMCP`` instance on which to register the three tools.
             Called from ``server.py`` immediately after constructing the
             local ``mcp`` instance, before starting the transport.

    Note:
        Also applies a compatibility shim to ``mcp.call_tool`` so it returns
        just the content sequence (``list[ContentBlock]``) for MCP >= 1.26.0,
        which changed the return type to a ``(content, metadata)`` tuple.
    """
    # --- MCP >= 1.26 compatibility: call_tool returns (content, meta) tuple ---
    _original_call_tool = mcp.call_tool

    async def _compat_call_tool(name: str, arguments: dict) -> list:
        result = await _original_call_tool(name, arguments)
        if isinstance(result, tuple):
            return result[0]
        return result

    mcp.call_tool = _compat_call_tool
    # -------------------------------------------------------------------------

    @mcp.tool(
        name="orchemist_launch",
        description="Launch a pipeline run by template ID and return the run_id.",
    )
    async def orchemist_launch(
        template_id: str,
        mode: str = "dry-run",
        inputs: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Launch a pipeline run by template ID.

        Args:
            template_id: ID or path of the template to run.
            mode: Execution mode — one of ``dry-run``, ``standalone``, ``openclaw``.
            inputs: Optional dict of pipeline input values.

        Returns:
            JSON string ``{"run_id": ..., "status": "running"}`` on success,
            or an error string on failure.
        """
        # --- Validate required parameters ---
        if not template_id:
            return "Missing required parameter: template_id"

        if mode not in _VALID_MODES:
            return (
                f"Invalid mode: {mode}. "
                f"Supported modes: dry-run, standalone, openclaw"
            )

        try:
            engine = TemplateEngine()
            try:
                template_file = engine.resolve_template(template_id)
                template = engine.load_template(template_file)
            except (TemplateNotFoundError, FileNotFoundError, KeyError, ValueError):
                return f"Template not found: {template_id}"

            # Build run ID and output directory (mirrors cli.py _launch_openclaw)
            run_id = str(uuid.uuid4())[:8]
            safe_id = re.sub(r"[^\w\-]", "_", template.id)
            output_dir = Path(
                f"./output/{safe_id}"
                f"-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-{run_id}"
            )
            output_dir.mkdir(parents=True, exist_ok=True)

            # Persist run record to DB
            effective_db_path = _get_persistent_db_path()
            db = Database(Path(effective_db_path))
            db.insert_pipeline_run(
                {
                    "run_id": run_id,
                    "template_path": str(template_file.resolve()),
                    "template_id": template.id,
                    "input_json": json.dumps(inputs or {}),
                    "mode": mode,
                    "output_dir": str(output_dir.resolve()),
                    "gateway_url": None,
                    "skip_scoring": 0,
                    "status": "pending",
                }
            )

            # Spawn daemon process (non-blocking, matches cli.py pattern)
            log_file_path = output_dir / ".orch-daemon.log"
            with log_file_path.open("a") as log_fh:
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

            return json.dumps({"run_id": run_id, "status": "running"})

        except Exception as exc:
            logger.error("orchemist_launch error: %s", exc, exc_info=True)
            return "Orchemist API not reachable"

    @mcp.tool(
        name="orchemist_status",
        description="Get the status and progress of a pipeline run.",
    )
    async def orchemist_status(run_id: str) -> str:
        """Get the current status of a pipeline run.

        Args:
            run_id: The run ID returned by ``orchemist_launch``.

        Returns:
            JSON string with ``run_id``, ``status``, ``current_phase``,
            ``completed_phases``, ``elapsed`` (float seconds), and ``score``
            (float or null).  Returns an error string on failure.
        """
        try:
            db = Database(Path(_get_persistent_db_path()))
            run = db.get_pipeline_run(run_id)
            if run is None:
                return f"Run not found: {run_id}"

            started_at = run.get("started_at")
            completed_at = run.get("completed_at")

            if started_at is None:
                elapsed = 0.0
            elif completed_at is not None:
                elapsed = float((completed_at - started_at).total_seconds())
            else:
                elapsed = float((datetime.now() - started_at).total_seconds())

            return json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "current_phase": run.get("current_phase"),
                    "completed_phases": _parse_json_list(run.get("completed_phases")),
                    "elapsed": elapsed,
                    "score": run.get("scoring_score"),
                }
            )

        except Exception as exc:
            logger.error("orchemist_status error: %s", exc, exc_info=True)
            return "Orchemist API not reachable"

    @mcp.tool(
        name="orchemist_logs",
        description="Retrieve logs for a pipeline run, optionally filtered to a specific phase.",
    )
    async def orchemist_logs(run_id: str, phase: Optional[str] = None) -> str:
        """Retrieve log content for a pipeline run.

        Args:
            run_id: The run ID returned by ``orchemist_launch``.
            phase: Optional phase name.  When provided, returns only the
                   output file for that phase (``<output_dir>/<phase>.md``).
                   When omitted, returns the full daemon log.

        Returns:
            Log content as plain text, ``"(no logs available)"`` when the
            log file does not yet exist, or an error string on failure.
        """
        try:
            db = Database(Path(_get_persistent_db_path()))
            run = db.get_pipeline_run(run_id)
            if run is None:
                return f"Run not found: {run_id}"

            output_dir = Path(run["output_dir"])

            if phase is None:
                log_path = output_dir / ".orch-daemon.log"
                if not log_path.exists():
                    return "(no logs available)"
                return log_path.read_text(encoding="utf-8", errors="replace")
            else:
                phase_path = output_dir / f"{phase}.md"
                if not phase_path.exists():
                    return f"Phase not found: {phase} in run {run_id}"
                return phase_path.read_text(encoding="utf-8", errors="replace")

        except Exception as exc:
            logger.error("orchemist_logs error: %s", exc, exc_info=True)
            return "Orchemist API not reachable"
