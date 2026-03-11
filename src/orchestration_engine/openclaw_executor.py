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
import math
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .errors import (
    AuthenticationError,
    GatewayHTTPError,
    GatewayUnavailableError,
    RateLimitError,
    classify_http_error,
)
from .model_fallback import ModelFallbackChain
from .recovery import CircuitBreakerState, ErrorType, ExecutorRetryConfig, classify_exception_error_type
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
# 20 minutes — used when no per-phase or per-executor timeout is provided (#240).
#
# MIGRATION NOTE (issue #240): This was raised from 600 s (10 min) to 1200 s
# (20 min) to accommodate longer sub-agent research sessions.  Any orchestrator
# config or CI pipeline that relied on the previous 10-minute ceiling should be
# reviewed.  Pass an explicit ``timeout_seconds`` to the constructor or a per-
# task ``timeout_seconds`` to override this default for a specific executor or
# task.
DEFAULT_TIMEOUT_SECONDS = 1200

# How long to sleep between each poll of the session status endpoint.
POLL_INTERVAL_SECONDS = 3.0

# Maximum number of messages to request from sessions_history in a single call.
#
# NOTE (issue #239): The OpenClaw gateway's sessions_history tool does NOT support
# offset-based pagination — no "offset" or "before" parameters are documented or
# accepted by the /tools/invoke endpoint.  We therefore use the highest safe limit
# as a ceiling so that even long sub-agent research sessions (web searches, multi-
# turn conversations) are captured in full.  If a session produces MORE than
# SESSIONS_HISTORY_LIMIT messages, a warning is emitted, but pagination cannot be
# attempted without gateway support.  Should the gateway add offset/cursor support
# in a future release, replace the single-call fetch with a paginating helper.
SESSIONS_HISTORY_LIMIT: int = 1000

# ---------------------------------------------------------------------------
# Module-level circuit-breaker registry (issue #346)
# ---------------------------------------------------------------------------
# Shared across all OpenClawExecutor instances in the same process, keyed by
# the resolved model string (e.g. "anthropic/claude-sonnet-4-6").  A single
# lock guards all read-modify-write operations on the dict and its values so
# that concurrent workers do not corrupt the failure counters.
_CIRCUIT_BREAKERS: Dict[str, CircuitBreakerState] = {}
_CIRCUIT_BREAKERS_LOCK = threading.Lock()


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
        self.timeout_seconds = timeout_seconds if timeout_seconds else DEFAULT_TIMEOUT_SECONDS
        self.dry_run = dry_run

        # ── Graceful shutdown support (Issue #488) ───────────────────────────
        # Tracks the session key of the currently running sub-agent session.
        # Set immediately after spawn; cleared on completion or error.
        self._active_session_key: Optional[str] = None

        # Event set by request_shutdown() to interrupt the polling loop.
        # The SIGTERM handler in daemon.py calls request_shutdown() to propagate
        # the shutdown signal into the executor without waiting for the session
        # to complete naturally.
        self._shutdown_event: threading.Event = threading.Event()

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
    # Graceful shutdown API (Issue #488)
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        """Signal the executor to exit its polling loop on the next iteration.

        Safe to call from a signal handler or another thread.  Sets
        ``_shutdown_event`` so ``_run_session()`` breaks out of the polling
        loop on the next ``time.sleep`` wakeup instead of blocking until the
        sub-agent session completes or times out.

        Logs the active session key (if any) so the orphaned session is
        traceable in the daemon log.
        """
        self._shutdown_event.set()
        if self._active_session_key:
            logger.warning(
                "Shutdown requested — active session %s will be abandoned",
                self._active_session_key,
            )
        else:
            logger.warning("Shutdown requested — no active session to abandon")

    def cancel_active_session(self) -> None:
        """Best-effort cancellation of the currently active sub-agent session.

        1. Logs the orphaned session key for post-mortem debugging.
        2. Attempts to invoke ``sessions_stop`` via the gateway API.  If the
           gateway does not support this tool (or returns an error), the
           failure is logged as a warning and silently swallowed — the daemon
           must still exit cleanly.
        3. Clears ``_active_session_key`` regardless of whether the stop
           call succeeded.

        This method is **idempotent**: calling it when no session is active is
        a no-op.
        """
        session_key = self._active_session_key
        if not session_key:
            logger.debug("cancel_active_session: no active session to cancel")
            return

        logger.warning(
            "Cancelling orphaned session: %s "
            "(best-effort — session may continue running on gateway)",
            session_key,
        )
        try:
            self._invoke_tool("sessions_stop", {"sessionKey": session_key})
            logger.info("sessions_stop succeeded for session %s", session_key)
        except Exception as exc:
            logger.warning(
                "sessions_stop not supported or failed for session %s (non-fatal): %s",
                session_key,
                exc,
            )
        finally:
            self._active_session_key = None

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

        # ── COMMAND TASK: run locally via subprocess, skip LLM agent ────────
        if task.type == TaskType.COMMAND:
            return self._execute_command_task(task, start_time, task_id)

        # ── ACCEPTANCE_RUN TASK: run pytest locally, skip LLM agent ─────────
        if task.type == TaskType.ACCEPTANCE_RUN:
            return self._execute_acceptance_run_task(task, start_time, task_id)

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

        # ── 4. Real execution with exponential-backoff retry (issue #346) ───
        # Use per-task timeout if available, otherwise fall back to executor default.
        effective_timeout = (
            task.timeout_seconds
            if hasattr(task, "timeout_seconds") and task.timeout_seconds
            else self.timeout_seconds
        )

        retry_cfg = ExecutorRetryConfig()

        # ── 4a. Circuit-breaker pre-check ────────────────────────────────────
        # Ensure an entry exists in the shared registry for this model key.
        with _CIRCUIT_BREAKERS_LOCK:
            if model not in _CIRCUIT_BREAKERS:
                _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)
            cb_state = _CIRCUIT_BREAKERS[model]

        # circuit_breaker_reset_seconds → minutes (CircuitBreakerState uses minutes)
        cb_reset_minutes = retry_cfg.circuit_breaker_reset_seconds // 60

        if cb_state.is_open(retry_cfg.circuit_breaker_threshold, cb_reset_minutes):
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.warning(
                "Circuit breaker open for model %s — skipping task %s without attempt",
                model, task_id,
            )
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    TaskError(
                        code="circuit_open",
                        message=(
                            f"Circuit breaker is open for model {model}: "
                            f"too many consecutive failures. "
                            f"Will retry automatically after {retry_cfg.circuit_breaker_reset_seconds}s."
                        ),
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model,
                execution_time_seconds=elapsed,
            )

        # ── 4a'. Instantiate fallback chain (issue #347) ─────────────────────
        # Build an ordered list of model tiers to try.  When the template
        # specifies ``model_chain`` the explicit list is used; otherwise the
        # ``ModelFallbackChain`` constructor falls back to DEFAULT_MODEL_CHAIN
        # (["sonnet", "opus"]).  The chain's first entry drives the model used
        # in the first iteration of the outer loop below.
        _chain_tiers = task.payload.get("model_chain") or []
        if not _chain_tiers:
            # No explicit chain — seed from the current tier_key so that a
            # haiku-configured phase still starts with haiku and only falls
            # back to opus on exhaustion.
            _implicit = [tier_key] if tier_key else ["sonnet"]
            if _implicit[0] != "opus":
                _implicit = _implicit + ["opus"]
            _chain_tiers = _implicit
        chain = ModelFallbackChain(_chain_tiers)
        # Sync the model variable with the chain's starting tier.
        model = MODEL_MAP.get(chain.current(), MODEL_MAP["sonnet"])

        # ── 4b. Outer fallback loop — iterates over model chain ──────────────
        # Outer loop: each iteration exhausts all retry attempts on the current
        # model tier before escalating to the next tier in the chain.
        # Inner retry loop (4b-inner) is unchanged from issue #346.
        output_text: Optional[str] = None
        tokens_consumed: int = 0
        last_exc: Optional[Exception] = None
        last_error_code: str = "execution_error"
        last_error_msg: str = "Unknown error"
        partial_output: str = ""
        partial_tokens: int = 0
        succeeded: bool = False
        # Set to True when the inner loop breaks due to a PERMANENT error —
        # permanent errors must NOT trigger model escalation.
        permanent_failure: bool = False

        while True:  # outer loop over model chain (issue #347)
            # Ensure a circuit-breaker entry exists for the current model tier.
            with _CIRCUIT_BREAKERS_LOCK:
                if model not in _CIRCUIT_BREAKERS:
                    _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)

            # ── CB pre-check per outer-loop iteration ─────────────────────────
            # When we advance to a new tier (e.g. after a `continue` from the
            # CB-blocked escalation below), we need to verify the new tier's CB
            # is not also open before running its inner retry loop.
            with _CIRCUIT_BREAKERS_LOCK:
                cb_cur = _CIRCUIT_BREAKERS[model]
            if cb_cur.is_open(retry_cfg.circuit_breaker_threshold, cb_reset_minutes):
                if chain.has_next():
                    prev = chain.current()
                    nxt = chain.advance()
                    model = MODEL_MAP.get(nxt, MODEL_MAP["sonnet"])
                    logger.warning(
                        "Task %s: circuit breaker open for model '%s' (tier '%s') — "
                        "skipping and escalating to tier '%s'",
                        task_id, MODEL_MAP.get(prev, prev), prev, nxt,
                    )
                    continue
                else:
                    # All tiers circuit-broken — exit with failure.
                    break

            # ── 4b-inner. Per-model retry loop (unchanged from #346) ─────────
            for attempt in range(retry_cfg.max_attempts):
                if attempt > 0:
                    # Exponential backoff: backoff_base * backoff_multiplier^(retry_index)
                    # where retry_index = attempt - 1 (0-based retry counter).
                    retry_index = attempt - 1
                    wait_seconds = min(
                        retry_cfg.backoff_base * (retry_cfg.backoff_multiplier ** retry_index),
                        retry_cfg.backoff_max,
                    )
                    logger.info(
                        "Task %s: retry %d/%d after %.1fs backoff (error: %s)",
                        task_id,
                        attempt,
                        retry_cfg.max_attempts - 1,
                        wait_seconds,
                        last_error_code,
                    )
                    time.sleep(wait_seconds)

                try:
                    output_text, tokens_consumed = self._run_session(
                        prompt, model, thinking, timeout=effective_timeout
                    )
                    # ── Success path ─────────────────────────────────────────
                    with _CIRCUIT_BREAKERS_LOCK:
                        _CIRCUIT_BREAKERS[model].record_success()
                    succeeded = True
                    break

                except Exception as exc:
                    last_exc = exc
                    error_type = classify_exception_error_type(exc)

                    # Record failure in the shared circuit breaker.
                    with _CIRCUIT_BREAKERS_LOCK:
                        _CIRCUIT_BREAKERS[model].record_failure(
                            retry_cfg.circuit_breaker_threshold
                        )

                    # Preserve partial output if the sub-agent produced any before failing.
                    partial_output = getattr(exc, "partial_output", "") or ""
                    partial_tokens = getattr(exc, "partial_tokens", 0) or 0

                    # Determine error code and emit a log at the appropriate level.
                    if isinstance(exc, TimeoutError):
                        last_error_code = "timeout"
                        last_error_msg = str(exc)
                        logger.error(
                            "OpenClaw session timed out for task %s (attempt %d): %s",
                            task_id, attempt + 1, exc,
                        )
                    elif isinstance(exc, RateLimitError):
                        last_error_code = "rate_limited"
                        last_error_msg = str(exc)
                        retry_hint = (
                            f" retry after {exc.retry_after}s"
                            if exc.retry_after is not None else ""
                        )
                        logger.warning(
                            "Rate limited (429) —%s for task %s (attempt %d)",
                            retry_hint, task_id, attempt + 1,
                        )
                    else:
                        last_error_code = "execution_error"
                        last_error_msg = str(exc)
                        logger.error(
                            "OpenClaw execution failed for task %s (attempt %d): %s",
                            task_id, attempt + 1, exc,
                        )

                    # Permanent errors should not be retried or escalated.
                    if error_type == ErrorType.PERMANENT:
                        logger.info(
                            "Task %s: not retrying after PERMANENT error (%s): %s",
                            task_id, type(exc).__name__, exc,
                        )
                        permanent_failure = True
                        break

                    # If the circuit breaker has just opened, stop retrying early.
                    with _CIRCUIT_BREAKERS_LOCK:
                        cb_now_open = _CIRCUIT_BREAKERS[model].is_open(
                            retry_cfg.circuit_breaker_threshold, cb_reset_minutes
                        )
                    if cb_now_open:
                        logger.warning(
                            "Circuit breaker opened for model %s after task %s failure — "
                            "aborting retry loop",
                            model, task_id,
                        )
                        break

                    # Otherwise loop to the next attempt (backoff applied at top of loop).

            # ── 4b-outer. After inner retry loop: check chain for escalation ─
            if succeeded:
                # Task completed successfully — exit outer loop.
                break

            if permanent_failure:
                # Permanent errors (auth, bad-request) must not be retried on
                # a different model — escalation would also fail.
                break

            if chain.has_next():
                # All retries exhausted on the current tier — escalate to next.
                prev_tier = chain.current()
                next_tier = chain.advance()
                new_model = MODEL_MAP.get(next_tier, MODEL_MAP["sonnet"])
                logger.warning(
                    "Task %s: all retries exhausted on model '%s' (tier '%s') — "
                    "escalating to tier '%s' (model '%s')",
                    task_id, model, prev_tier, next_tier, new_model,
                )
                model = new_model

                # Circuit-breaker pre-check for the new model tier.
                with _CIRCUIT_BREAKERS_LOCK:
                    if model not in _CIRCUIT_BREAKERS:
                        _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)
                    cb_new = _CIRCUIT_BREAKERS[model]

                if cb_new.is_open(retry_cfg.circuit_breaker_threshold, cb_reset_minutes):
                    logger.warning(
                        "Circuit breaker open for escalated model %s — "
                        "skipping tier '%s' for task %s",
                        model, next_tier, task_id,
                    )
                    # Treat the CB-blocked tier as exhausted and try the next one.
                    continue

                # Reset per-attempt state and retry on the new model.
                succeeded = False
                continue

            else:
                # All tiers exhausted — fall through to failure path.
                break

        # ── 4c. Handle overall failure ───────────────────────────────────────
        if not succeeded:
            elapsed = (datetime.now() - start_time).total_seconds()
            result_data = {"partial_output": partial_output} if partial_output else {}
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result=result_data,
                errors=[
                    TaskError(
                        code=last_error_code,
                        message=last_error_msg,
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model,
                execution_time_seconds=elapsed,
                tokens_consumed=partial_tokens,
            )

        # ── 4d. Post-success checks ──────────────────────────────────────────
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
    # Command task handling
    # ------------------------------------------------------------------

    def _execute_command_task(
        self,
        task: TaskSpec,
        start_time: datetime,
        task_id: str,
    ) -> TaskResult:
        """Execute a TaskType.COMMAND task locally via subprocess.

        Security model:
        - Command is parsed with shlex.split (no shell interpolation)
        - shell=False prevents shell injection
        - Executable (argv[0]) is validated against allowed_commands whitelist
          when a non-empty whitelist is provided
        - Output is written to {output_dir}/{task_id}.md when output_dir is set

        Args:
            task:       TaskSpec with type == TaskType.COMMAND.
            start_time: Datetime when the task started (already captured).
            task_id:    Stable task identifier string.

        Returns:
            TaskResult with stdout+stderr captured in result['output'].
        """
        raw_command: str = task.payload.get("command", "")
        allowed_commands: List[str] = task.payload.get("allowed_commands", [])
        output_dir: str = task.payload.get("output_dir", "")
        working_dir: Optional[str] = task.payload.get("working_dir") or None
        timeout_sec: int = (
            task.timeout_seconds
            if hasattr(task, "timeout_seconds") and task.timeout_seconds
            else 300
        )

        # ── Validation ────────────────────────────────────────────────
        if not raw_command.strip():
            return self._command_error(
                task_id, task, start_time,
                "command_missing",
                "No 'command' specified in task payload",
            )

        try:
            cmd_parts = shlex.split(raw_command)
        except ValueError as exc:
            return self._command_error(
                task_id, task, start_time,
                "command_parse_error",
                f"shlex.split failed: {exc}",
            )

        if not cmd_parts:
            return self._command_error(
                task_id, task, start_time,
                "command_empty",
                "Command is empty after parsing",
            )

        executable = cmd_parts[0]

        # Whitelist check — only enforce when a non-empty list is provided
        if allowed_commands:
            if executable not in allowed_commands:
                return self._command_error(
                    task_id, task, start_time,
                    "command_not_allowed",
                    f"Command '{executable}' is not in allowed_commands: {allowed_commands}",
                )

        logger.info(
            "OpenClawExecutor: COMMAND task=%s, executable=%s, cwd=%s",
            task_id, executable, working_dir or "<inherit>",
        )

        # ── Dry-run shortcut ──────────────────────────────────────────
        if self.dry_run:
            mock_output = (
                f"[dry-run] Would execute: {raw_command}\n"
                f"[dry-run] working_dir={working_dir}, allowed={allowed_commands}"
            )
            elapsed = (datetime.now() - start_time).total_seconds()
            self._write_command_output(output_dir, task_id, raw_command, mock_output, 0)
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS,
                confidence=1.0,
                result={"output": mock_output, "dry_run": True, "command": raw_command},
                errors=[],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-subprocess",
                execution_time_seconds=elapsed,
            )

        # ── Execute ───────────────────────────────────────────────────
        try:
            proc = subprocess.run(
                cmd_parts,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=working_dir,
            )
        except subprocess.TimeoutExpired:
            return self._command_error(
                task_id, task, start_time,
                "timeout",
                f"Command timed out after {timeout_sec}s",
            )
        except FileNotFoundError:
            return self._command_error(
                task_id, task, start_time,
                "executable_not_found",
                f"Executable not found: {executable}",
            )
        except Exception as exc:
            return self._command_error(
                task_id, task, start_time,
                "execution_error",
                str(exc),
            )

        combined_output = (proc.stdout or "") + (proc.stderr or "")
        elapsed = (datetime.now() - start_time).total_seconds()

        # ── Write output file ─────────────────────────────────────────
        self._write_command_output(
            output_dir, task_id, raw_command, combined_output, proc.returncode
        )

        # ── Return result ─────────────────────────────────────────────
        if proc.returncode == 0:
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS,
                confidence=1.0,
                result={
                    "output": combined_output,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "return_code": proc.returncode,
                    "command": raw_command,
                },
                errors=[],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-subprocess",
                execution_time_seconds=elapsed,
            )
        else:
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={
                    "output": combined_output,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "return_code": proc.returncode,
                    "command": raw_command,
                },
                errors=[
                    TaskError(
                        code="command_failed",
                        message=(
                            f"Command exited with code {proc.returncode}. "
                            f"stderr: {proc.stderr[:500]}"
                        ),
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-subprocess",
                execution_time_seconds=elapsed,
            )

    def _execute_acceptance_run_task(
        self,
        task: TaskSpec,
        start_time: datetime,
        task_id: str,
    ) -> TaskResult:
        """Execute acceptance tests via pytest — engine-side, no LLM agent.

        Reads ``output_dir`` from ``task.payload``, runs pytest on
        ``{output_dir}/acceptance_tests.py``, writes engine-verified results
        to ``{output_dir}/acceptance_results.json``, and returns a
        ``TaskResult`` with ``state=SUCCESS`` iff all tests pass.

        Args:
            task:       TaskSpec with type == TaskType.ACCEPTANCE_RUN.
            start_time: Datetime when the task started (already captured).
            task_id:    Stable task identifier string.

        Returns:
            TaskResult with pytest outcome in metadata and failure summary in
            result['text'] (for downstream prompt feedback).
        """
        from . import test_runner  # local import to avoid circular deps at module load

        output_dir: str = task.payload.get("output_dir", "")
        timeout_sec: int = (
            task.timeout_seconds
            if hasattr(task, "timeout_seconds") and task.timeout_seconds
            else 300
        )

        if not output_dir:
            return self._acceptance_run_error(
                task_id, task, start_time,
                "missing_output_dir",
                "No 'output_dir' in task payload for acceptance_run phase",
            )

        test_file = os.path.join(output_dir, "acceptance_tests.py")

        if not os.path.exists(test_file):
            return self._acceptance_run_error(
                task_id, task, start_time,
                "test_file_not_found",
                f"acceptance_tests.py not found at: {test_file}",
            )

        # ── Dry-run shortcut ──────────────────────────────────────────
        if self.dry_run:
            mock_result = test_runner.TestRunResult(
                passed=3, failed=0, errors=0, total=3,
                pass_rate=1.0,
                failure_details="",
                full_output="[dry-run]",
                exit_code=0,
            )
            test_runner.write_acceptance_results(mock_result, output_dir)
            elapsed = (datetime.now() - start_time).total_seconds()
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS,
                confidence=1.0,
                result={"text": "[dry-run] acceptance_run: 3 passed, 0 failed"},
                errors=[],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-subprocess",
                execution_time_seconds=elapsed,
            )

        # ── Execute pytest ────────────────────────────────────────────
        try:
            result = test_runner.run_pytest(test_file, timeout_seconds=timeout_sec)
        except Exception as exc:
            return self._acceptance_run_error(
                task_id, task, start_time, "execution_error", str(exc)
            )

        test_runner.write_acceptance_results(result, output_dir)

        elapsed = (datetime.now() - start_time).total_seconds()
        all_passed = (result.failed == 0 and result.errors == 0 and result.total > 0)
        state = TaskState.SUCCESS if all_passed else TaskState.FAILED

        summary = test_runner.format_failure_summary(result)

        logger.info(
            "OpenClawExecutor: ACCEPTANCE_RUN task=%s state=%s "
            "passed=%d failed=%d errors=%d total=%d",
            task_id, state.value,
            result.passed, result.failed, result.errors, result.total,
        )

        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=state,
            confidence=result.pass_rate,
            result={"text": summary},
            errors=[],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used="local-subprocess",
            execution_time_seconds=elapsed,
        )

    def _acceptance_run_error(
        self,
        task_id: str,
        task: TaskSpec,
        start_time: datetime,
        code: str,
        message: str,
    ) -> TaskResult:
        """Return a FAILED TaskResult for an acceptance_run-phase error."""
        logger.error("ACCEPTANCE_RUN task %s failed: [%s] %s", task_id, code, message)
        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={"text": ""},
            errors=[TaskError(code=code, message=message, severity="error")],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used="local-subprocess",
            execution_time_seconds=(datetime.now() - start_time).total_seconds(),
        )

    def _write_command_output(
        self,
        output_dir: str,
        task_id: str,
        raw_command: str,
        combined_output: str,
        return_code: int,
    ) -> None:
        """Write command output to {output_dir}/{safe_task_id}.md.

        Skips silently (with a warning log) if output_dir is not set or the
        write fails — the TaskResult already contains the output in memory.
        """
        if not output_dir:
            return
        try:
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", task_id)
            out_path = os.path.join(output_dir, f"{safe_id}.md")
            os.makedirs(output_dir, exist_ok=True)
            with open(out_path, "w") as f:
                f.write(f"# Command Output — {task_id}\n\n")
                f.write(f"**Command:** `{raw_command}`\n")
                f.write(f"**Exit code:** {return_code}\n\n")
                f.write("```\n")
                f.write(combined_output)
                f.write("\n```\n")
            logger.info("Command output written to %s", out_path)
        except OSError as exc:
            logger.warning("Could not write command output to file: %s", exc)

    def _command_error(
        self,
        task_id: str,
        task: TaskSpec,
        start_time: datetime,
        code: str,
        message: str,
    ) -> TaskResult:
        """Return a FAILED TaskResult for a command-phase error."""
        logger.error("COMMAND task %s failed: [%s] %s", task_id, code, message)
        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            errors=[TaskError(code=code, message=message, severity="error")],
            started_at=start_time,
            completed_at=datetime.now(),
            model_used="local-subprocess",
            execution_time_seconds=(datetime.now() - start_time).total_seconds(),
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
            raise classify_http_error(exc.code, error_body, exc.headers) from exc

    def _http_get(self, url: str) -> Dict[str, Any]:
        """GET *url* and return the decoded response dict."""
        req = urllib.request.Request(url, headers=self._build_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise classify_http_error(exc.code, error_body, exc.headers) from exc

    def _emit_stall_event(
        self,
        session_key: str,
        stall_seconds: float,
        last_tokens: int,
    ) -> None:
        """Emit a stall-detection event to the DB for SSE consumers (#413).

        Best-effort: failures are logged but never propagated to the caller.
        """
        try:
            from .db import Database

            db = Database()
            # Find the active pipeline run for this session (if any)
            runs = db.list_pipeline_runs(limit=5)
            for run in runs:
                if run.get("status") == "running":
                    db.insert_pipeline_run_event(
                        run_id=run["run_id"],
                        event_type="stall_detected",
                        phase_id=run.get("current_phase"),
                        metadata={
                            "session_key": session_key,
                            "stall_seconds": round(stall_seconds, 1),
                            "last_tokens": last_tokens,
                            "message": (
                                f"No token progress for {stall_seconds:.0f}s — "
                                "possible rate limit"
                            ),
                        },
                    )
                    break
        except Exception as exc:
            logger.debug("Could not emit stall event: %s", exc)

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

        # Resolve effective timeout (#240).
        # Use explicit None-check (not falsy "or") so that a caller-provided
        # value is never silently ignored.
        # Fallback chain: explicit timeout → self.timeout_seconds.
        # The module constant DEFAULT_TIMEOUT_SECONDS serves as the constructor
        # default, not as a hard-coded override here — using self.timeout_seconds
        # ensures that a custom per-executor timeout (e.g.
        # OpenClawExecutor(timeout_seconds=300)) is honoured by _run_session.
        # Infinite timeouts are re-mapped to DEFAULT_TIMEOUT_SECONDS to avoid a
        # deadline that can never be exceeded.
        # Zero or negative timeouts are rejected early with a clear ValueError to avoid
        # a ZeroDivisionError in the 80% warning path.
        if timeout is None:
            effective_timeout: float = float(self.timeout_seconds)
        elif math.isinf(float(timeout)):
            logger.warning(
                "Infinite timeout requested; falling back to DEFAULT_TIMEOUT_SECONDS=%ds",
                DEFAULT_TIMEOUT_SECONDS,
            )
            effective_timeout = float(DEFAULT_TIMEOUT_SECONDS)
        elif timeout <= 0 or math.isnan(float(timeout)):
            raise ValueError(
                f"timeout must be a positive integer (got {timeout!r}). "
                "Use timeout=None to apply the executor default."
            )
        else:
            effective_timeout = float(timeout)

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

        # Track the active session key so SIGTERM handler can identify and
        # cancel orphaned sessions (Issue #488).
        self._active_session_key = session_key

        # ── 2. Poll via /tools/invoke → sessions_history ─────────────
        loop_start: float = time.monotonic()
        deadline: float = loop_start + effective_timeout
        # Total window in seconds — stored separately so it remains immutable
        # across loop iterations (deadline is derived from it once).
        total_timeout: float = effective_timeout

        # 80% warning state — fired at most once per session (#240 AC-4).
        warning_fired: bool = False

        # SESSIONS_HISTORY_LIMIT ceiling warning — fired at most once per session (#239).
        # Mirrors the warning_fired pattern to avoid log spam when a long session
        # stabilises at exactly SESSIONS_HISTORY_LIMIT messages across many polls.
        limit_warning_fired: bool = False

        # Session cleanup detection state (#241).
        # Tracks whether any poll has ever returned a non-empty message list.
        # If True and the current poll returns empty, the session was GC'd.
        had_messages: bool = False

        # ── Rate limit / stall detection (#413) ─────────────────────────
        # Track token progress to detect stalls (possible rate limiting).
        _last_token_count: int = 0
        _last_token_change_time: float = loop_start
        _stall_warned: bool = False
        _STALL_THRESHOLD_SECONDS: float = 60.0  # configurable threshold

        while True:
            now: float = time.monotonic()

            # ── Shutdown check (Issue #488) ───────────────────────────────
            # Checked before the deadline so SIGTERM immediately breaks the
            # loop without waiting for the next poll cycle.  The caller's
            # exception handler records the session key as orphaned.
            if self._shutdown_event.is_set():
                logger.warning(
                    "Shutdown event set — exiting poll loop for session %s "
                    "(session may still be running on gateway)",
                    session_key,
                )
                raise RuntimeError(
                    f"Session {session_key} polling interrupted by shutdown request"
                )

            # ── Deadline check (AC-3, AC-5) ──────────────────────────────
            # Evaluated on every iteration, including after successful but
            # non-terminal gateway responses, so the loop cannot spin forever.
            if now > deadline:
                raise TimeoutError(
                    f"OpenClaw session {session_key} did not complete within "
                    f"{effective_timeout}s"
                )

            # ── 80% elapsed warning (AC-4) ────────────────────────────────
            elapsed: float = now - loop_start
            if not warning_fired and elapsed >= 0.8 * total_timeout:
                logger.warning(
                    "Session %s: %.0f%% of timeout elapsed (%.1fs / %ds) — "
                    "session may not complete in time.",
                    session_key,
                    100.0 * elapsed / total_timeout,
                    elapsed,
                    total_timeout,
                )
                warning_fired = True

            time.sleep(POLL_INTERVAL_SECONDS)

            try:
                hist_result = self._invoke_tool("sessions_history", {
                    "sessionKey": session_key,
                    "limit": SESSIONS_HISTORY_LIMIT,
                })
            except (RuntimeError, GatewayHTTPError) as exc:
                # Session may not be ready yet; includes transient HTTP errors
                logger.debug(f"Poll error (may be transient): {exc}")
                continue

            hist_text = self._parse_tool_text(hist_result)
            if not hist_text:
                continue

            try:
                history = json.loads(hist_text)
            except (json.JSONDecodeError, TypeError):
                continue

            messages = history.get("messages") or []

            # Warn if the response is at the limit ceiling — some messages may
            # be missing if the session produced more than SESSIONS_HISTORY_LIMIT
            # entries.  Offset-based pagination is NOT supported by the current
            # gateway (sessions_history has no "offset" parameter).  See #239.
            # Guard with limit_warning_fired so the warning appears at most once
            # per session, even if the session stalls at exactly the limit for
            # many poll iterations.
            if len(messages) == SESSIONS_HISTORY_LIMIT and not limit_warning_fired:
                logger.warning(
                    "sessions_history returned %d messages for session %s — "
                    "response is at the limit ceiling, some earlier content may "
                    "be lost.  Consider reducing sub-agent verbosity or requesting "
                    "gateway-side pagination support.",
                    SESSIONS_HISTORY_LIMIT,
                    session_key,
                )
                limit_warning_fired = True

            # ── Session cleanup detection (#241) ──────────────────────────
            # A session that previously had messages but now returns an empty
            # list has been garbage-collected by the gateway.
            if not messages:
                if had_messages:
                    raise RuntimeError(
                        f"Session {session_key} was garbage-collected: "
                        f"history previously contained messages but is now empty. "
                        f"The gateway may have evicted the session."
                    )
                # Session not yet started — treat as "not ready", keep polling.
                continue

            # Mark that this session has produced at least one message (AC-7).
            had_messages = True

            # ── Stall detection (#413) ───────────────────────────────────
            # Extract total token count from the last assistant message's
            # usage data to detect stalls (possible rate limiting).
            _current_tokens = 0
            for _msg in reversed(messages):
                if _msg.get("role") == "assistant":
                    _usage = _msg.get("usage", {})
                    _current_tokens = (
                        _usage.get("totalTokens", 0)
                        or _usage.get("input", 0) + _usage.get("output", 0)
                    )
                    break

            if _current_tokens > _last_token_count:
                # Progress detected — reset stall timer
                _last_token_count = _current_tokens
                _last_token_change_time = time.monotonic()
                if _stall_warned:
                    logger.info(
                        "Session %s: token progress resumed (now %d tokens)",
                        session_key, _current_tokens,
                    )
                    _stall_warned = False
            else:
                # No progress — check if stalled
                stall_seconds = time.monotonic() - _last_token_change_time
                if stall_seconds >= _STALL_THRESHOLD_SECONDS and not _stall_warned:
                    logger.warning(
                        "Session %s: no token progress for %.0fs — "
                        "possible rate limit (last tokens: %d)",
                        session_key, stall_seconds, _last_token_count,
                    )
                    _stall_warned = True
                    # Emit stall event for SSE consumers (#413)
                    self._emit_stall_event(
                        session_key, stall_seconds, _last_token_count
                    )

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
            _TERMINAL_REASONS = {"stop", "end_turn", "error", "max_tokens"}
            if stop not in _TERMINAL_REASONS:
                # Not yet complete — still generating or using tools
                logger.debug(f"Session {session_key}: stopReason='{stop}', still running...")
                continue

            # Issue #482: Don't treat gateway-retryable API errors as terminal.
            # The gateway retries overloaded/rate-limit errors internally,
            # producing new assistant messages on success. Continue polling
            # to capture them. The executor's timeout acts as safety net.
            if stop == "error":
                error_msg = last_assistant.get("errorMessage", "")
                _GATEWAY_RETRYABLE_ERRORS = {
                    "overloaded_error",
                    "rate_limit_error",
                    "api_error",
                }
                if any(err_type in error_msg for err_type in _GATEWAY_RETRYABLE_ERRORS):
                    logger.debug(
                        "Session %s: stopReason='error' with gateway-retryable "
                        "error (%s), continuing to poll...",
                        session_key,
                        error_msg[:120],
                    )
                    continue

            is_error = stop in ("error", "max_tokens")
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
            #
            # Root-cause detail (issue #210): the `content` field of a
            # sessions_history message can be either:
            #   (a) A list of content blocks: [{"type": "text", "text": "..."}, ...]
            #       — standard Anthropic API format.
            #   (b) A plain string: "..."
            #       — used by some OpenClaw gateway response shapes.
            #
            # The original code `for c in (mc if isinstance(mc, list) else []):`
            # silently dropped string content because `isinstance(str, list)` is
            # False, causing the inner loop to iterate over an empty list.
            # Only list-format messages contributed to `text_parts`; string-format
            # messages — which may carry the bulk of the sub-agent's output —
            # were invisible to the orchestrator.  This was the root cause of
            # truncation: the assembled output was a subset of what the sub-agent
            # actually produced.
            text_parts = []
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                mc = msg.get("content", [])
                if isinstance(mc, str):
                    # Plain-string content — include directly (case b above).
                    text = mc.strip()
                    if text:
                        text_parts.append(text)
                else:
                    # List of content blocks — extract only "text" typed blocks.
                    # "tool_use" blocks carry tool-call parameters (not user-visible
                    # output) and "thinking" blocks carry internal reasoning; both
                    # are intentionally skipped.
                    for c in (mc if isinstance(mc, list) else []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "").strip()
                            if text:
                                text_parts.append(text)

            output = "\n\n".join(text_parts)

            # Diagnostic logging for capture-size analysis (issue #210).
            # Logs per-part and total sizes so truncation can be detected
            # by comparing captured chars against the token-based estimate.
            logger.debug(
                "Session %s: assembled output from %d assistant text block(s); "
                "sizes: [%s]; total=%d chars",
                session_key,
                len(text_parts),
                ", ".join(str(len(p)) for p in text_parts),
                len(output),
            )

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

            # Estimate expected chars from tokens (rough heuristic: ~4 chars/token).
            # A large gap between expected and captured chars is a signal that
            # truncation occurred (e.g. only the final short summary was captured
            # instead of the full multi-turn output).  See issue #210.
            expected_chars = total_tokens * 4 if total_tokens else None
            if expected_chars and len(output) < expected_chars * 0.5:
                logger.warning(
                    "Session %s: captured output (%d chars) is less than 50%% of "
                    "token-estimated size (~%d chars from %d tokens × 4). "
                    "Possible truncation — check sessions_history limit and whether "
                    "the agent wrote output to files instead of returning text.",
                    session_key,
                    len(output),
                    expected_chars,
                    total_tokens,
                )
            logger.info(
                "Session %s completed: %d chars captured, %d tokens consumed"
                "%s",
                session_key,
                len(output),
                total_tokens,
                f" (~{expected_chars} chars expected)" if expected_chars else "",
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
                self._active_session_key = None
                raise err

            # Session completed successfully — clear the tracked session key
            # so cancel_active_session() becomes a no-op (Issue #488).
            self._active_session_key = None
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
