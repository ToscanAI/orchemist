"""OpenAI-compatible executor for fallback models (Gemini via proxy, etc.)."""
import json
import time
import urllib.request
import urllib.error
from typing import Optional

from .executor import ExecutorResult, TaskState


class OpenAICompatibleExecutor:
    """Executor that talks to any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(
        self,
        base_url: str = "http://localhost:8765/v1",
        model: str = "gemini-3-pro-preview",
        api_key: str = "dummy",
        timeout_seconds: int = 300,
        dry_run: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run

    def __repr__(self) -> str:
        masked_key = f"{self.api_key[:4]}***" if len(self.api_key) >= 4 else "***"
        return (
            f"OpenAICompatibleExecutor("
            f"base_url={self.base_url!r}, "
            f"model={self.model!r}, "
            f"api_key={masked_key!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"dry_run={self.dry_run!r})"
        )

    def execute(self, task: str, worker_id: str = "fallback", **kwargs) -> ExecutorResult:
        """Execute a task against the OpenAI-compatible endpoint.

        Args:
            task:      The prompt / task string to send to the model.
            worker_id: Identifier for this worker (informational).
            **kwargs:  Accepted but ignored (for interface compatibility).

        Returns:
            ExecutorResult with SUCCESS state on success, FAILED on any error.
        """
        if self.dry_run:
            return ExecutorResult(
                state=TaskState.SUCCESS,
                output=f"[DRY RUN] Fallback: {task[:100]}...",
                worker_id=worker_id,
            )

        url = f"{self.base_url}/chat/completions"
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": task}],
                "temperature": 0.7,
            }
        ).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )

        try:
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode())

            elapsed = time.monotonic() - start
            output = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            if not output:
                return ExecutorResult(
                    state=TaskState.FAILED,
                    output="Empty response from fallback",
                    worker_id=worker_id,
                    error_code="empty_response",
                )

            return ExecutorResult(
                state=TaskState.SUCCESS,
                output=output,
                worker_id=worker_id,
                duration_seconds=elapsed,
            )

        except urllib.error.URLError as e:
            return ExecutorResult(
                state=TaskState.FAILED,
                output=f"Connection error: {e}",
                worker_id=worker_id,
                error_code="connection_error",
            )
        except TimeoutError:
            return ExecutorResult(
                state=TaskState.FAILED,
                output="Fallback executor timed out",
                worker_id=worker_id,
                error_code="timeout",
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            return ExecutorResult(
                state=TaskState.FAILED,
                output=f"Invalid response: {e}",
                worker_id=worker_id,
                error_code="invalid_response",
            )

    def can_handle(self, task_type: str) -> bool:
        """Return True — this executor accepts any task type."""
        return True

    def estimate_cost(self, task: str, **kwargs) -> float:
        """Return 0.0 — free via proxy."""
        return 0.0  # Free via proxy
