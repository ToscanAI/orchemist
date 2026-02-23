"""Fallback retry logic for rate-limited or timed-out phases."""
from typing import Optional

from .executor import ExecutorResult, TaskState
from .openai_executor import OpenAICompatibleExecutor

#: Error codes that are safe to retry via the fallback executor.
RETRIABLE_ERRORS = {"rate_limit", "timeout", "overloaded"}


class FallbackHandler:
    """Wraps a primary executor with optional fallback on retriable errors.

    When the primary executor fails with a retriable error code (rate_limit,
    timeout, overloaded) **and** a fallback is configured, the task is
    re-executed via an :class:`OpenAICompatibleExecutor`.  All other failures
    (and all successes) are returned as-is.

    Args:
        primary_executor: Any executor with an ``execute(task, worker_id, **kwargs)``
                          method that returns a :class:`~executor.ExecutorResult`.
        fallback_config:  Optional dict with keys accepted by
                          :class:`OpenAICompatibleExecutor`'s constructor:
                          ``base_url``, ``model``, ``api_key``,
                          ``timeout_seconds``.  Pass ``None`` (default) to
                          disable fallback entirely.

    Example::

        handler = FallbackHandler(
            primary_executor=my_primary,
            fallback_config={
                "base_url": "http://localhost:8765/v1",
                "model": "gemini-3-pro-preview",
                "api_key": "secret",
            },
        )
        result = handler.execute("Write a haiku about cloud APIs.")
    """

    def __init__(
        self,
        primary_executor,
        fallback_config: Optional[dict] = None,
    ):
        self.primary = primary_executor
        self.fallback: Optional[OpenAICompatibleExecutor] = None

        if fallback_config:
            self.fallback = OpenAICompatibleExecutor(
                base_url=fallback_config.get("base_url", "http://localhost:8765/v1"),
                model=fallback_config.get("model", "gemini-3-pro-preview"),
                api_key=fallback_config.get("api_key", "dummy"),
                timeout_seconds=fallback_config.get("timeout_seconds", 300),
            )

    def execute(
        self, task: str, worker_id: str = "primary", **kwargs
    ) -> ExecutorResult:
        """Execute *task* via the primary executor, falling back on retriable errors.

        Args:
            task:      The prompt / task string.
            worker_id: Identifier propagated to whichever executor runs the task.
            **kwargs:  Forwarded to the primary executor's ``execute`` call.

        Returns:
            :class:`~executor.ExecutorResult` from either the primary or fallback executor.
        """
        result = self.primary.execute(task, worker_id=worker_id, **kwargs)

        if result.state == TaskState.FAILED and self.fallback:
            error_code = getattr(result, "error_code", "")
            if error_code in RETRIABLE_ERRORS:
                return self.fallback.execute(
                    task,
                    worker_id=f"{worker_id}-fallback",
                    **kwargs,
                )

        return result
