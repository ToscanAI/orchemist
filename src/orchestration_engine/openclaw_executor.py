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
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from .runner import TaskExecutor
from .schemas import ModelTier, TaskError, TaskResult, TaskSpec, TaskState, TaskType

logger = logging.getLogger(__name__)

# Patterns to redact from log messages
_SECRET_PATTERNS = re.compile(
    r"(Bearer\s+)[A-Za-z0-9+/=_-]{8,}|"
    r"(Authorization:\s*)[^\s,}]+|"
    r"(token[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9+/=_-]{8,}",
    re.IGNORECASE,
)


class _SecretRedactingFilter(logging.Filter):
    """Redact secrets (bearer tokens, API keys) from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_PATTERNS.sub(r"\1<REDACTED>", record.msg)
        if record.args:
            new_args = []
            for arg in (record.args if isinstance(record.args, tuple) else (record.args,)):
                if isinstance(arg, str):
                    new_args.append(_SECRET_PATTERNS.sub(r"\1<REDACTED>", arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        return True


# Apply filter to this module's logger
logger.addFilter(_SecretRedactingFilter())


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

# Instruction appended to every sub-agent prompt so it returns its full output
# as text instead of writing it to workspace files.  The orchestrator reads the
# final assistant message text — anything written to files is invisible to it.
OUTPUT_CAPTURE_INSTRUCTION = (
    "\n\n---\n"
    "ORCHESTRATOR INSTRUCTION (do not remove):\n"
    "You are running as a sub-agent inside an orchestration pipeline.\n"
    "Return your COMPLETE output as text in your final response.\n"
    "Do NOT write output to workspace files — the orchestrator reads your\n"
    "text reply directly and cannot access files you write.\n"
    "Your final assistant message is what gets passed to the next pipeline phase.\n"
    "---"
)


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
        # Token priority: explicit arg > env var > openclaw.json config > empty
        self.gateway_token = (
            gateway_token
            if gateway_token is not None
            else os.environ.get("OPENCLAW_GATEWAY_TOKEN")
            or self._read_token_from_config()
            or ""
        )
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run

    @staticmethod
    def _read_token_from_config() -> Optional[str]:
        """Try to read gateway token from ~/.openclaw/openclaw.json."""
        try:
            config_path = os.path.expanduser("~/.openclaw/openclaw.json")
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config = json.load(f)
                token = config.get("gateway", {}).get("auth", {}).get("token", "")
                if token:
                    logger.debug("Auto-discovered gateway token from %s", config_path)
                    return token
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.debug("Could not read token from openclaw.json: %s", exc)
        return None

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

        # Append the output-capture instruction so sub-agents return their
        # full output as text rather than writing it to workspace files
        # (see issue #210).
        prompt = prompt + OUTPUT_CAPTURE_INSTRUCTION

        logger.info(
            f"OpenClawExecutor: task={task_id}, model={model}, "
            f"thinking={thinking}, prompt_len={len(prompt)}"
        )

        # ── 3. Dry-run shortcut ──────────────────────────────────────────────
        if self.dry_run:
            return self._dry_run_result(task_id, task, model, start_time)

        # ── 4. Real execution ────────────────────────────────────────────────
        # Use per-task timeout if available, otherwise fall back to executor default
        effective_timeout = (
            task.timeout_seconds
            if hasattr(task, "timeout_seconds") and task.timeout_seconds
            else self.timeout_seconds
        )
        try:
            output_text, tokens_consumed = self._run_session(
                prompt, model, thinking, timeout=effective_timeout
            )
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
            # Capture partial output from sub-agent error sessions (#212)
            partial = getattr(exc, "partial_output", "")
            partial_tokens = getattr(exc, "partial_tokens", 0)
            result_data = (
                {"partial_output": partial} if partial else {}
            )
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result=result_data,
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
                tokens_used=partial_tokens,
            )

        elapsed = (datetime.now() - start_time).total_seconds()

        if not output_text or (isinstance(output_text, str) and not output_text.strip()):
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
            tokens_consumed=tokens_consumed,
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
        timeout: Optional[int] = None,
    ) -> str:
        """Spawn a sub-agent session and poll until completion.

        Uses the gateway's ``POST /tools/invoke`` endpoint to call
        ``sessions_spawn`` and ``sessions_history``.

        Args:
            prompt:   The prompt text to execute.
            model:    Full model string (e.g. "anthropic/claude-sonnet-4-6").
            thinking: Thinking level string or None.
            timeout:  Timeout in seconds (overrides self.timeout_seconds).

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
        effective_timeout = timeout or self.timeout_seconds
        spawn_args["runTimeoutSeconds"] = effective_timeout

        logger.debug(
            f"Spawning session via /tools/invoke → sessions_spawn "
            f"(timeout={effective_timeout}s)"
        )
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
        deadline = time.monotonic() + effective_timeout

        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"OpenClaw session {session_key} did not complete within "
                    f"{effective_timeout}s"
                )

            time.sleep(POLL_INTERVAL_SECONDS)

            try:
                hist_result = self._invoke_tool("sessions_history", {
                    "sessionKey": session_key,
                    "limit": 50,
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

            # Check for stop reason indicating completion
            stop = last_assistant.get("stopReason", "")
            _TERMINAL_REASONS = {"stop", "end_turn", "error"}
            if stop not in _TERMINAL_REASONS:
                # Not yet complete — still generating or using tools
                logger.debug(f"Session {session_key}: stopReason='{stop}', still running...")
                continue

            is_error = stop == "error"
            if is_error:
                logger.warning(
                    f"Session {session_key}: sub-agent ended with stopReason='error'. "
                    "Capturing partial output and marking phase as failed."
                )

            # Session completed — extract text from ALL assistant messages in
            # order so that sub-agents which produce output across multiple
            # turns (or whose final message is a brief summary after earlier
            # substantive replies) are captured fully.  This addresses #210
            # where the orchestrator only received the ~2 KB final summary
            # instead of the full ~17-30 KB output.
            text_parts = []
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                mc = msg.get("content", [])
                for c in (mc if isinstance(mc, list) else []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "").strip()
                        if text:
                            text_parts.append(text)

            output = "\n\n".join(text_parts)

            # Extract token usage — sessions_history doesn't include per-message
            # usage, so we query sessions_list for the session's totalTokens
            total_tokens = 0
            try:
                list_result = self._invoke_tool("sessions_list", {
                    "activeMinutes": 60,
                })
                list_text = self._parse_tool_text(list_result)
                if list_text:
                    sessions_data = json.loads(list_text)
                    sessions = (
                        sessions_data if isinstance(sessions_data, list)
                        else sessions_data.get("sessions", [])
                    )
                    for s in sessions:
                        sk = s.get("sessionKey", "") or s.get("key", "")
                        if sk == session_key:
                            total_tokens = s.get("totalTokens", 0)
                            break
            except Exception as exc:
                logger.debug(f"Could not extract token count: {exc}")

            logger.info(
                f"Session {session_key} completed: {len(output)} chars, "
                f"{total_tokens} tokens"
            )

            if is_error:
                # Return partial output so the caller can include it in the
                # phase result, but raise so the phase is marked FAILED.
                # We embed the captured text in the exception so the caller's
                # generic ``except Exception`` handler can surface it.
                err = RuntimeError(
                    f"Sub-agent ended with stopReason='error'. "
                    f"Partial output ({len(output)} chars) captured."
                )
                err.partial_output = output  # type: ignore[attr-defined]
                err.partial_tokens = total_tokens  # type: ignore[attr-defined]
                raise err

            return output, total_tokens

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
