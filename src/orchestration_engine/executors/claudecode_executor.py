"""ClaudeCodeExecutor — routes task execution through the active Claude Code MCP session.

Uses the MCP sampling capability (context.session.create_message()) to ask the
Claude Code host to generate completions. No Anthropic API key required;
uses the user's Claude Code subscription.

Architecture note:
    This executor uses FastMCP's get_context() → context.session.create_message()
    pattern. The session is only accessible inside an active MCP tool handler
    (i.e., while Claude Code is processing an orchemist_launch call).
    Outside a tool handler, context.session raises AttributeError — we treat
    this as the "no active session" signal.
"""

import asyncio
import concurrent.futures
import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from ..runner import TaskExecutor
from ..schemas import TaskError, TaskResult, TaskSpec, TaskState, TaskType

logger = logging.getLogger(__name__)


# Sentinel exceptions — internal only, never escape the module boundary
class _NoSessionError(Exception):
    pass


class _RejectedError(Exception):
    pass


class _EmptyResponseError(Exception):
    pass


# stopReason values that indicate Claude Code rejection
_REJECTION_STOP_REASONS = frozenset({"error", "denied", "cancelled"})


class ClaudeCodeExecutor(TaskExecutor):
    """Executor that routes prompts through the active Claude Code MCP session.

    Implements the MCP sampling pattern: calls context.session.create_message()
    to ask the Claude Code host (the session initiator) to generate a completion.
    """

    def __init__(self, mcp_server: Any):
        """Initialize with a FastMCP server instance.

        Args:
            mcp_server: A FastMCP server instance. Must not be None and must
                        expose a get_context() method.

        Raises:
            ValueError: If mcp_server is None or lacks the required interface.
        """
        if mcp_server is None or not hasattr(mcp_server, "get_context"):
            raise ValueError("ClaudeCodeExecutor requires an active MCP server context")
        self._mcp_server = mcp_server

    def can_handle(self, task_type: TaskType) -> bool:
        """This executor handles all task types."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        """Subscription-based execution — no per-token cost."""
        return 0.0

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "claudecode-worker",
        model_tier: str = None,
        thinking_level: str = None,
    ) -> TaskResult:
        """Execute a task by routing it through the Claude Code MCP session.

        Args:
            task: Task specification. Prompt is taken from payload["prompt"],
                  or the entire payload is JSON-serialized if prompt is missing.
            worker_id: Identifier for this worker.
            model_tier: Unused — model selection is controlled by Claude Code.
            thinking_level: Unused — model settings controlled by Claude Code.

        Returns:
            TaskResult with state SUCCESS on success, FAILED on any error.
        """
        start_time = datetime.now()
        task_id = task.id if hasattr(task, "id") and task.id else str(uuid4())

        # Extract prompt — fall back to JSON-serialized payload
        prompt = task.payload.get("prompt", "")
        if not prompt:
            prompt = json.dumps(task.payload, indent=2)

        # Run async sampling through the sync bridge
        try:
            result_text = self._run_sampling(prompt)
        except _NoSessionError:
            return self._make_failed(
                task_id, task, start_time,
                "No active MCP session context"
            )
        except _RejectedError:
            return self._make_failed(
                task_id, task, start_time,
                "Claude Code rejected the sampling request"
            )
        except _EmptyResponseError:
            return self._make_failed(
                task_id, task, start_time,
                "Empty response from Claude Code session"
            )
        except Exception as exc:
            return self._make_failed(
                task_id, task, start_time,
                f"MCP session error: {exc}"
            )

        elapsed = (datetime.now() - start_time).total_seconds()
        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"output": result_text},
            errors=[],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used="claude-code-session",
            execution_time_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_sampling(self, prompt: str) -> str:
        """Bridge: run the async MCP sampling call from a synchronous context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We are inside an async context — run in a fresh thread
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._sample(prompt))
                return future.result()
        else:
            return asyncio.run(self._sample(prompt))

    async def _sample(self, prompt: str) -> str:
        """Perform the async MCP sampling call."""
        from mcp.types import SamplingMessage, TextContent

        ctx = self._mcp_server.get_context()

        try:
            session = ctx.session
        except AttributeError:
            raise _NoSessionError()

        result = await session.create_message(
            messages=[
                SamplingMessage(
                    role="user",
                    content=TextContent(type="text", text=prompt),
                )
            ],
            max_tokens=4096,
        )

        stop_reason = getattr(result, "stopReason", None) or ""
        if stop_reason in _REJECTION_STOP_REASONS:
            raise _RejectedError()

        content = getattr(result, "content", None)
        if content is None:
            raise _EmptyResponseError()

        text = getattr(content, "text", None)
        if not text or not str(text).strip():
            raise _EmptyResponseError()

        return str(text)

    @staticmethod
    def _make_failed(
        task_id: str, task: TaskSpec, start_time: datetime, message: str
    ) -> TaskResult:
        """Build a FAILED TaskResult with a descriptive error."""
        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            errors=[
                TaskError(
                    code="executor_error",
                    message=message,
                    severity="error",
                )
            ],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used="claude-code-session",
            execution_time_seconds=(datetime.now() - start_time).total_seconds(),
        )
