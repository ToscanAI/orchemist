"""Gemini CLI executor — minimal subprocess wrapper for the ``gemini`` CLI.

Shells out to the local ``gemini -p "<prompt>"`` command and captures stdout
as the response.  Designed as a no-frills bridge for the dialogue phase
prototype (Track B of the Orchemist pivot, Issue #677).  The wider engine
already has executors for OpenRouter / Anthropic / OpenClaw — this module
exists solely to give the dialogue runner a second provider so the wedge
("two different model providers across a phase boundary") is real, not
simulated.

Out of scope (deliberately)
---------------------------
* No auth-refresh logic — Gemini CLI handles its own credential cache.
* No streaming — the dialogue phase needs the full response anyway.
* No tool-calling — reviewer rounds are pure text-in / text-out.
* No retry / backoff — a single shot per call.  Higher-level retry should
  be the dialogue loop's responsibility (or a future PR).

Public API
----------
* :class:`GeminiCliExecutor` — concrete :class:`TaskExecutor` implementation.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..schemas import TaskError, TaskResult, TaskSpec, TaskState, TaskType
from ..timestamps import now_utc
from ._common import BaseExecutor

logger = logging.getLogger(__name__)

__all__ = ["GeminiCliExecutor"]

# Default timeout — deep-think mode can take 5+ min per #677
_DEFAULT_TIMEOUT_SECONDS: int = 600

# Default binary name; honour shutil.which() so PATH overrides work.
_DEFAULT_BINARY: str = "gemini"


class GeminiCliExecutor(BaseExecutor):
    """Executor that delegates to the local ``gemini`` CLI.

    Implements the :class:`~.runner.TaskExecutor` ABC interface by subclassing
    :class:`~._common.BaseExecutor` (which subclasses ``TaskExecutor``). The old
    "duck-type to avoid an import cycle through ``runner.py``" workaround no
    longer applies: ``runner.py`` imports only ``schemas`` and nothing in it
    imports any executor module, so the chain
    ``gemini → _common → runner → schemas`` is acyclic (#927).

    Args:
        binary: Path or name of the gemini binary.  Defaults to ``gemini``;
            resolved against ``$PATH`` via :func:`shutil.which`.
        default_model: Optional model id to use when the task payload does
            not specify one (e.g. ``gemini-3.1-pro-preview``).
        default_timeout_seconds: Subprocess timeout in seconds.
        extra_args: Extra CLI flags appended to every invocation (e.g.
            ``["--non-interactive"]``).  Useful in CI.
    """

    def __init__(
        self,
        binary: str = _DEFAULT_BINARY,
        default_model: Optional[str] = None,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        self.binary = binary
        self.default_model = default_model
        self.default_timeout_seconds = max(1, int(default_timeout_seconds))
        self.extra_args: List[str] = list(extra_args or [])

    # ── TaskExecutor ABC ──────────────────────────────────────────────

    def can_handle(self, task_type: TaskType) -> bool:
        """The Gemini CLI is text-in/text-out — usable for any task type."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        """Rough cost estimate.  Gemini 3.1 Pro is ~ $1.25/$5 per Mtok."""
        return 0.02

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "gemini-cli",
        model_tier: Optional[str] = None,
        thinking_level: Optional[str] = None,  # noqa: ARG002 — accepted for ABC compat
    ) -> TaskResult:
        """Run a single ``gemini -p`` invocation and return a :class:`TaskResult`.

        Args:
            task: Task spec; ``task.payload["prompt"]`` is sent to gemini.
                If ``task.payload["model"]`` is set, it overrides
                ``default_model`` for this call.
            worker_id: Identifier for this worker (logged + recorded in metadata).
            model_tier: Ignored — Gemini CLI does not use tier names.  The
                concrete model is taken from ``payload["model"]`` →
                ``default_model``.  Accepted for ABC compatibility.
            thinking_level: Ignored — accepted for ABC compatibility.

        Returns:
            :class:`TaskResult` with ``result["output"]`` containing stdout.
            Non-zero exit code → ``state=FAILED`` with a :class:`TaskError`.
        """
        # #927 fix: capture a datetime at execute() ENTRY so TaskResult.started_at
        # reflects start time, not subprocess-completion time. elapsed/duration math
        # below derives from this same datetime via (datetime.now() - start_time).
        start_time = self._capture_start_time()
        payload: Dict[str, Any] = task.payload or {}
        prompt = str(payload.get("prompt", "") or "")
        if not prompt:
            return self._failed_result(
                task=task,
                worker_id=worker_id,
                start_time=start_time,
                code="empty_prompt",
                message="GeminiCliExecutor: task.payload['prompt'] is empty",
            )

        model = payload.get("model") or self.default_model
        timeout = self._resolve_timeout(task, payload)

        cmd = self._build_command(prompt=prompt, model=model)
        binary_resolved = shutil.which(cmd[0]) or cmd[0]
        logger.info(
            "GeminiCliExecutor: invoking %s (model=%s, timeout=%ds, prompt_len=%d)",
            binary_resolved, model or "<default>", timeout, len(prompt),
        )

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # #927: start_time is now a datetime, so compute elapsed via datetime
            # arithmetic (was `time.time() - start_time`, a float-minus-datetime
            # TypeError after the fix). elapsed is unused here but the subtraction
            # still executes before the raise below.
            elapsed = (now_utc() - start_time).total_seconds()
            stdout_partial = (exc.stdout or "") if isinstance(exc.stdout, str) else (
                exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            )
            stderr_partial = (exc.stderr or "") if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            )
            logger.warning(
                "GeminiCliExecutor: timed out after %ds (stderr=%r)",
                timeout, (stderr_partial[:200] if stderr_partial else ""),
            )
            raise TimeoutError(
                f"gemini CLI exceeded {timeout}s timeout; "
                f"stderr={stderr_partial[:200] or '(empty)'}"
            ) from exc
        except FileNotFoundError as exc:
            return self._failed_result(
                task=task,
                worker_id=worker_id,
                start_time=start_time,
                code="binary_not_found",
                message=f"gemini binary not found on PATH: {exc}",
            )
        except OSError as exc:
            return self._failed_result(
                task=task,
                worker_id=worker_id,
                start_time=start_time,
                code="subprocess_error",
                message=f"failed to launch gemini CLI: {exc}",
            )

        elapsed = (now_utc() - start_time).total_seconds()  # #927: datetime arithmetic
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode

        if exit_code != 0:
            logger.warning(
                "GeminiCliExecutor: exit=%d stderr=%r",
                exit_code, stderr[:500],
            )
            raise GeminiCliError(
                f"gemini CLI exited {exit_code}: {stderr.strip()[:500] or '(empty stderr)'}"
            )

        if stderr.strip():
            logger.debug("GeminiCliExecutor stderr: %s", stderr[:500])

        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"output": stdout, "stderr": stderr[:2000]},
            model_used=model or "gemini",
            execution_time_seconds=elapsed,
            tokens_consumed=0,  # Gemini CLI does not report token counts
            cost_usd=Decimal("0"),
            started_at=start_time,  # #927: entry datetime, not subprocess-exit time
            completed_at=now_utc(),
            metadata={
                "worker_id": worker_id,
                "exit_code": exit_code,
                "stdout_chars": len(stdout),
                "stderr_chars": len(stderr),
                "binary": binary_resolved,
            },
        )

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_command(self, prompt: str, model: Optional[str]) -> List[str]:
        """Assemble the argv list for the gemini CLI."""
        cmd: List[str] = [self.binary, "-p", prompt]
        if model:
            cmd.extend(["-m", model])
        if self.extra_args:
            cmd.extend(self.extra_args)
        return cmd

    def _resolve_timeout(self, task: TaskSpec, payload: Dict[str, Any]) -> int:
        """Pick the timeout to use for this call.

        Priority:
        1. ``payload["timeout_seconds"]`` (per-call override)
        2. ``task.timeout_seconds`` (TaskSpec field) — but only when it's the
           non-default value (most callers leave it at 3600).
        3. ``self.default_timeout_seconds``
        """
        raw = payload.get("timeout_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            return int(raw)
        # We deliberately don't honour task.timeout_seconds=3600 because that's
        # the schema default; only honour it when caller explicitly shrunk it.
        ts = getattr(task, "timeout_seconds", None)
        if isinstance(ts, int) and 0 < ts < self.default_timeout_seconds:
            return ts
        return self.default_timeout_seconds

    def _failed_result(
        self,
        task: TaskSpec,
        worker_id: str,
        start_time: datetime,
        code: str,
        message: str,
    ) -> TaskResult:
        """Construct a uniform FAILED TaskResult for non-exceptional failures.

        ``start_time`` is the entry datetime captured by ``execute()`` (#927):
        ``started_at`` is set to it, and ``elapsed`` is derived via datetime
        arithmetic (was ``time.time() - float_start``).
        """
        elapsed = (now_utc() - start_time).total_seconds()
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            errors=[TaskError(code=code, message=message, severity="error")],
            started_at=start_time,  # #927: entry datetime, not construction time
            completed_at=now_utc(),
            execution_time_seconds=elapsed,
            metadata={"worker_id": worker_id},
        )


class GeminiCliError(RuntimeError):
    """Raised when the gemini CLI exits non-zero.

    The dialogue runner catches generic ``Exception`` and converts this into
    a per-round ``error`` field, so a more specific class is mainly useful
    for tests that want to assert on type.
    """
