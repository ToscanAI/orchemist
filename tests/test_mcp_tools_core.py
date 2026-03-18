"""
Acceptance tests for MCP-2: Core Tools — Launch, Status, Logs

These tests are written BEFORE implementation from behavioral contracts only.
They express what the system SHOULD DO, not how it does it.

Import path: /home/toscan/orchestration-engine
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from datetime import datetime




import pytest


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _get_registered_tools():
    """
    Build a fresh FastMCP instance, register tools on it, then return a
    dict mapping tool name → callable for easy invocation in tests.
    
    Assumption: register_tools(mcp) is the registration entrypoint in
    orchestration_engine.mcp.tools (per spec Section B guidance).
    """
    from mcp.server.fastmcp import FastMCP
    from orchestration_engine.mcp.tools import register_tools

    mcp = FastMCP(name="test-orchemist")
    register_tools(mcp)

    tools_list = asyncio.run(mcp.list_tools())
    # Extract the underlying callables by name from the mcp._tool_manager or similar;
    # since FastMCP exposes list_tools(), we use direct call via mcp.call_tool()
    return mcp


@pytest.fixture(scope="module")
def mcp_server():
    """Module-scoped MCP instance with tools registered."""
    from mcp.server.fastmcp import FastMCP
    from orchestration_engine.mcp.tools import register_tools
    mcp = FastMCP(name="test-orchemist")
    register_tools(mcp)
    return mcp


def call_tool(mcp_server, tool_name, **kwargs):
    """Helper to call an MCP tool by name and return its string result."""
    result = asyncio.run(mcp_server.call_tool(tool_name, kwargs))
    # FastMCP returns a list of content items; extract text
    if isinstance(result, list) and result:
        item = result[0]
        if hasattr(item, 'text'):
            return item.text
        return str(item)
    return str(result)


# ---------------------------------------------------------------------------
# orchemist_launch — behavioral contracts
# ---------------------------------------------------------------------------

class TestOrchemistLaunchBehavior:

    @patch('orchestration_engine.mcp.tools.subprocess.Popen')
    @patch('orchestration_engine.mcp.tools.Database')
    def test_launch_valid_template_returns_run_id_and_status_running(
        self, mock_db_class, mock_popen, mcp_server
    ):
        """
        Behavioral contract: When called with a valid template_id and optional mode,
        the system creates a new pipeline run and returns a JSON response containing
        both a run_id string and status: "running".
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        with patch('orchestration_engine.mcp.tools.TemplateEngine') as mock_te_class:
            mock_te = MagicMock()
            mock_te_class.return_value = mock_te
            mock_template = MagicMock()
            mock_template.id = "coding-pipeline-v1"
            mock_template_file = MagicMock()
            mock_te.resolve_template.return_value = mock_template_file
            mock_te.load_template.return_value = mock_template

            with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
                mock_path_instance = MagicMock()
                mock_path_class.return_value = mock_path_instance
                mock_path_class.home.return_value = Path("/tmp/fake-home")

                result = call_tool(mcp_server, "orchemist_launch", template_id="coding-pipeline-v1")

        data = json.loads(result)
        assert "run_id" in data, "Response must contain run_id"
        assert isinstance(data["run_id"], str), "run_id must be a string"
        assert data["status"] == "running", "status must be 'running'"

    @patch('orchestration_engine.mcp.tools.subprocess.Popen')
    @patch('orchestration_engine.mcp.tools.Database')
    def test_launch_with_inputs_returns_run_id_and_status_running(
        self, mock_db_class, mock_popen, mcp_server
    ):
        """
        Behavioral contract: When called with a valid template_id, optional mode,
        and an inputs object, the system passes those inputs to the pipeline and
        returns a JSON response containing a run_id string and status: "running".
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        with patch('orchestration_engine.mcp.tools.TemplateEngine') as mock_te_class:
            mock_te = MagicMock()
            mock_te_class.return_value = mock_te
            mock_template = MagicMock()
            mock_template.id = "coding-pipeline-v1"
            mock_template_file = MagicMock()
            mock_te.resolve_template.return_value = mock_template_file
            mock_te.load_template.return_value = mock_template

            with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
                mock_path_class.return_value = MagicMock()
                mock_path_class.home.return_value = Path("/tmp/fake-home")

                result = call_tool(
                    mcp_server,
                    "orchemist_launch",
                    template_id="coding-pipeline-v1",
                    inputs={"issue_url": "https://github.com/org/repo/issues/1"},
                )

        data = json.loads(result)
        assert "run_id" in data, "Response must contain run_id even when inputs are supplied"
        assert isinstance(data["run_id"], str), "run_id must be a string"
        assert data["status"] == "running", "status must be 'running' even when inputs are supplied"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_launch_unknown_template_returns_template_not_found_error(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called with a template_id that does not correspond
        to any known template, the system returns an error string:
        "Template not found: <template_id>".
        """
        with patch('orchestration_engine.mcp.tools.TemplateEngine') as mock_te_class:
            mock_te = MagicMock()
            mock_te_class.return_value = mock_te
            # Simulate template resolution failure
            from orchestration_engine.templates import TemplateNotFoundError
            mock_te.resolve_template.side_effect = TemplateNotFoundError("no-such-template")

            result = call_tool(mcp_server, "orchemist_launch", template_id="no-such-template")

        assert result == "Template not found: no-such-template", (
            f"Expected 'Template not found: no-such-template', got: {result!r}"
        )

    def test_launch_empty_template_id_returns_missing_parameter_error(self, mcp_server):
        """
        Behavioral contract: When called without a template_id (or with an empty
        string), the system returns an error string:
        "Missing required parameter: template_id".
        """
        result = call_tool(mcp_server, "orchemist_launch", template_id="")
        assert result == "Missing required parameter: template_id", (
            f"Expected missing-parameter error, got: {result!r}"
        )

    def test_launch_invalid_mode_returns_mode_error(self, mcp_server):
        """
        Behavioral contract: When called with a mode value that is not one of
        'dry-run', 'standalone', or 'openclaw', the system returns:
        "Invalid mode: <mode>. Supported modes: dry-run, standalone, openclaw".
        """
        result = call_tool(mcp_server, "orchemist_launch", template_id="some-template", mode="invalid-mode")
        assert result == "Invalid mode: invalid-mode. Supported modes: dry-run, standalone, openclaw", (
            f"Expected invalid-mode error, got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_launch_infrastructure_failure_returns_api_not_reachable(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When the underlying storage or process infrastructure
        is unreachable or raises an unexpected error, the system returns an error
        string 'Orchemist API not reachable' rather than raising an unhandled exception.
        """
        mock_db_class.side_effect = Exception("DB connection refused")

        with patch('orchestration_engine.mcp.tools.TemplateEngine') as mock_te_class:
            mock_te = MagicMock()
            mock_te_class.return_value = mock_te
            mock_template = MagicMock()
            mock_template.id = "coding-pipeline-v1"
            mock_te.resolve_template.return_value = MagicMock()
            mock_te.load_template.return_value = mock_template

            result = call_tool(mcp_server, "orchemist_launch", template_id="coding-pipeline-v1")

        assert result == "Orchemist API not reachable", (
            f"Expected 'Orchemist API not reachable', got: {result!r}"
        )


# ---------------------------------------------------------------------------
# orchemist_status — behavioral contracts
# ---------------------------------------------------------------------------

class TestOrchemistStatusBehavior:

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_valid_run_returns_all_required_fields(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called with a run_id that exists, the system
        returns a JSON response containing exactly: run_id (string), status (string),
        current_phase (string or null), completed_phases (array of strings),
        elapsed (float), and score (float or null).
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'abc123',
            'status': 'running',
            'current_phase': 'spec',
            'completed_phases': '["spec"]',
            'started_at': datetime(2026, 1, 1, 12, 0, 0),
            'completed_at': None,
            'scoring_score': None,
        }

        result = call_tool(mcp_server, "orchemist_status", run_id="abc123")
        data = json.loads(result)

        assert "run_id" in data
        assert "status" in data
        assert "current_phase" in data
        assert "completed_phases" in data
        assert "elapsed" in data
        assert "score" in data
        assert isinstance(data["run_id"], str)
        assert isinstance(data["status"], str)
        assert isinstance(data["completed_phases"], list)
        assert isinstance(data["elapsed"], float)

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_elapsed_is_float(self, mock_db_class, mcp_server):
        """
        Behavioral contract: The elapsed field in the status response is a float
        representing seconds since the run started as a floating-point number.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'abc123',
            'status': 'running',
            'current_phase': None,
            'completed_phases': '[]',
            'started_at': datetime(2026, 1, 1, 12, 0, 0),
            'completed_at': None,
            'scoring_score': None,
        }

        result = call_tool(mcp_server, "orchemist_status", run_id="abc123")
        data = json.loads(result)
        assert isinstance(data["elapsed"], float), "elapsed must be a float"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_running_pipeline_returns_partial_completed_phases(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called for a pipeline that is still running,
        the system returns status: "running" and a partial (possibly empty) list
        of completed_phases reflecting current progress.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'run-running',
            'status': 'running',
            'current_phase': 'build',
            'completed_phases': '["spec", "acceptance_test"]',
            'started_at': datetime(2026, 1, 1, 12, 0, 0),
            'completed_at': None,
            'scoring_score': None,
        }

        result = call_tool(mcp_server, "orchemist_status", run_id="run-running")
        data = json.loads(result)

        assert data["status"] == "running"
        assert isinstance(data["completed_phases"], list)
        # Phases that are done are listed; total is less than all phases
        assert data["completed_phases"] == ["spec", "acceptance_test"]

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_completed_with_scoring_returns_float_score(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called for a pipeline that completed with scoring
        enabled, the system returns a numeric float value in the score field.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'run-scored',
            'status': 'completed',
            'current_phase': None,
            'completed_phases': '["spec", "build", "review"]',
            'started_at': datetime(2026, 1, 1, 12, 0, 0),
            'completed_at': datetime(2026, 1, 1, 12, 30, 0),
            'scoring_score': 0.92,
        }

        result = call_tool(mcp_server, "orchemist_status", run_id="run-scored")
        data = json.loads(result)

        assert data["score"] is not None, "score must not be null for a scored run"
        assert isinstance(data["score"], float), "score must be a float"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_completed_without_scoring_returns_null_score(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called for a pipeline that completed without
        scoring, the system returns score: null.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'run-no-score',
            'status': 'completed',
            'current_phase': None,
            'completed_phases': '["spec", "build"]',
            'started_at': datetime(2026, 1, 1, 12, 0, 0),
            'completed_at': datetime(2026, 1, 1, 12, 10, 0),
            'scoring_score': None,
        }

        result = call_tool(mcp_server, "orchemist_status", run_id="run-no-score")
        data = json.loads(result)
        assert data["score"] is None, "score must be null for a run without scoring"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_nonexistent_run_id_returns_run_not_found(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called with a run_id that does not exist,
        the system returns the error string "Run not found: <run_id>".
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = None

        result = call_tool(mcp_server, "orchemist_status", run_id="ghost-run")
        assert result == "Run not found: ghost-run", (
            f"Expected 'Run not found: ghost-run', got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_empty_string_run_id_returns_run_not_found_empty(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called with an empty string as run_id, the system
        returns the error string "Run not found: " (with empty string appended).
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = None

        result = call_tool(mcp_server, "orchemist_status", run_id="")
        assert result == "Run not found: ", (
            f"Expected 'Run not found: ', got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_status_infrastructure_failure_returns_api_not_reachable(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When the underlying storage or process infrastructure
        is unreachable or raises an unexpected error, the system returns
        'Orchemist API not reachable' rather than raising an unhandled exception.
        """
        mock_db_class.side_effect = Exception("Connection refused")
        result = call_tool(mcp_server, "orchemist_status", run_id="any-run")
        assert result == "Orchemist API not reachable", (
            f"Expected 'Orchemist API not reachable', got: {result!r}"
        )


# ---------------------------------------------------------------------------
# orchemist_logs — behavioral contracts
# ---------------------------------------------------------------------------

class TestOrchemistLogsBehavior:

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_full_log_returned_when_no_phase_specified(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called with a valid run_id and no phase parameter,
        the system returns the full daemon log content for that pipeline run as plain text.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'log-run',
            'output_dir': '/tmp/fake-output-dir',
        }
        log_content = "2026-01-01 12:00:00 INFO Pipeline started\n2026-01-01 12:01:00 INFO Phase spec started"

        with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
            mock_output_dir = MagicMock()
            mock_log_file = MagicMock()
            mock_log_file.exists.return_value = True
            mock_log_file.read_text.return_value = log_content
            mock_output_dir.__truediv__ = lambda self, other: mock_log_file
            mock_path_class.return_value = mock_output_dir
            mock_path_class.home.return_value = Path("/tmp/fake-home")

            result = call_tool(mcp_server, "orchemist_logs", run_id="log-run")

        assert result == log_content, "Full log content must be returned when no phase filter given"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_returns_no_logs_available_when_log_file_missing(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called with a valid run_id and no phase parameter,
        if the log file does not yet exist (e.g., daemon not yet started), the system
        returns the string "(no logs available)" rather than an error.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'queued-run',
            'output_dir': '/tmp/fake-output-dir',
        }

        with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
            mock_output_dir = MagicMock()
            mock_log_file = MagicMock()
            mock_log_file.exists.return_value = False  # Log not written yet
            mock_output_dir.__truediv__ = lambda self, other: mock_log_file
            mock_path_class.return_value = mock_output_dir
            mock_path_class.home.return_value = Path("/tmp/fake-home")

            result = call_tool(mcp_server, "orchemist_logs", run_id="queued-run")

        assert result == "(no logs available)", (
            f"Expected '(no logs available)', got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_phase_filter_returns_only_phase_output(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called with a valid run_id and a phase parameter,
        the system returns only the text output for that specific phase.
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'log-run',
            'output_dir': '/tmp/fake-output-dir',
        }
        phase_content = "# Spec Phase Output\nThis is the spec."

        with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
            mock_output_dir = MagicMock()
            mock_phase_file = MagicMock()
            mock_phase_file.exists.return_value = True
            mock_phase_file.read_text.return_value = phase_content
            mock_output_dir.__truediv__ = lambda self, other: mock_phase_file
            mock_path_class.return_value = mock_output_dir
            mock_path_class.home.return_value = Path("/tmp/fake-home")

            result = call_tool(mcp_server, "orchemist_logs", run_id="log-run", phase="spec")

        assert result == phase_content, "Only the specified phase content must be returned"

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_nonexistent_run_id_returns_run_not_found(self, mock_db_class, mcp_server):
        """
        Behavioral contract: When called with a run_id that does not exist,
        the system returns the error string "Run not found: <run_id>".
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = None

        result = call_tool(mcp_server, "orchemist_logs", run_id="ghost-run")
        assert result == "Run not found: ghost-run", (
            f"Expected 'Run not found: ghost-run', got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_empty_string_run_id_returns_run_not_found_empty(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called with an empty string as run_id,
        the system returns the error string "Run not found: " (with empty string appended).
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = None

        result = call_tool(mcp_server, "orchemist_logs", run_id="")
        assert result == "Run not found: ", (
            f"Expected 'Run not found: ', got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_phase_not_found_returns_phase_not_found_error(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When called with a run_id that exists but the specified
        phase has no output file in the run's output directory (either never ran or
        invalid phase name), the system returns:
        "Phase not found: <phase> in run <run_id>".
        """
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.get_pipeline_run.return_value = {
            'run_id': 'existing-run',
            'output_dir': '/tmp/fake-output-dir',
        }

        with patch('orchestration_engine.mcp.tools.Path') as mock_path_class:
            mock_output_dir = MagicMock()
            mock_phase_file = MagicMock()
            mock_phase_file.exists.return_value = False  # Phase file missing
            mock_output_dir.__truediv__ = lambda self, other: mock_phase_file
            mock_path_class.return_value = mock_output_dir
            mock_path_class.home.return_value = Path("/tmp/fake-home")

            result = call_tool(
                mcp_server, "orchemist_logs", run_id="existing-run", phase="nonexistent-phase"
            )

        assert result == "Phase not found: nonexistent-phase in run existing-run", (
            f"Expected phase-not-found error, got: {result!r}"
        )

    @patch('orchestration_engine.mcp.tools.Database')
    def test_logs_infrastructure_failure_returns_api_not_reachable(
        self, mock_db_class, mcp_server
    ):
        """
        Behavioral contract: When the underlying storage or process infrastructure
        is unreachable or raises an unexpected error, the system returns
        'Orchemist API not reachable' rather than raising an unhandled exception.
        """
        mock_db_class.side_effect = Exception("Disk I/O error")
        result = call_tool(mcp_server, "orchemist_logs", run_id="any-run")
        assert result == "Orchemist API not reachable", (
            f"Expected 'Orchemist API not reachable', got: {result!r}"
        )


# ---------------------------------------------------------------------------
# Tool Registration — behavioral contracts
# ---------------------------------------------------------------------------

class TestToolRegistrationBehavior:

    def test_tools_list_includes_exactly_three_tools(self, mcp_server):
        """
        Behavioral contract: When an MCP client requests the list of available
        tools (tools/list), the server includes exactly three tools:
        orchemist_launch, orchemist_status, and orchemist_logs.
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        tool_names = {t.name for t in tools_list}
        assert tool_names == {"orchemist_launch", "orchemist_status", "orchemist_logs"}, (
            f"Expected exactly 3 tools, got: {tool_names}"
        )

    def test_orchemist_launch_has_correct_description(self, mcp_server):
        """
        Behavioral contract: The orchemist_launch tool is listed with description
        "Launch a pipeline run by template ID and return the run_id."
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        launch_tool = next((t for t in tools_list if t.name == "orchemist_launch"), None)
        assert launch_tool is not None, "orchemist_launch must be in tools list"
        assert launch_tool.description == "Launch a pipeline run by template ID and return the run_id.", (
            f"Got description: {launch_tool.description!r}"
        )

    def test_orchemist_status_has_correct_description(self, mcp_server):
        """
        Behavioral contract: The orchemist_status tool is listed with description
        "Get the status and progress of a pipeline run."
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        status_tool = next((t for t in tools_list if t.name == "orchemist_status"), None)
        assert status_tool is not None, "orchemist_status must be in tools list"
        assert status_tool.description == "Get the status and progress of a pipeline run.", (
            f"Got description: {status_tool.description!r}"
        )

    def test_orchemist_logs_has_correct_description(self, mcp_server):
        """
        Behavioral contract: The orchemist_logs tool is listed with description
        "Retrieve logs for a pipeline run, optionally filtered to a specific phase."
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        logs_tool = next((t for t in tools_list if t.name == "orchemist_logs"), None)
        assert logs_tool is not None, "orchemist_logs must be in tools list"
        assert logs_tool.description == "Retrieve logs for a pipeline run, optionally filtered to a specific phase.", (
            f"Got description: {logs_tool.description!r}"
        )

    def test_orchemist_launch_has_required_template_id_param(self, mcp_server):
        """
        Behavioral contract: The orchemist_launch tool is listed with one required
        parameter template_id of type string.
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        launch_tool = next((t for t in tools_list if t.name == "orchemist_launch"), None)
        assert launch_tool is not None

        schema = launch_tool.inputSchema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        assert "template_id" in properties, "template_id must be in schema properties"
        assert "template_id" in required, "template_id must be in required list"
        assert properties["template_id"].get("type") == "string", "template_id must be type string"

    def test_orchemist_launch_has_optional_mode_and_inputs_params(self, mcp_server):
        """
        Behavioral contract: The orchemist_launch tool is listed with two optional
        parameters: mode (string, default "dry-run") and inputs (object, optional).
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        launch_tool = next((t for t in tools_list if t.name == "orchemist_launch"), None)
        assert launch_tool is not None

        schema = launch_tool.inputSchema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        assert "mode" in properties, "mode must be in schema properties"
        assert "mode" not in required, "mode must NOT be required (it is optional)"
        assert "inputs" in properties, "inputs must be in schema properties"
        assert "inputs" not in required, "inputs must NOT be required (it is optional)"

    def test_orchemist_status_has_required_run_id_param(self, mcp_server):
        """
        Behavioral contract: The orchemist_status tool is listed with one required
        parameter run_id of type string.
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        status_tool = next((t for t in tools_list if t.name == "orchemist_status"), None)
        assert status_tool is not None

        schema = status_tool.inputSchema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        assert "run_id" in properties, "run_id must be in schema properties"
        assert "run_id" in required, "run_id must be in required list"
        assert properties["run_id"].get("type") == "string", "run_id must be type string"

    def test_orchemist_logs_has_required_run_id_and_optional_phase(self, mcp_server):
        """
        Behavioral contract: The orchemist_logs tool is listed with one required
        parameter run_id of type string and one optional parameter phase (string).
        """
        tools_list = asyncio.run(mcp_server.list_tools())
        logs_tool = next((t for t in tools_list if t.name == "orchemist_logs"), None)
        assert logs_tool is not None

        schema = logs_tool.inputSchema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        assert "run_id" in properties, "run_id must be in schema properties"
        assert "run_id" in required, "run_id must be in required list"
        assert "phase" in properties, "phase must be in schema properties"
        assert "phase" not in required, "phase must NOT be required (it is optional)"
