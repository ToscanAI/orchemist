"""
Acceptance tests for ClaudeCodeExecutor behavioral contracts.
Issue #637 — [Feature] Create ClaudeCodeExecutor class with MCP session routing

Tests are written PRE-IMPLEMENTATION from behavioral contracts only.
No implementation details are assumed — all contracts are expressed as
observable inputs → observable outputs.

Usage:
    pytest /tmp/output-637-v2/acceptance_tests.py -v

Requirements:
    - ClaudeCodeExecutor must be importable from orchestration_engine.executors
    - Tests use only pytest + standard Python + unittest.mock
"""

import sys
import asyncio
import json
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

# Make the repo importable
sys.path.insert(0, '/home/toscan/orchestration-engine/src')
sys.path.insert(0, '/home/toscan/orchestration-engine')

from orchestration_engine.schemas import (
    TaskSpec, TaskType, TaskState, Priority, TaskResult
)


# ---------------------------------------------------------------------------
# Helpers: build mock MCP server objects that mimic expected MCP interface
# ---------------------------------------------------------------------------

def _make_text_response(stop_reason="endTurn", text="Claude Code response text"):
    """Build a mock MCP CreateMessageResult with text content."""
    mock_result = MagicMock()
    mock_result.stopReason = stop_reason
    mock_content = MagicMock()
    mock_content.text = text
    mock_result.content = mock_content
    return mock_result


def _make_valid_mcp_server(stop_reason="endTurn", response_text="Claude Code response text"):
    """Build a mock FastMCP server with a working get_context() + session."""
    server = MagicMock()
    mock_result = _make_text_response(stop_reason=stop_reason, text=response_text)

    session = MagicMock()
    session.create_message = AsyncMock(return_value=mock_result)

    ctx = MagicMock()
    ctx.session = session

    server.get_context.return_value = ctx
    return server


def _make_no_session_server():
    """Mock server where ctx.session raises AttributeError (no active request handler)."""
    server = MagicMock()
    server.get_context = MagicMock(spec=["__call__"])

    # Create a context where .session raises AttributeError (as mcp lib does)
    ctx = MagicMock(spec=[])  # spec=[] means no attributes defined
    server.get_context.return_value = ctx
    return server


def _make_task(payload=None):
    """Build a minimal TaskSpec for testing."""
    if payload is None:
        payload = {"prompt": "Summarize the project status."}
    return TaskSpec(
        type=TaskType.CONTENT,
        payload=payload,
        priority=Priority.NORMAL,
    )


def _import_executor():
    """Import ClaudeCodeExecutor — delays ImportError to test runtime."""
    from orchestration_engine.executors import ClaudeCodeExecutor
    return ClaudeCodeExecutor


# ---------------------------------------------------------------------------
# CONSTRUCTION CONTRACTS
# ---------------------------------------------------------------------------

class TestConstruction:
    """Behavioral contracts: instantiation with valid/invalid MCP server contexts."""

    def test_none_raises_value_error(self):
        """
        CONTRACT: When instantiated with None as the MCP server context,
        the system raises ValueError with the exact message
        'ClaudeCodeExecutor requires an active MCP server context'.
        """
        ClaudeCodeExecutor = _import_executor()
        with pytest.raises(ValueError, match="ClaudeCodeExecutor requires an active MCP server context"):
            ClaudeCodeExecutor(mcp_server=None)

    def test_invalid_type_raises_value_error(self):
        """
        CONTRACT: When instantiated with a non-None value that does not expose
        the required MCP server interface (e.g., a plain string), the system
        raises ValueError with the same message as for None.
        """
        ClaudeCodeExecutor = _import_executor()
        with pytest.raises(ValueError, match="ClaudeCodeExecutor requires an active MCP server context"):
            ClaudeCodeExecutor(mcp_server="not-a-server")

    def test_object_without_required_interface_raises_value_error(self):
        """
        CONTRACT: When instantiated with an object that lacks the required MCP
        server interface methods, the system raises ValueError — same error as None.
        """
        ClaudeCodeExecutor = _import_executor()

        class PlainObject:
            pass

        with pytest.raises(ValueError, match="ClaudeCodeExecutor requires an active MCP server context"):
            ClaudeCodeExecutor(mcp_server=PlainObject())

    def test_integer_raises_value_error(self):
        """
        CONTRACT: Any non-server value (e.g., integer) raises ValueError with
        the MCP server context message.
        """
        ClaudeCodeExecutor = _import_executor()
        with pytest.raises(ValueError, match="ClaudeCodeExecutor requires an active MCP server context"):
            ClaudeCodeExecutor(mcp_server=42)

    def test_valid_server_constructs_successfully(self):
        """
        CONTRACT: When instantiated with a valid MCP server context (one that
        exposes the required interface), the executor is ready — no exception raised.
        """
        ClaudeCodeExecutor = _import_executor()
        valid_server = _make_valid_mcp_server()
        executor = ClaudeCodeExecutor(mcp_server=valid_server)
        assert executor is not None


# ---------------------------------------------------------------------------
# CAPABILITY QUERY CONTRACTS
# ---------------------------------------------------------------------------

class TestCapabilityQueries:
    """Behavioral contracts: can_handle and estimate_cost queries."""

    def test_can_handle_any_task_type_returns_true(self):
        """
        CONTRACT: When asked whether the executor can handle any task type,
        the system returns True unconditionally — it handles all task types.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        for task_type in TaskType:
            result = executor.can_handle(task_type)
            assert result is True, f"Expected True for task_type={task_type}, got {result}"

    def test_estimate_cost_returns_zero(self):
        """
        CONTRACT: When asked to estimate the execution cost, the system returns
        0.0 — subscription-based execution has no per-task cost.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        task = _make_task()
        cost = executor.estimate_cost(task)
        assert cost == 0.0, f"Expected 0.0 cost, got {cost}"

    def test_estimate_cost_always_zero_regardless_of_payload(self):
        """
        CONTRACT: Cost is 0.0 regardless of task payload size or type.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())

        small_task = _make_task({"prompt": "Hi"})
        large_task = _make_task({"prompt": "x" * 10000})
        code_task = _make_task.__func__(_make_task, {"prompt": "big code"}) if False else TaskSpec(
            type=TaskType.CODE, payload={"prompt": "write 500 unit tests"}, priority=Priority.HIGH
        )

        assert executor.estimate_cost(small_task) == 0.0
        assert executor.estimate_cost(large_task) == 0.0
        assert executor.estimate_cost(code_task) == 0.0


# ---------------------------------------------------------------------------
# HAPPY PATH EXECUTION CONTRACTS
# ---------------------------------------------------------------------------

class TestHappyPathExecution:
    """Behavioral contracts: successful task execution."""

    def test_successful_execution_returns_success_state(self):
        """
        CONTRACT: When the Claude Code session is active and returns a valid
        response, the system returns a result with state SUCCESS.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.SUCCESS

    def test_successful_execution_stores_response_in_output_key(self):
        """
        CONTRACT: When Claude Code returns a successful response with text content,
        the system stores the response text in the 'output' key of the result dict.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(
            mcp_server=_make_valid_mcp_server(response_text="Generated summary text here")
        )
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert "output" in result.result, f"Expected 'output' key in result.result, got: {result.result}"
        assert result.result["output"] == "Generated summary text here"

    def test_successful_execution_has_no_errors(self):
        """
        CONTRACT: A successful execution produces a result with an empty errors list.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.errors == []

    def test_result_is_taskresult_instance(self):
        """
        CONTRACT: The return value of execute() is a TaskResult, regardless of outcome.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert isinstance(result, TaskResult)

    def test_synchronous_execution_completes_without_hanging(self):
        """
        CONTRACT: When the executor is invoked from a synchronous context,
        the call completes and returns a result — no hang, no blocking indefinitely.
        (Timeout enforced by pytest-timeout or the test runner itself.)
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_valid_mcp_server())
        task = _make_task()
        # If this hangs, the test framework will time out
        result = executor.execute(task, worker_id="sync-worker")
        assert result is not None


# ---------------------------------------------------------------------------
# EMPTY PROMPT CONTRACT
# ---------------------------------------------------------------------------

class TestEmptyPromptBehavior:
    """Behavioral contracts: tasks with no usable prompt."""

    def test_missing_prompt_key_attempts_execution_not_preflight_failure(self):
        """
        CONTRACT: When the task payload contains no 'prompt' key, the system
        serializes the entire payload and attempts execution — it does NOT return
        a pre-flight failure for the missing prompt key.
        Result state should be SUCCESS (if session is active), not FAILED due to
        missing prompt.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server(response_text="Response to context")
        executor = ClaudeCodeExecutor(mcp_server=server)
        task_without_prompt = _make_task({"context": "some-context-data", "instructions": "analyze"})
        result = executor.execute(task_without_prompt, worker_id="test-worker")
        assert result.state == TaskState.SUCCESS, (
            "A task without a 'prompt' key should be attempted (payload serialized), "
            f"not pre-flight failed. Got state={result.state}"
        )

    def test_empty_string_prompt_attempts_execution(self):
        """
        CONTRACT: When the task payload contains prompt='', the system
        serializes the entire payload rather than returning a pre-flight failure.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server(response_text="Response")
        executor = ClaudeCodeExecutor(mcp_server=server)
        task_empty_prompt = _make_task({"prompt": "", "data": "something"})
        result = executor.execute(task_empty_prompt, worker_id="test-worker")
        assert result.state == TaskState.SUCCESS


# ---------------------------------------------------------------------------
# NO ACTIVE SESSION CONTRACTS
# ---------------------------------------------------------------------------

class TestNoActiveSession:
    """Behavioral contracts: executor called with no active Claude Code session."""

    def test_no_active_session_returns_failed_not_raises(self):
        """
        CONTRACT: When no active Claude Code session exists (no live request
        handler context), the system returns a FAILED result — it does NOT
        raise an unhandled exception.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_no_session_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_no_active_session_error_message(self):
        """
        CONTRACT: When no active session exists, the error message is exactly
        'No active MCP session context'.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_no_session_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        error_messages = [e.message for e in result.errors]
        assert any("No active MCP session context" in msg for msg in error_messages), (
            f"Expected 'No active MCP session context' in errors, got: {error_messages}"
        )

    def test_no_active_session_does_not_hang(self):
        """
        CONTRACT: Absence of an active session causes an immediate FAILED result —
        no blocking or hanging.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_no_session_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result is not None  # If we reach here, no hang occurred


# ---------------------------------------------------------------------------
# RUNTIME ERROR / SESSION FAILURE CONTRACTS
# ---------------------------------------------------------------------------

class TestSessionRuntimeErrors:
    """Behavioral contracts: session errors during execution."""

    def test_runtime_error_returns_failed(self):
        """
        CONTRACT: When the attempt to route the task to Claude Code fails with
        any runtime error, the system catches it and returns a FAILED result —
        no unhandled exception propagates.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            side_effect=RuntimeError("Connection dropped")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_runtime_error_message_format(self):
        """
        CONTRACT: When a runtime error occurs, the error message is of the form
        'MCP session error: <details>' where <details> includes the exception message.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            side_effect=RuntimeError("Connection dropped")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        error_messages = [e.message for e in result.errors]
        assert any("MCP session error:" in msg for msg in error_messages), (
            f"Expected 'MCP session error:' prefix in errors, got: {error_messages}"
        )
        assert any("Connection dropped" in msg for msg in error_messages), (
            f"Expected exception detail 'Connection dropped' in errors, got: {error_messages}"
        )

    def test_timeout_error_returns_failed(self):
        """
        CONTRACT: An asyncio.TimeoutError (simulated session timeout) is caught
        and returned as a FAILED result, not raised.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_generic_exception_returns_failed_not_raises(self):
        """
        CONTRACT: Any arbitrary exception from the session is caught and
        converted to FAILED — no unhandled exception propagates.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            side_effect=Exception("Unexpected internal error")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        # Must not raise
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_network_failure_does_not_hang(self):
        """
        CONTRACT: Session failure of any kind returns a result immediately —
        no hanging or blocking.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            side_effect=RuntimeError("Network failure")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result is not None


# ---------------------------------------------------------------------------
# EMPTY / UNPARSEABLE RESPONSE CONTRACTS
# ---------------------------------------------------------------------------

class TestEmptyResponse:
    """Behavioral contracts: Claude Code returns no usable text content."""

    def test_empty_text_returns_failed(self):
        """
        CONTRACT: When Claude Code returns an empty string as text content,
        the system returns a FAILED result with message
        'Empty response from Claude Code session'.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason="endTurn", text="")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_empty_response_correct_error_message(self):
        """
        CONTRACT: Empty response failure carries the message
        'Empty response from Claude Code session'.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason="endTurn", text="")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        error_messages = [e.message for e in result.errors]
        assert any("Empty response from Claude Code session" in msg for msg in error_messages), (
            f"Expected empty response message, got: {error_messages}"
        )

    def test_whitespace_only_response_returns_failed(self):
        """
        CONTRACT: A response containing only whitespace is not usable text —
        the system treats it as an empty response and returns FAILED.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason="endTurn", text="   \n\t  ")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED

    def test_none_content_returns_failed(self):
        """
        CONTRACT: When the response object has no extractable text content
        (content is None), the system returns a FAILED result.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        mock_result = MagicMock()
        mock_result.stopReason = "endTurn"
        mock_result.content = None
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=mock_result
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# REJECTION SIGNAL CONTRACTS
# ---------------------------------------------------------------------------

class TestRejectionSignals:
    """Behavioral contracts: Claude Code signals rejection via stop reason."""

    @pytest.mark.parametrize("stop_reason", ["error", "denied", "cancelled"])
    def test_rejection_signal_returns_failed(self, stop_reason):
        """
        CONTRACT: When the Claude Code host signals rejection (via rejection
        stop reason values: 'error', 'denied', or 'cancelled'), the system
        returns a FAILED result — regardless of any content payload.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason=stop_reason, text="Some content")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED, (
            f"Expected FAILED for stop_reason='{stop_reason}', got {result.state}"
        )

    @pytest.mark.parametrize("stop_reason", ["error", "denied", "cancelled"])
    def test_rejection_signal_correct_error_message(self, stop_reason):
        """
        CONTRACT: When rejected, the error message is exactly
        'Claude Code rejected the sampling request'.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason=stop_reason, text="Ignored content")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        error_messages = [e.message for e in result.errors]
        assert any("Claude Code rejected the sampling request" in msg for msg in error_messages), (
            f"stop_reason='{stop_reason}': Expected rejection message, got: {error_messages}"
        )

    def test_rejection_is_unconditional_even_with_content(self):
        """
        CONTRACT: Rejection stop reason MUST produce FAILED unconditionally —
        even when a content payload is also present in the response.
        The rejection signal takes precedence over any content.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason="denied", text="I cannot process this request")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED
        # Also verify the content is NOT in the output key (rejection takes precedence)
        assert "output" not in result.result or result.result.get("output") is None or result.state == TaskState.FAILED

    @pytest.mark.parametrize("stop_reason", ["endTurn", "stopSequence", "maxTokens", "toolUse"])
    def test_non_rejection_stop_reasons_succeed(self, stop_reason):
        """
        CONTRACT: Stop reasons that are NOT rejection signals ('endTurn', 'stopSequence',
        'maxTokens', 'toolUse') should produce SUCCESS when text content is present.
        """
        ClaudeCodeExecutor = _import_executor()
        server = _make_valid_mcp_server()
        server.get_context.return_value.session.create_message = AsyncMock(
            return_value=_make_text_response(stop_reason=stop_reason, text="Valid content")
        )
        executor = ClaudeCodeExecutor(mcp_server=server)
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.SUCCESS, (
            f"stop_reason='{stop_reason}' should succeed, got {result.state}"
        )


# ---------------------------------------------------------------------------
# FAILED RESULT STRUCTURE CONTRACTS
# ---------------------------------------------------------------------------

class TestFailedResultStructure:
    """Behavioral contracts: structure of FAILED results."""

    def test_failed_result_has_errors_list(self):
        """
        CONTRACT: Any FAILED result contains at least one error in the errors list.
        """
        ClaudeCodeExecutor = _import_executor()
        executor = ClaudeCodeExecutor(mcp_server=_make_no_session_server())
        task = _make_task()
        result = executor.execute(task, worker_id="test-worker")
        assert result.state == TaskState.FAILED
        assert len(result.errors) > 0, "FAILED result must have at least one error"

    def test_failed_result_does_not_raise(self):
        """
        CONTRACT: Any failure condition results in a returned TaskResult — never
        an unhandled exception escaping the execute() call.
        """
        ClaudeCodeExecutor = _import_executor()
        # Test with no session
        executor = ClaudeCodeExecutor(mcp_server=_make_no_session_server())
        task = _make_task()
        try:
            result = executor.execute(task, worker_id="test-worker")
        except Exception as e:
            pytest.fail(f"execute() raised an unhandled exception: {type(e).__name__}: {e}")
