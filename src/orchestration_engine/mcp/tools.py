"""MCP tool implementations for Orchemist.

Registers three tools on a FastMCP instance:
- orchemist_launch: Launch a pipeline run via POST /api/v1/runs
- orchemist_status: Get run status via GET /api/v1/runs/{run_id}
- orchemist_logs: Get run logs (full or per-phase)

Note: Per-phase logs are read from the filesystem at <output_dir>/<phase>.md.
This requires the MCP server to run on the same machine as the pipeline daemon.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

_API_BASE = os.environ.get("ORCHEMIST_API_URL", "http://localhost:8375")


class _ApiNotReachable(Exception):
    """Raised when the Orchemist REST API cannot be contacted."""


def _get(url: str) -> httpx.Response:
    """Make a GET request; raise _ApiNotReachable on connection failure."""
    try:
        return httpx.get(url, timeout=10.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException, OSError) as e:
        raise _ApiNotReachable(str(e))


def _post(url: str, json: dict) -> httpx.Response:
    """Make a POST request; raise _ApiNotReachable on connection failure."""
    try:
        return httpx.post(url, json=json, timeout=10.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException, OSError) as e:
        raise _ApiNotReachable(str(e))


def _format_elapsed(started_at: Optional[str], completed_at: Optional[str]) -> str:
    """Compute human-readable elapsed time from ISO timestamp strings."""
    if not started_at:
        return "N/A"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end_str = completed_at if completed_at else datetime.now(timezone.utc).isoformat()
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        total_seconds = int((end - start).total_seconds())
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
    except Exception:
        return "N/A"


def register_tools(mcp: FastMCP) -> None:
    """Register all orchemist tools on the given FastMCP instance.

    Args:
        mcp: The FastMCP instance to register tools on.
    """

    @mcp.tool()
    def orchemist_launch(
        template_id: str,
        mode: str = "dry-run",
        inputs: Optional[dict] = None,
    ):
        """Launch a pipeline run.

        Args:
            template_id: The template to run. Required.
            mode: Execution mode — 'dry-run', 'standalone', or 'openclaw'. Default: 'dry-run'.
            inputs: Optional key/value map of inputs to pass to the pipeline.

        Returns:
            On success: 'run_id: <id>\\nstatus: running'
            On error: descriptive error message.
        """
        if not template_id:
            return "Missing required parameter: template_id"

        payload = {
            "template_id": template_id,
            "mode": mode,
            "input": inputs or {},
        }
        try:
            resp = _post(f"{_API_BASE}/api/v1/runs", json=payload)
        except _ApiNotReachable:
            return "Orchemist API not reachable"

        if resp.status_code == 404:
            return f"Template not found: {template_id}"

        if not resp.is_success:
            try:
                detail = resp.json().get("detail", "Unknown error")
            except Exception:
                detail = resp.text or "Unknown error"
            return f"Error: {detail}"

        data = resp.json()
        return f"run_id: {data['run_id']}\nstatus: {data['status']}"

    @mcp.tool()
    def orchemist_status(run_id: str):
        """Get the current status of a pipeline run.

        Args:
            run_id: The run identifier to query.

        Returns:
            Multi-line text with fields: run_id, status, current_phase,
            completed_phases, elapsed, score.
            On error: descriptive error message.
        """
        try:
            resp = _get(f"{_API_BASE}/api/v1/runs/{run_id}")
        except _ApiNotReachable:
            return "Orchemist API not reachable"

        if resp.status_code == 404:
            return f"Run not found: {run_id}"

        data = resp.json()
        elapsed = _format_elapsed(data.get("started_at"), data.get("completed_at"))
        score = data.get("scoring_score")  # float or None
        completed = data.get("completed_phases") or []
        completed_str = ", ".join(completed) if completed else "none"

        lines = [
            f"run_id: {data['run_id']}",
            f"status: {data['status']}",
            f"current_phase: {data.get('current_phase') or 'N/A'}",
            f"completed_phases: {completed_str}",
            f"elapsed: {elapsed}",
            f"score: {score}",
        ]
        return "\n".join(lines)

    @mcp.tool()
    def orchemist_logs(run_id: str, phase: Optional[str] = None):
        """Get logs for a pipeline run, optionally filtered by phase.

        Args:
            run_id: The run identifier to query.
            phase: Optional phase name. If provided, returns only output for that phase.

        Returns:
            Log content as plain text. Empty string if no logs yet.
            On error: descriptive error message.
        """
        try:
            resp = _get(f"{_API_BASE}/api/v1/runs/{run_id}")
        except _ApiNotReachable:
            return "Orchemist API not reachable"

        if resp.status_code == 404:
            return f"Run not found: {run_id}"

        run_data = resp.json()

        if phase:
            output_dir = Path(run_data.get("output_dir", ""))
            phase_md = output_dir / f"{phase}.md"
            phase_json = output_dir / f"{phase}.json"
            if phase_md.exists():
                return phase_md.read_text(encoding="utf-8", errors="replace")
            if phase_json.exists():
                return phase_json.read_text(encoding="utf-8", errors="replace")
            return f"Phase not found: {phase} in run {run_id}"
        else:
            try:
                log_resp = _get(f"{_API_BASE}/api/v1/runs/{run_id}/logs")
            except _ApiNotReachable:
                return "Orchemist API not reachable"

            if log_resp.status_code == 404:
                # Log file not yet created — run exists but has produced no output yet
                return ""

            return log_resp.json().get("log", "")
