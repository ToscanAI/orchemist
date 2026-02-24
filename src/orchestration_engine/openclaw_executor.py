"""OpenClaw sub-agent executor — spawns real OpenClaw sessions per pipeline phase.

This executor communicates with the OpenClaw gateway HTTP API to spawn sub-agent
sessions, poll for completion, and extract output.  It uses only stdlib
(urllib.request) — no new pip dependencies required.

Usage:
    executor = OpenClawExecutor(
        gateway_url="http://localhost:4444",
        gateway_token="your-token",          # optional
    )
    # or set OPENCLAW_GATEWAY_URL / OPENCLAW_GATEWAY_TOKEN env vars

    result = executor.execute(task, worker_id="worker-1", model_tier="sonnet")
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from .runner import TaskExecutor
from .schemas import ModelTier, TaskError, TaskResult, TaskSpec, TaskState, TaskType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants (spec requirement)
# ---------------------------------------------------------------------------

MODEL_MAP: Dict[str, str] = {
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6",
    # ModelTier enum fallbacks
    ModelTier.HAIKU: "anthropic/claude-haiku-4-5-20251001",
    ModelTier.SONNET: "anthropic/claude-sonnet-4-6",
    ModelTier.OPUS: "anthropic/claude-opus-4-6",
}

THINKING_MAP: Dict[str, Optional[str]] = {
    "off": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# Default timeout for waiting on a spawned session to complete.
DEFAULT_TIMEOUT_SECONDS = 600

# How long to sleep between each poll of the session status endpoint.
POLL_INTERVAL_SECONDS = 3.0


class OpenClawExecutor(TaskExecutor):
    """Executor that spawns real OpenClaw sub-agent sessions per pipeline phase.

    The executor communicates with the OpenClaw gateway over HTTP:

    1. POST  ``{gateway_url}/api/sessions/spawn``   — starts the sub-agent
    2. GET   ``{gateway_url}/api/sessions/{key}``   — polls for completion
    3. Extracts ``output`` (or ``result``/``content``) from the final response.

    Args:
        gateway_url:       Base URL of the OpenClaw gateway daemon.
                           Defaults to ``OPENCLAW_GATEWAY_URL`` env var or
                           ``"http://localhost:4444"``.
        gateway_token:     Optional bearer token for gateway authentication.
                           Defaults to ``OPENCLAW_GATEWAY_TOKEN`` env var.
        timeout_seconds:   Maximum seconds to wait for a session to complete.
        dry_run:           When True, skip actual HTTP calls and return mock output.
    """

    def __init__(
        self,
        gateway_url: Optional[str] = None,
        gateway_token: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        dry_run: bool = False,
    ) -> None:
        self.gateway_url = (
            gateway_url
            if gateway_url is not None
            else os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:18789")
        ).rstrip("/")
        # Use explicit token if provided (even empty string overrides env var)
        self.gateway_token = (
            gateway_token
            if gateway_token is not None
            else os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
        )
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # TaskExecutor interface
    # ------------------------------------------------------------------

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: D102
        return True

    def estimate_cost(self, task: TaskSpec) -> float:  # noqa: D102
        # Rough cost estimate (same scale as AnthropicExecutor)
        tier = task.preferred_model or ModelTier.SONNET
        costs = {
            ModelTier.HAIKU: 0.002,
            ModelTier.SONNET: 0.02,
            ModelTier.OPUS: 0.10,
        }
        return costs.get(tier, 0.02)

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "openclaw-worker",
        model_tier: Optional[str] = None,
        thinking_level: Optional[str] = None,
    ) -> TaskResult:
        """Execute a task by spawning an OpenClaw sub-agent session.

        Args:
            task:           The task specification (prompt in ``payload["prompt"]``).
            worker_id:      Identifier for this worker (informational).
            model_tier:     Model tier override — haiku / sonnet / opus.
            thinking_level: Thinking budget override — off / low / medium / high.

        Returns:
            TaskResult with the session output or error details.
        """
        start_time = datetime.now()
        task_id = task.id if hasattr(task, "id") else str(uuid4())

        # ── 1. Resolve model / thinking ──────────────────────────────────────
        tier_key = model_tier or (
            task.preferred_model.value
            if hasattr(task.preferred_model, "value")
            else task.preferred_model
        ) or "sonnet"
        model = MODEL_MAP.get(tier_key, MODEL_MAP["sonnet"])

        thinking_key = (thinking_level or "off").lower()
        thinking = THINKING_MAP.get(thinking_key, None)

        # ── 2. Extract prompt ────────────────────────────────────────────────
        prompt = task.payload.get("prompt", "")
        if not prompt:
            prompt = json.dumps(task.payload, indent=2)

        logger.info(
            f"OpenClawExecutor: task={task_id}, model={model}, "
            f"thinking={thinking}, prompt_len={len(prompt)}"
        )

        # ── 3. Dry-run shortcut ──────────────────────────────────────────────
        if self.dry_run:
            return self._dry_run_result(task_id, task, model, start_time)

        # ── 4. Real execution ────────────────────────────────────────────────
        try:
            output_text = self._run_session(prompt, model, thinking)
        except TimeoutError as exc:
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.error(f"OpenClaw session timed out for task {task_id}: {exc}")
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    TaskError(
                        code="timeout",
                        message=str(exc),
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model,
                execution_time_seconds=elapsed,
            )
        except Exception as exc:
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.error(f"OpenClaw execution failed for task {task_id}: {exc}")
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    TaskError(
                        code="execution_error",
                        message=str(exc),
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model,
                execution_time_seconds=elapsed,
            )

        elapsed = (datetime.now() - start_time).total_seconds()

        if not output_text:
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    TaskError(
                        code="empty_output",
                        message="OpenClaw session returned empty output",
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model,
                execution_time_seconds=elapsed,
            )

        output_data = {"text": output_text}

        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result=output_data,
            errors=[],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used=model,
            tokens_consumed=0,  # Gateway does not expose token counts
            execution_time_seconds=elapsed,
            cost_usd=None,
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        """Return HTTP headers for gateway requests."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        return headers

    def _http_post(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST JSON *body* to *url* and return the decoded response dict."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers=self._build_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gateway HTTP error {exc.code}: {error_body}"
            ) from exc

    def _http_get(self, url: str) -> Dict[str, Any]:
        """GET *url* and return the decoded response dict."""
        req = urllib.request.Request(url, headers=self._build_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gateway HTTP error {exc.code}: {error_body}"
            ) from exc

    def _invoke_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an OpenClaw tool via the gateway's /tools/invoke endpoint.

        Returns the parsed response dict. Raises RuntimeError on failure.
        """
        url = f"{self.gateway_url}/tools/invoke"
        body = {"tool": tool_name, "args": args}
        resp = self._http_post(url, body)

        if not resp.get("ok"):
            error = resp.get("error", {})
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"Gateway tool '{tool_name}' failed: {msg}")

        return resp.get("result", {})

    def _parse_tool_text(self, result: Dict[str, Any]) -> str:
        """Extract the text payload from a tool invoke result."""
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
        return ""

    def _run_session(
        self,
        prompt: str,
        model: str,
        thinking: Optional[str],
    ) -> str:
        """Spawn a sub-agent session and poll until completion.

        Uses the gateway's ``POST /tools/invoke`` endpoint to call
        ``sessions_spawn`` and ``sessions_history``.

        Args:
            prompt:   The prompt text to execute.
            model:    Full model string (e.g. "anthropic/claude-sonnet-4-6").
            thinking: Thinking level string or None.

        Returns:
            The output text from the completed session.

        Raises:
            TimeoutError: If the session does not complete within timeout.
            RuntimeError: On HTTP errors or unexpected responses.
        """
        # ── 1. Spawn via /tools/invoke → sessions_spawn ──────────────
        spawn_args: Dict[str, Any] = {
            "task": prompt,
            "model": model,
        }
        if thinking is not None:
            spawn_args["thinking"] = thinking
        spawn_args["runTimeoutSeconds"] = self.timeout_seconds

        logger.debug(f"Spawning session via /tools/invoke → sessions_spawn")
        spawn_result = self._invoke_tool("sessions_spawn", spawn_args)

        # Extract session key from details or parse from text
        details = spawn_result.get("details", {})
        session_key = details.get("childSessionKey")
        if not session_key:
            # Fallback: parse from text content
            text = self._parse_tool_text(spawn_result)
            if text:
                try:
                    parsed = json.loads(text)
                    session_key = parsed.get("childSessionKey")
                except (json.JSONDecodeError, TypeError):
                    pass
        if not session_key:
            raise RuntimeError(
                f"Gateway spawn response missing session key. "
                f"Response: {json.dumps(spawn_result)}"
            )

        logger.info(f"Session spawned: {session_key}")

        # ── 2. Poll via /tools/invoke → sessions_history ─────────────
        deadline = time.monotonic() + self.timeout_seconds

        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"OpenClaw session {session_key} did not complete within "
                    f"{self.timeout_seconds}s"
                )

            time.sleep(POLL_INTERVAL_SECONDS)

            try:
                hist_result = self._invoke_tool("sessions_history", {
                    "sessionKey": session_key,
                    "limit": 5,
                })
            except RuntimeError as exc:
                # Session may not be ready yet
                logger.debug(f"Poll error (may be transient): {exc}")
                continue

            hist_text = self._parse_tool_text(hist_result)
            if not hist_text:
                continue

            try:
                history = json.loads(hist_text)
            except (json.JSONDecodeError, TypeError):
                continue

            messages = history.get("messages", [])
            if not messages:
                continue

            # Find the last assistant message
            last_assistant = None
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    break

            if not last_assistant:
                continue

            # Check if it's a final response (has content, not just tool calls)
            content = last_assistant.get("content", [])
            has_text = any(
                isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip()
                for c in (content if isinstance(content, list) else [])
            )

            if has_text:
                # Extract all text blocks
                text_parts = []
                for c in (content if isinstance(content, list) else []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                return "\n".join(text_parts)

            # Check for stop reason indicating completion
            stop = last_assistant.get("stopReason", "")
            if stop in ("stop", "end_turn") and not has_text:
                # Completed but empty — might have only tool calls
                logger.debug(f"Session {session_key} stopped with no text output, continuing poll...")
                continue

    @staticmethod
    def _extract_output(response: Dict[str, Any]) -> str:
        """Extract text output from a completed session response.

        Tries common keys used by different gateway versions.
        """
        for key in ("output", "result", "content", "text", "message"):
            val = response.get(key)
            if val and isinstance(val, str):
                return val
            if val and isinstance(val, dict):
                # Nested: e.g. result.text
                for inner_key in ("output", "text", "content", "message"):
                    inner = val.get(inner_key)
                    if inner and isinstance(inner, str):
                        return inner
        return ""

    # ------------------------------------------------------------------
    # Dry-run helpers
    # ------------------------------------------------------------------

    def _dry_run_result(
        self,
        task_id: str,
        task: TaskSpec,
        model: str,
        start_time: datetime,
    ) -> TaskResult:
        """Return a mock successful TaskResult without any HTTP calls."""
        elapsed = (datetime.now() - start_time).total_seconds()
        mock_output = (
            f"[dry-run] OpenClaw sub-agent would execute via model {model}. "
            f"Phase payload keys: {list(task.payload.keys())}."
        )
        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"text": mock_output, "dry_run": True},
            errors=[],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used=model,
            tokens_consumed=0,
            execution_time_seconds=elapsed,
            cost_usd=None,
        )
