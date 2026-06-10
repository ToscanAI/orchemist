"""Lightweight runner adapter for synchronous pipeline execution.

PipelineRunner is a minimal struct that satisfies the interface contract
required by PhaseSequencer (runner.queue + runner.executors) without
starting background threads or requiring a persistent database.

Used exclusively by the `orch run` CLI command.
"""

import logging
import tempfile
from pathlib import Path
from typing import Any, List, Optional

from .db import Database
from .queue import TaskQueue
from .runner import TaskExecutor  # ABC only — no heavy imports

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Minimal runner adapter for PhaseSequencer.

    Does NOT start threads, does NOT use WorkerPool, RecoveryManager, or
    ProgressTracker. Just a queue + executor list, which is all PhaseSequencer
    needs.

    Args:
        executors: Ordered list of executors. PhaseSequencer picks the first
                   one where can_handle(task_type) returns True.
        db_path:   SQLite database path. Pass ":memory:" for ephemeral runs
                   (no disk footprint). Defaults to a tempfile that is deleted
                   after the context manager exits.

    Usage (as context manager — recommended):
        with PipelineRunner.standalone(api_key="sk-ant-...") as runner:
            seq = PhaseSequencer(template, runner)
            result = seq.execute(initial_input)

    Usage (manual):
        runner = PipelineRunner.standalone(api_key="sk-ant-...")
        try:
            seq = PhaseSequencer(template, runner)
            result = seq.execute(initial_input)
        finally:
            runner.close()
    """

    def __init__(
        self,
        executors: List[TaskExecutor],
        db_path: str = ":memory:",
    ) -> None:
        self._db_path = db_path
        self._tmp_dir = None  # set if we create a temp dir

        # If db_path is sentinel "temp", create a real temp file
        if db_path == "temp":
            self._tmp_dir = tempfile.TemporaryDirectory(prefix="orch-run-")
            db_file = Path(self._tmp_dir.name) / "pipeline.db"
            self._db_path = str(db_file)

        self._db = Database(self._db_path)
        self.queue: TaskQueue = TaskQueue(self._db)
        self.executors: List[TaskExecutor] = executors

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------

    @classmethod
    def standalone(
        cls,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        db_path: str = ":memory:",
        executor_type: Optional[str] = None,
    ) -> "PipelineRunner":
        """Create a PipelineRunner using AnthropicExecutor (direct API calls).

        Args:
            api_key:        Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            max_tokens:     Maximum output tokens per API call.
            db_path:        SQLite path (":memory:" for no-disk, "temp" for temp file).
            executor_type:  Forwarded from --executor CLI flag. Stored for future use
                            when ClaudeCodeExecutor is wired (see Issue #635 parent epic).

        Raises:
            ValueError: If no API key is found anywhere.
        """
        import os

        from .executors.anthropic_executor import AnthropicExecutor

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key required for standalone mode.\n"
                "  Option 1: orch run --api-key sk-ant-...\n"
                "  Option 2: export ANTHROPIC_API_KEY=sk-ant-..."
            )

        executor = AnthropicExecutor(api_key=resolved_key, max_tokens=max_tokens)
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def from_template(
        cls,
        template,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner pre-configured from a :class:`~templates.PipelineTemplate`.

        Args:
            template:   Loaded :class:`~templates.PipelineTemplate` instance.
            api_key:    Anthropic API key (or ``ANTHROPIC_API_KEY`` env var).
            max_tokens: Maximum output tokens per API call.
            db_path:    SQLite path.

        Returns:
            :class:`PipelineRunner` configured with an AnthropicExecutor.
        """
        return cls.standalone(
            api_key=api_key,
            max_tokens=max_tokens,
            db_path=db_path,
        )

    @classmethod
    def openclaw(
        cls,
        gateway_url: Optional[str] = None,
        gateway_token: Optional[str] = None,
        timeout_seconds: int = 600,
        dry_run: bool = False,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using OpenClawExecutor (sub-agent spawning).

        Args:
            gateway_url:       OpenClaw gateway URL (default http://localhost:4444,
                               or ``OPENCLAW_GATEWAY_URL`` env var).
            gateway_token:     Optional bearer token (or ``OPENCLAW_GATEWAY_TOKEN``
                               env var).
            timeout_seconds:   Max seconds per phase session (default 600).
            dry_run:           Skip real HTTP calls and return mock output.
            db_path:           SQLite path.
        """
        from .openclaw_executor import OpenClawExecutor

        executor = OpenClawExecutor(
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def openrouter(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_map: Optional[dict] = None,
        timeout_seconds: int = 300,
        max_tokens: int = 16384,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using OpenRouterExecutor (multi-provider routing).

        Args:
            api_key:          OpenRouter API key (or ``OPENROUTER_API_KEY`` env var).
            base_url:         API base URL (for proxies or self-hosted routers).
            model_map:        Custom model tier → model ID overrides.
            timeout_seconds:  HTTP request timeout per call (default 300s).
            max_tokens:       Maximum output tokens per request.
            db_path:          SQLite path.

        Raises:
            ValueError: If no API key is found anywhere.
        """
        import os

        from .executors.openrouter_executor import OpenRouterExecutor

        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "OpenRouter API key required.\n"
                "  Option 1: orch run --api-key sk-or-...\n"
                "  Option 2: export OPENROUTER_API_KEY=sk-or-..."
            )

        executor = OpenRouterExecutor(
            api_key=resolved_key,
            base_url=base_url,
            model_map=model_map,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def dry_run(
        cls,
        delay_seconds: float = 0.0,
        failure_rate: float = 0.0,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using DryRunExecutor (testing/CI).

        Args:
            delay_seconds: Simulated execution delay per phase.
            failure_rate:  Probability [0.0-1.0] of simulated phase failure.
            db_path:       SQLite path.
        """
        from .runner import DryRunExecutor

        executor = DryRunExecutor(
            delay_seconds=delay_seconds,
            failure_rate=failure_rate,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def claudecode(
        cls,
        mcp_server: Any,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using ClaudeCodeExecutor (MCP session routing).

        Routes task execution through the active Claude Code MCP session using
        the sampling capability. No Anthropic API key required — uses the user's
        Claude Code subscription.

        Args:
            mcp_server:      A FastMCP server instance with an active session.
                             Must not be None and must expose get_context().
            db_path:         SQLite path (":memory:" for no-disk, "temp" for temp file).

        Raises:
            ValueError: If mcp_server is None or lacks get_context.
                        Propagated directly from ClaudeCodeExecutor.__init__.
        """
        from .executors.claudecode_executor import ClaudeCodeExecutor

        executor = ClaudeCodeExecutor(mcp_server=mcp_server)
        return cls(executors=[executor], db_path=db_path)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "PipelineRunner":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        """Release database connections and clean up temp files."""
        try:
            self._db.close()
        except Exception:
            pass
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None
