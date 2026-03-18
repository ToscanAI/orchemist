"""
E2E integration test for the full MCP tool chain.

Tests the complete flow without mocking any internal components:
  initialize → tools/list → launch → status → logs → error paths

Uses hello-pipeline in dry-run mode. No internal mocks.

Issue: #471 — MCP E2E Integration Test
"""

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import pytest

from mcp.server.fastmcp import FastMCP
from orchestration_engine.mcp.tools import register_tools, _get_persistent_db_path
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def call_tool(mcp_server, tool_name, **kwargs):
    """Call an MCP tool and return its string text result."""
    result = asyncio.run(mcp_server.call_tool(tool_name, kwargs))
    if isinstance(result, list) and result:
        item = result[0]
        return item.text if hasattr(item, "text") else str(item)
    return str(result)


@pytest.fixture(scope="module")
def mcp_server():
    """Real FastMCP instance with tools registered. No mocking."""
    mcp = FastMCP(name="test-orchemist-e2e")
    register_tools(mcp)
    return mcp


@pytest.fixture(scope="module")
def all_tools(mcp_server):
    """Resolved list of all tools from the live server."""
    return asyncio.run(mcp_server.list_tools())


@pytest.fixture(scope="module", autouse=True)
def cleanup_orphan_processes():
    """
    Collect run_ids from E2E tests and terminate their daemon processes on teardown.

    Tests that launch pipelines must declare this fixture as a parameter and
    append their run_ids to the yielded list so teardown can kill daemons.
    """
    launched_run_ids = []
    yield launched_run_ids

    # Teardown: SIGTERM all daemon processes spawned during tests
    db_path = Path(_get_persistent_db_path())
    if db_path.exists():
        db = Database(str(db_path))
        for run_id in launched_run_ids:
            try:
                run = db.get_pipeline_run(run_id)
                if run and run.get("pid"):
                    pid = run["pid"]
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass  # Already exited — fine
            except Exception:
                pass  # Don't let cleanup failures mask test failures


# ---------------------------------------------------------------------------
# Test 1 — Tool Discovery: exactly 3 tools are exposed
# ---------------------------------------------------------------------------


class TestE2EToolDiscovery:
    """Verify MCP server exposes exactly the expected tools with correct schemas."""

    def test_server_exposes_exactly_three_tools(self, all_tools):
        """
        BEHAVIORAL CONTRACT:
        When an MCP client requests the list of available tools, the server
        returns exactly 3 tools: orchemist_launch, orchemist_status, and
        orchemist_logs — no more, no fewer.
        """
        tool_names = {t.name for t in all_tools}
        assert tool_names == {
            "orchemist_launch",
            "orchemist_status",
            "orchemist_logs",
        }, (
            f"Expected exactly 3 tools, got: {tool_names}"
        )

    def test_orchemist_launch_schema(self, all_tools):
        """
        BEHAVIORAL CONTRACT:
        orchemist_launch has template_id as a required string parameter;
        mode and inputs are present in the schema but NOT required.
        """
        launch = next(t for t in all_tools if t.name == "orchemist_launch")
        schema = launch.inputSchema
        required = schema.get("required", [])
        props = schema.get("properties", {})

        assert "template_id" in required, "template_id must be required"
        assert props.get("template_id", {}).get("type") == "string", (
            "template_id must be type string"
        )
        assert "mode" not in required, "mode must NOT be required"
        assert "inputs" not in required, "inputs must NOT be required"
        assert "mode" in props, "mode must be present in properties"
        assert "inputs" in props, "inputs must be present in properties"

    def test_orchemist_status_schema(self, all_tools):
        """
        BEHAVIORAL CONTRACT:
        orchemist_status has run_id as a required string parameter and no
        other required parameters.
        """
        status = next(t for t in all_tools if t.name == "orchemist_status")
        schema = status.inputSchema
        required = schema.get("required", [])

        assert "run_id" in required, "run_id must be required"
        assert schema.get("properties", {}).get("run_id", {}).get("type") == "string", (
            "run_id must be type string"
        )
        assert set(required) == {"run_id"}, (
            f"orchemist_status must have ONLY run_id as required, got: {required}"
        )

    def test_orchemist_logs_schema(self, all_tools):
        """
        BEHAVIORAL CONTRACT:
        orchemist_logs has run_id as a required string parameter; phase is
        present in the schema but NOT required.
        """
        logs = next(t for t in all_tools if t.name == "orchemist_logs")
        schema = logs.inputSchema
        required = schema.get("required", [])
        props = schema.get("properties", {})

        assert "run_id" in required, "run_id must be required"
        assert props.get("run_id", {}).get("type") == "string", (
            "run_id must be type string"
        )
        assert "phase" not in required, "phase must NOT be required"
        assert "phase" in props, "phase must be present in properties"


# ---------------------------------------------------------------------------
# Test 2 — Full E2E flow: launch → status → logs
# ---------------------------------------------------------------------------


class TestE2EFullFlow:
    """Exercise the complete launch → status → logs chain with real infrastructure."""

    def test_e2e_launch_status_logs(self, mcp_server, cleanup_orphan_processes):
        """
        BEHAVIORAL CONTRACT:
        A complete E2E flow using hello-pipeline in dry-run mode:
          1. Launch returns run_id + status "running"
          2. Immediate status call returns all six typed fields
          3. Logs (no phase) return valid content without "Run not found:"
          4. Logs (phase="hello") return valid content without "Run not found:"
        """
        # --- LAUNCH ---
        launch_result = call_tool(
            mcp_server,
            "orchemist_launch",
            template_id="hello-pipeline",
            mode="dry-run",
        )
        launch_data = json.loads(launch_result)
        assert "run_id" in launch_data, "launch must return run_id"
        assert isinstance(launch_data["run_id"], str) and launch_data["run_id"], (
            "run_id must be a non-empty string"
        )
        assert launch_data["status"] == "running", "initial status must be 'running'"
        run_id = launch_data["run_id"]
        cleanup_orphan_processes.append(run_id)

        # --- STATUS (immediate — DB write happens before daemon spawn) ---
        status_result = call_tool(mcp_server, "orchemist_status", run_id=run_id)
        status_data = json.loads(status_result)

        # Field presence
        required_fields = {
            "run_id",
            "status",
            "current_phase",
            "completed_phases",
            "elapsed",
            "score",
        }
        assert required_fields.issubset(status_data.keys()), (
            f"Missing fields: {required_fields - set(status_data.keys())}"
        )

        # Field types (all six — Finding 6)
        assert isinstance(status_data["run_id"], str), "run_id must be str"
        assert isinstance(status_data["status"], str), "status must be str"
        assert isinstance(status_data["completed_phases"], list), (
            "completed_phases must be list"
        )
        assert isinstance(status_data["elapsed"], float), (
            f"elapsed must be float, got {type(status_data['elapsed'])}"
        )
        assert status_data["score"] is None or isinstance(status_data["score"], float), (
            f"score must be float or null, got: {status_data['score']!r}"
        )
        assert status_data["current_phase"] is None or isinstance(
            status_data["current_phase"], str
        ), f"current_phase must be str or null, got: {status_data['current_phase']!r}"

        # run_id consistency
        assert status_data["run_id"] == run_id, (
            "run_id in status must match launched run_id"
        )

        # --- LOGS: no phase (allow daemon to start writing) ---
        time.sleep(0.5)
        logs_full = call_tool(mcp_server, "orchemist_logs", run_id=run_id)
        assert isinstance(logs_full, str), "logs response must be a string"
        assert "Run not found:" not in logs_full, (
            f"Unexpected 'Run not found:' in full logs: {logs_full!r}"
        )
        # Response is either the sentinel or actual log content
        assert logs_full == "(no logs available)" or len(logs_full) > 0, (
            "logs must be '(no logs available)' or non-empty"
        )

        # --- LOGS: phase-filtered (phase="hello" — the real phase name) ---
        logs_phase = call_tool(
            mcp_server, "orchemist_logs", run_id=run_id, phase="hello"
        )
        assert isinstance(logs_phase, str), "phase-filtered logs must be a string"
        assert "Run not found:" not in logs_phase, (
            f"Unexpected 'Run not found:' in phase-filtered logs: {logs_phase!r}"
        )
        if "Phase not found:" in logs_phase:
            assert logs_phase == f"Phase not found: hello in run {run_id}", (
                f"Phase-not-found message must have correct format, got: {logs_phase!r}"
            )
        # Otherwise it's phase output content — no further assertion on richness


# ---------------------------------------------------------------------------
# Test 3 — Error handling: all documented error paths
# ---------------------------------------------------------------------------


class TestE2EErrorHandling:
    """Verify all documented error paths return exactly the specified strings."""

    def test_launch_nonexistent_template(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given template_id='nonexistent-template', the response is exactly
        'Template not found: nonexistent-template'.
        """
        result = call_tool(
            mcp_server, "orchemist_launch", template_id="nonexistent-template"
        )
        assert result == "Template not found: nonexistent-template", (
            f"Expected exact error string, got: {result!r}"
        )

    def test_launch_empty_template_id(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given template_id='', the response is exactly
        'Missing required parameter: template_id'.
        """
        result = call_tool(mcp_server, "orchemist_launch", template_id="")
        assert result == "Missing required parameter: template_id", (
            f"Expected missing-parameter error, got: {result!r}"
        )

    def test_launch_invalid_mode(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given a mode not in {dry-run, standalone, openclaw}, the response is
        'Invalid mode: <mode>. Supported modes: dry-run, standalone, openclaw'.
        """
        invalid_mode = "turbo-mode"
        result = call_tool(
            mcp_server,
            "orchemist_launch",
            template_id="hello-pipeline",
            mode=invalid_mode,
        )
        expected = (
            f"Invalid mode: {invalid_mode}. "
            "Supported modes: dry-run, standalone, openclaw"
        )
        assert result == expected, (
            f"Expected exact error for invalid mode, got: {result!r}"
        )

    def test_status_nonexistent_run_id(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given a run_id that does not exist, orchemist_status returns a string
        containing 'Run not found:'.
        """
        fake_id = "definitely-not-a-real-run-id"
        result = call_tool(mcp_server, "orchemist_status", run_id=fake_id)
        assert "Run not found:" in result, (
            f"Expected 'Run not found:' for unknown run_id, got: {result!r}"
        )

    def test_logs_nonexistent_run_id_no_phase(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given a run_id that does not exist (no phase), orchemist_logs returns
        a string containing 'Run not found:'.
        """
        fake_id = "definitely-not-a-real-run-id-00000001"
        result = call_tool(mcp_server, "orchemist_logs", run_id=fake_id)
        assert "Run not found:" in result, (
            f"Expected 'Run not found:' for unknown run_id (no phase), got: {result!r}"
        )

    def test_logs_nonexistent_run_id_with_phase(self, mcp_server):
        """
        BEHAVIORAL CONTRACT:
        Given a run_id that does not exist (with phase), orchemist_logs still
        returns a string containing 'Run not found:' — not 'Phase not found:'.
        """
        fake_id = "definitely-not-a-real-run-id-00000002"
        result = call_tool(mcp_server, "orchemist_logs", run_id=fake_id, phase="hello")
        assert "Run not found:" in result, (
            f"Expected 'Run not found:' for unknown run_id with phase, got: {result!r}"
        )

    def test_logs_nonexistent_phase_valid_run_id(
        self, mcp_server, cleanup_orphan_processes
    ):
        """
        BEHAVIORAL CONTRACT:
        Given a valid run_id and phase='nonexistent-phase', orchemist_logs
        returns a string containing 'Phase not found:'.
        """
        # Launch to get a valid run_id
        launch_result = call_tool(
            mcp_server,
            "orchemist_launch",
            template_id="hello-pipeline",
            mode="dry-run",
        )
        run_id = json.loads(launch_result)["run_id"]
        cleanup_orphan_processes.append(run_id)

        result = call_tool(
            mcp_server,
            "orchemist_logs",
            run_id=run_id,
            phase="nonexistent-phase",
        )
        assert "Phase not found:" in result, (
            f"Expected 'Phase not found:' for nonexistent phase, got: {result!r}"
        )
        assert run_id in result, (
            f"Phase not found message must include run_id, got: {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Cleanup: verify the cleanup fixture works correctly
# ---------------------------------------------------------------------------


class TestE2ECleanup:
    """Verify the cleanup fixture collects and terminates daemon processes."""

    def test_cleanup_fixture_collects_run_ids(
        self, mcp_server, cleanup_orphan_processes
    ):
        """
        BEHAVIORAL CONTRACT:
        Given the E2E test suite completes, no daemon processes spawned by
        the test remain running after teardown. This test verifies the fixture
        collects run_ids correctly so teardown can kill daemons.
        """
        launch_result = call_tool(
            mcp_server,
            "orchemist_launch",
            template_id="hello-pipeline",
            mode="dry-run",
        )
        data = json.loads(launch_result)
        run_id = data["run_id"]
        cleanup_orphan_processes.append(run_id)

        # Verify the run exists in the DB (daemon was spawned)
        status_result = call_tool(mcp_server, "orchemist_status", run_id=run_id)
        status_data = json.loads(status_result)

        assert status_data["run_id"] == run_id, (
            "Launched run_id must be tracked and retrievable"
        )
        # The cleanup fixture will SIGTERM this process at module teardown
