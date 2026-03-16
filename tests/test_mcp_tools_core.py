"""Unit tests for MCP core tools — launch, status, logs (Issue #468).

All tests mock HTTP calls — no running server required.
Tests cover all behavioral contracts from the spec (Section A).
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from orchestration_engine.mcp.tools import _ApiNotReachable, register_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_response(status_code: int, data=None, text: str = "") -> MagicMock:
    """Create a mock httpx.Response with the given status code and data."""
    m = MagicMock()
    m.status_code = status_code
    m.is_success = status_code < 400
    if isinstance(data, dict):
        m.json.return_value = data
        m.text = str(data)
    else:
        m.json.side_effect = Exception("not JSON")
        m.text = text
    return m


def make_mcp_with_tools() -> FastMCP:
    """Return a FastMCP instance with all three orchemist tools registered."""
    mcp = FastMCP(name="orchemist-test")
    register_tools(mcp)
    return mcp


def call_tool(mcp: FastMCP, tool_name: str, args: dict) -> str:
    """Invoke an MCP tool synchronously and return its text output."""
    result = asyncio.run(mcp.call_tool(tool_name, args))
    if isinstance(result, list) and result:
        item = result[0]
        return item.text if hasattr(item, "text") else str(item)
    return str(result)


# ---------------------------------------------------------------------------
# orchemist_launch — Happy Path
# ---------------------------------------------------------------------------

class TestOrchemistLaunch:
    def test_valid_template_returns_run_id_and_status(self):
        """Contract: Valid template_id returns 'run_id: <id>' and 'status: running'."""
        with patch("orchestration_engine.mcp.tools._post") as mock_post:
            mock_post.return_value = make_mock_response(201, {"run_id": "abc123", "status": "running"})
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_launch", {"template_id": "hello-pipeline", "mode": "dry-run"})
        assert "run_id: abc123" in output
        assert "status: running" in output

    def test_valid_template_with_inputs_passes_inputs(self):
        """Contract: inputs are forwarded in the payload as 'input' key."""
        with patch("orchestration_engine.mcp.tools._post") as mock_post:
            mock_post.return_value = make_mock_response(201, {"run_id": "def456", "status": "running"})
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_launch", {
                "template_id": "hello-pipeline",
                "mode": "dry-run",
                "inputs": {"greeting": "hello"},
            })
        # Verify the HTTP payload included the inputs
        call_args = mock_post.call_args
        payload = call_args[1].get("json", {}) if call_args[1] else call_args[0][1]
        assert payload.get("input") == {"greeting": "hello"}
        assert "run_id: def456" in output

    def test_invalid_template_returns_not_found_error(self):
        """Contract: 404 from server → 'Template not found: <template_id>'."""
        with patch("orchestration_engine.mcp.tools._post") as mock_post:
            mock_post.return_value = make_mock_response(404, {"detail": "Template 'nonexistent' not found"})
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_launch", {"template_id": "nonexistent"})
        assert output == "Template not found: nonexistent"

    def test_empty_template_id_returns_missing_parameter_error(self):
        """Contract: template_id='' → 'Missing required parameter: template_id'."""
        mcp = make_mcp_with_tools()
        output = call_tool(mcp, "orchemist_launch", {"template_id": ""})
        assert output == "Missing required parameter: template_id"

    def test_api_not_reachable_returns_error_string(self):
        """Contract: API unreachable → 'Orchemist API not reachable'."""
        with patch("orchestration_engine.mcp.tools._post", side_effect=_ApiNotReachable("refused")):
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_launch", {"template_id": "hello-pipeline"})
        assert output == "Orchemist API not reachable"
        assert "Traceback" not in output

    def test_invalid_mode_returns_server_error(self):
        """Contract: unrecognized mode → server returns 422, tool returns 'Error: <detail>'."""
        with patch("orchestration_engine.mcp.tools._post") as mock_post:
            mock_post.return_value = make_mock_response(
                422, {"detail": "Input should be 'standalone', 'openclaw' or 'dry-run'"}
            )
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_launch", {
                "template_id": "hello-pipeline",
                "mode": "invalid-mode",
            })
        assert output.startswith("Error:")


# ---------------------------------------------------------------------------
# orchemist_status — Happy Path
# ---------------------------------------------------------------------------

_RUNNING_RUN = {
    "run_id": "run-001",
    "status": "running",
    "current_phase": "spec",
    "completed_phases": ["init", "spec_adversary"],
    "output_dir": "/tmp/run-001",
    "scoring_score": None,
    "started_at": "2026-01-01T10:00:00+00:00",
    "completed_at": None,
}

_DONE_SCORED_RUN = {
    "run_id": "run-002",
    "status": "success",
    "current_phase": None,
    "completed_phases": ["spec", "acceptance_test", "implement", "review", "scoring"],
    "output_dir": "/tmp/run-002",
    "scoring_score": 0.92,
    "started_at": "2026-01-01T10:00:00+00:00",
    "completed_at": "2026-01-01T10:05:30+00:00",
}

_FAILED_RUN = {
    "run_id": "run-003",
    "status": "failed",
    "current_phase": "implement",
    "completed_phases": ["spec", "acceptance_test"],
    "output_dir": "/tmp/run-003",
    "scoring_score": None,
    "started_at": "2026-01-01T10:00:00+00:00",
    "completed_at": "2026-01-01T10:02:10+00:00",
}


class TestOrchemistStatus:
    def test_valid_run_returns_all_six_fields(self):
        """Contract: Response contains run_id, status, current_phase, completed_phases, elapsed, score."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _RUNNING_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-001"})
        assert "run_id:" in output
        assert "status:" in output
        assert "current_phase:" in output
        assert "completed_phases:" in output
        assert "elapsed:" in output
        assert "score:" in output

    def test_running_pipeline_returns_partial_phases(self):
        """Contract: Running pipeline shows status: running + partial completed_phases."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _RUNNING_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-001"})
        assert "status: running" in output
        assert "init" in output or "spec_adversary" in output

    def test_completed_run_with_score_returns_float(self):
        """Contract: Completed run with score → 'score: 0.92'."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _DONE_SCORED_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-002"})
        assert "score: 0.92" in output

    def test_run_without_score_returns_none(self):
        """Contract: Run with no score → 'score: None' (not omitted)."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _RUNNING_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-001"})
        assert "score: None" in output or "score: null" in output

    def test_failed_run_returns_failed_status_and_last_active_phase(self):
        """Contract: Failed run shows status: failed and current_phase of failing phase."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _FAILED_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-003"})
        assert "status: failed" in output
        assert "current_phase: implement" in output

    def test_failed_run_shows_phases_before_failure(self):
        """Contract: Failed run completed_phases shows only phases before the failure."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, _FAILED_RUN)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-003"})
        assert "spec" in output
        assert "acceptance_test" in output

    def test_invalid_run_id_returns_run_not_found(self):
        """Contract: Unknown run_id → 'Run not found: <run_id>'."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(404, {"detail": "Run 'bad-id' not found"})
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "bad-id"})
        assert output == "Run not found: bad-id"

    def test_api_not_reachable_returns_error_string(self):
        """Contract: API unreachable → 'Orchemist API not reachable'."""
        with patch("orchestration_engine.mcp.tools._get", side_effect=_ApiNotReachable("refused")):
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_status", {"run_id": "run-001"})
        assert output == "Orchemist API not reachable"
        assert "Traceback" not in output


# ---------------------------------------------------------------------------
# orchemist_logs — Happy Path
# ---------------------------------------------------------------------------

class TestOrchemistLogs:
    def test_valid_run_returns_full_log(self):
        """Contract: No phase → returns full daemon log content."""
        run_data = {"run_id": "run1", "output_dir": "/tmp/run1", "completed_phases": ["spec"]}
        log_data = {"run_id": "run1", "log": "Phase spec started\nPhase spec completed\n"}
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.side_effect = [
                make_mock_response(200, run_data),
                make_mock_response(200, log_data),
            ]
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1"})
        assert "Phase spec" in output

    def test_empty_log_returns_empty_string(self):
        """Contract: Run exists but no logs yet → empty string (not an error)."""
        run_data = {"run_id": "run1", "output_dir": "/tmp/run1", "completed_phases": []}
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.side_effect = [
                make_mock_response(200, run_data),
                make_mock_response(404, {"detail": "Log file not found"}),
            ]
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1"})
        assert output == ""

    def test_with_phase_returns_only_that_phase(self, tmp_path):
        """Contract: phase='spec' → returns only spec phase output."""
        phase_md = tmp_path / "spec.md"
        phase_md.write_text("# Spec Output\nThis is the spec phase content.")
        run_data = {"run_id": "run1", "output_dir": str(tmp_path), "completed_phases": ["spec"]}
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, run_data)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1", "phase": "spec"})
        assert "Spec Output" in output

    def test_phase_json_fallback(self, tmp_path):
        """Contract: If .md not present but .json is, return JSON file content."""
        phase_json = tmp_path / "build.json"
        phase_json.write_text('{"result": "ok"}')
        run_data = {"run_id": "run1", "output_dir": str(tmp_path), "completed_phases": ["build"]}
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, run_data)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1", "phase": "build"})
        assert '"result": "ok"' in output

    def test_invalid_run_id_returns_run_not_found(self):
        """Contract: Unknown run_id → 'Run not found: <run_id>'."""
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(404, {"detail": "Run 'bad-id' not found"})
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "bad-id"})
        assert output == "Run not found: bad-id"

    def test_phase_not_found_returns_error(self, tmp_path):
        """Contract: Valid run but missing phase → 'Phase not found: <phase> in run <run_id>'."""
        run_data = {"run_id": "run1", "output_dir": str(tmp_path), "completed_phases": ["spec"]}
        with patch("orchestration_engine.mcp.tools._get") as mock_get:
            mock_get.return_value = make_mock_response(200, run_data)
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1", "phase": "nonexistent"})
        assert output == "Phase not found: nonexistent in run run1"

    def test_api_not_reachable_returns_error_string(self):
        """Contract: API unreachable → 'Orchemist API not reachable'."""
        with patch("orchestration_engine.mcp.tools._get", side_effect=_ApiNotReachable("refused")):
            mcp = make_mcp_with_tools()
            output = call_tool(mcp, "orchemist_logs", {"run_id": "run1"})
        assert output == "Orchemist API not reachable"
        assert "Traceback" not in output


# ---------------------------------------------------------------------------
# Tools Registration
# ---------------------------------------------------------------------------

class TestToolsRegistration:
    def test_all_three_tools_appear_in_list(self):
        """Contract: tools/list includes orchemist_launch, orchemist_status, orchemist_logs."""
        mcp = make_mcp_with_tools()
        tools = asyncio.run(mcp.list_tools())
        names = [t.name for t in tools]
        assert "orchemist_launch" in names
        assert "orchemist_status" in names
        assert "orchemist_logs" in names

    def test_launch_has_required_template_id_param(self):
        """Contract: orchemist_launch schema declares template_id as required."""
        mcp = make_mcp_with_tools()
        tools = asyncio.run(mcp.list_tools())
        tool = next(t for t in tools if t.name == "orchemist_launch")
        schema = tool.inputSchema
        assert "template_id" in schema.get("properties", {})
        assert "template_id" in schema.get("required", [])

    def test_launch_has_optional_mode_and_inputs(self):
        """Contract: orchemist_launch schema declares mode and inputs as optional."""
        mcp = make_mcp_with_tools()
        tools = asyncio.run(mcp.list_tools())
        tool = next(t for t in tools if t.name == "orchemist_launch")
        schema = tool.inputSchema
        props = schema.get("properties", {})
        required = schema.get("required", [])
        assert "mode" in props
        assert "inputs" in props
        assert "mode" not in required
        assert "inputs" not in required

    def test_status_has_required_run_id_only(self):
        """Contract: orchemist_status schema declares only run_id as required."""
        mcp = make_mcp_with_tools()
        tools = asyncio.run(mcp.list_tools())
        tool = next(t for t in tools if t.name == "orchemist_status")
        schema = tool.inputSchema
        assert "run_id" in schema.get("properties", {})
        assert "run_id" in schema.get("required", [])

    def test_logs_has_required_run_id_optional_phase(self):
        """Contract: orchemist_logs schema has run_id (required) and phase (optional)."""
        mcp = make_mcp_with_tools()
        tools = asyncio.run(mcp.list_tools())
        tool = next(t for t in tools if t.name == "orchemist_logs")
        schema = tool.inputSchema
        props = schema.get("properties", {})
        required = schema.get("required", [])
        assert "run_id" in props
        assert "run_id" in required
        assert "phase" in props
        assert "phase" not in required
