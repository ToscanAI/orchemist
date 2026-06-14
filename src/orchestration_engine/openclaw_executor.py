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
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import (
    GatewayHTTPError,
    RateLimitError,
    SpawnNoPromptDelivered,
    SpawnTransportTimeout,
    classify_http_error,
)
from .executors._common import _PRICING, BaseExecutor
from .model_fallback import ModelFallbackChain
from .model_registry import prefixed_id
from .recovery import (
    CircuitBreakerState,
    ErrorType,
    ExecutorRetryConfig,
    classify_exception_error_type,
)
from .schemas import ModelTier, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from .timestamps import now_utc

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

# Token pricing is the single shared ``_PRICING`` instance from
# ``executors._common`` (#927; imported above) — used by estimate_cost so
# estimates agree with the ledger by construction.

# Model tier → OpenClaw (anthropic/-prefixed) model ID mapping, built from the
# canonical model_registry (#916). SHORT string keys and ModelTier enum keys
# resolve to the same canonical prefixed id; the OPUS tier emits
# anthropic/claude-opus-4-8.
MODEL_MAP: Dict[str, str] = {
    "haiku": prefixed_id("haiku"),
    "sonnet": prefixed_id("sonnet"),
    "opus": prefixed_id("opus"),
    # ModelTier enum fallbacks
    **{tier: prefixed_id(tier) for tier in ModelTier},
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


def _is_transport_timeout(reason: Any) -> bool:
    """Return True when *reason* (a ``urllib.error.URLError.reason``) is a
    socket-level timeout.

    A urllib socket timeout surfaces either as a bare ``TimeoutError`` /
    ``socket.timeout`` or, on some paths, wrapped as
    ``urllib.error.URLError`` whose ``.reason`` is one of those. This helper
    recognises the wrapped form so the transport-timeout seam in
    :meth:`OpenClawExecutor._http_post` / :meth:`_http_get` can convert it to
    :class:`~.errors.SpawnTransportTimeout` (issue #732).
    """
    return isinstance(reason, (TimeoutError, socket.timeout))


# Instruction appended to every sub-agent prompt so it returns its full output
# as text instead of writing it to workspace files.  The orchestrator reads the
# final assistant message text — anything written to files is invisible to it.
OUTPUT_CAPTURE_INSTRUCTION = (
    "\n\n---\n"
    "ORCHESTRATOR INSTRUCTION (do not remove):\n"
    "You are running as a sub-agent inside an orchestration pipeline.\n"
    "Write your COMPLETE output to the file path specified in the task above.\n"
    "After writing the file, also return a brief summary (1-2 sentences) as\n"
    "your final message so the orchestrator can confirm completion.\n"
    "Your file output is what gets passed to the next pipeline phase.\n"
    "For verdict-producing phases (review, adversary): end the output file\n"
    "with a VERDICT: / COMMENT: block so the orchestrator can route correctly.\n"
    "---"
)


class OpenClawExecutor(BaseExecutor):
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

    provider_name = "openclaw"  # per-phase provider identity (#969)

    def __init__(
        self,
        gateway_url: Optional[str] = None,
        gateway_token: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
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
            else os.environ.get("OPENCLAW_GATEWAY_TOKEN") or self._read_token_from_config() or ""
        )
        # Timeout resolution chain (#753): explicit positive arg → openclaw.json
        # subagents.runTimeoutSeconds → DEFAULT_TIMEOUT_SECONDS (1200s).
        # An explicit zero/None falls through to the config value so callers that
        # pass `timeout_seconds=None` (e.g. older test fixtures) get the configured
        # subagent timeout rather than silently falling through to the hard-coded
        # default.
        if timeout_seconds:
            self.timeout_seconds = timeout_seconds
        else:
            config_timeout = self._read_subagent_timeout_from_config()
            self.timeout_seconds = config_timeout if config_timeout else DEFAULT_TIMEOUT_SECONDS
        self.dry_run = dry_run

        # ── Graceful shutdown support (Issue #488) ───────────────────────────
        # Tracks the session key of the currently running sub-agent session.
        # Set immediately after spawn; cleared on completion or error.
        self._active_session_key: Optional[str] = None

        # ── Per-spawn HTTP socket timeout override (issue #732) ──────────────
        # Set transiently by _run_session around the sessions_spawn call so the
        # per-retry 30→60→120 socket-timeout ladder applies ONLY to the spawn
        # HTTP request (polling keeps the default 30s). None → default 30s.
        # Threaded via an attribute (not a _http_post/_invoke_tool parameter) so
        # that test doubles patching _http_post with a (url, body) signature
        # continue to work unchanged.
        self._spawn_socket_timeout: Optional[float] = None

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

    @staticmethod
    def _read_subagent_timeout_from_config() -> Optional[int]:
        """Try to read subagent run timeout from ~/.openclaw/openclaw.json (#753).

        Returns the value of ``subagents.runTimeoutSeconds`` as a positive int,
        or ``None`` when the key is absent, malformed, non-numeric, or
        non-positive. Mirrors the byte-shape of :meth:`_read_token_from_config`:
        same path, same try/except, same DEBUG-level read-failure log.
        """
        try:
            config_path = os.path.expanduser("~/.openclaw/openclaw.json")
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config = json.load(f)
                raw = config.get("subagents", {}).get("runTimeoutSeconds")
                if raw is None:
                    return None
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    logger.debug(
                        "subagents.runTimeoutSeconds in openclaw.json is not an int: %r",
                        raw,
                    )
                    return None
                if value <= 0:
                    logger.debug(
                        "subagents.runTimeoutSeconds in openclaw.json is non-positive (%d)",
                        value,
                    )
                    return None
                logger.debug(
                    "Auto-discovered subagent run timeout from %s: %ds",
                    config_path,
                    value,
                )
                return value
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.debug("Could not read subagents.runTimeoutSeconds from openclaw.json: %s", exc)
        return None

    def _check_git_committed_output(
        self,
        session_key: str,
        output_dir: Optional[str],
        output_artifact: Optional[str],
    ) -> Optional[Tuple[str, int]]:
        """Git-output success-detection fallback for gateway-GC events (#735 RC-2).

        When the gateway garbage-collects a session whose agent has already
        committed its expected output file to git, the executor can recover by
        reading the committed file directly rather than raising RuntimeError.

        Consulted ONLY when BOTH ``output_dir`` and ``output_artifact`` are
        non-empty (per behavioural contract A3.2 + adversary F1 — the
        explicit-artefact contract avoids false-positive recovery on unrelated
        commits).

        Returns ``(output_text, tokens_consumed=0)`` on success or ``None`` to
        signal the caller it must raise the original RuntimeError. Bounded by
        the ``GC_OUTPUT_GRACE_SECONDS`` env var (default 60s).
        """
        if not output_dir or not output_artifact:
            return None

        try:
            output_dir_path = Path(output_dir)
            if not (output_dir_path / ".git").exists():
                return None
            artifact_path = output_dir_path / output_artifact
            if not artifact_path.exists():
                return None

            grace_seconds = int(os.environ.get("GC_OUTPUT_GRACE_SECONDS", "60"))
            result = subprocess.run(
                [
                    "git",
                    "log",
                    f"--since={grace_seconds} seconds ago",
                    "--name-only",
                    "--pretty=format:",
                    "-n",
                    "1",
                ],
                cwd=str(output_dir_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.debug(
                    "git log probe failed for session %s in %s: rc=%d, stderr=%s",
                    session_key,
                    output_dir,
                    result.returncode,
                    result.stderr,
                )
                return None
            if not result.stdout.strip():
                logger.debug(
                    "No recent commit (within %ds) in %s for session %s; " "GC fallback declines.",
                    grace_seconds,
                    output_dir,
                    session_key,
                )
                return None

            output_text = artifact_path.read_text(encoding="utf-8", errors="replace")
            logger.warning(
                "Session %s garbage-collected by gateway but %s/%s was committed "
                "within the last %ds — recovering via git-output fallback (#735 RC-2)",
                session_key,
                output_dir,
                output_artifact,
                grace_seconds,
            )
            return (output_text, 0)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.debug(
                "git-output fallback failed for session %s: %s",
                session_key,
                exc,
            )
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
        except Exception as exc:  # noqa: BLE001
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

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: ARG002, D102
        return True

    def estimate_cost(self, task: TaskSpec) -> float:  # noqa: D102
        # Rough cost estimate via the canonical PricingTable (#916), using a
        # representative token assumption (input ~500, output ~2000).
        tier = task.preferred_model or ModelTier.SONNET
        return _PRICING.compute_cost(prefixed_id(tier), 500, 2000)

    def execute(  # noqa: C901
        self,
        task: TaskSpec,
        worker_id: str = "openclaw-worker",  # noqa: ARG002
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
        start_time = self._capture_start_time()
        task_id = self._resolve_task_id(task)

        # ── COMMAND TASK: run locally via subprocess, skip LLM agent ────────
        if task.type == TaskType.COMMAND:
            return self._execute_command_task(task, start_time, task_id)

        # ── ACCEPTANCE_RUN TASK: run pytest locally, skip LLM agent ─────────
        if task.type == TaskType.ACCEPTANCE_RUN:
            return self._execute_acceptance_run_task(task, start_time, task_id)

        # ── 1. Resolve model / thinking ──────────────────────────────────────
        tier_key = (
            model_tier
            or (
                task.preferred_model.value
                if hasattr(task.preferred_model, "value")
                else task.preferred_model
            )
            or "sonnet"
        )
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
        # circuit_breaker_reset_seconds → minutes (CircuitBreakerState uses minutes)
        cb_reset_minutes = retry_cfg.circuit_breaker_reset_seconds // 60

        # ── 4a. Instantiate fallback chain BEFORE the CB gate (issue #480) ───
        # Per issue #480: the legacy early-return that fired when the requested
        # tier's CB was open (and returned `circuit_open` without consulting
        # alternative tiers) has been REMOVED. The outer-loop's CB pre-check
        # below now handles BOTH first-task entry and per-iteration escalation,
        # advancing the chain when the current tier's CB is open. Only when
        # EVERY tier in the chain has an open CB does the executor return a
        # FAILED result, with the new error code `all_tiers_unavailable` (per
        # adversary F4 + behavioural A1).
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
        # Snapshot of tiers skipped due to open CBs — used in the
        # all_tiers_unavailable error message when every tier is unavailable.
        _cb_skipped_tiers: List[str] = []

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
        # Set to True when the LAST attempt failed with a transport-class error
        # (HTTP socket timeout during spawn, or a spawned session that never
        # delivered a first message). Such failures must NOT escalate the model
        # — they are gateway/transport symptoms, not task-level agent failures
        # (issue #732). Reset per attempt so a later real failure clears it.
        transport_timeout_failure: bool = False
        # Set to True when the attempt failed because a spawned session never
        # delivered a first message (SpawnNoPromptDelivered). This fails the
        # task fast — retrying the SAME prompt on the SAME model is futile and
        # only risks more orphan sessions — so the inner retry loop breaks
        # immediately (issue #732, Bug B). Escalation is still suppressed via
        # transport_timeout_failure.
        no_prompt_failure: bool = False

        while True:  # outer loop over model chain (issue #347)
            # Ensure a circuit-breaker entry exists for the current model tier.
            with _CIRCUIT_BREAKERS_LOCK:
                if model not in _CIRCUIT_BREAKERS:
                    _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)

            # ── CB pre-check per outer-loop iteration ─────────────────────────
            # Per issue #480: this gate now ALSO handles first-task entry —
            # the legacy early-return at the top of execute_task has been
            # removed, so a CB-open tier here is advanced via chain.advance()
            # rather than triggering an immediate failure.
            with _CIRCUIT_BREAKERS_LOCK:
                cb_cur = _CIRCUIT_BREAKERS[model]
            if cb_cur.is_open(retry_cfg.circuit_breaker_threshold, cb_reset_minutes):
                # Snapshot the skipped tier for the all_tiers_unavailable error.
                _cb_skipped_tiers.append(f"{chain.current()}({model})")
                if chain.has_next():
                    prev = chain.current()
                    nxt = chain.advance()
                    model = MODEL_MAP.get(nxt, MODEL_MAP["sonnet"])
                    logger.warning(
                        "Task %s: circuit breaker open for model '%s' (tier '%s') — "
                        "skipping and escalating to tier '%s'",
                        task_id,
                        MODEL_MAP.get(prev, prev),
                        prev,
                        nxt,
                    )
                    continue
                else:
                    # All tiers circuit-broken — exit with failure.  Set
                    # last_error_* so the post-loop FAILED result carries the
                    # all_tiers_unavailable code (issue #480 / adversary F4).
                    last_error_code = "all_tiers_unavailable"
                    last_error_msg = (
                        f"All model tiers have open circuit breakers; "
                        f"none was eligible for task {task_id}. "
                        f"Probed tiers: {', '.join(_cb_skipped_tiers)}. "
                        f"Cooldown: {retry_cfg.circuit_breaker_reset_seconds}s."
                    )
                    break

            # ── 4b-inner. Per-model retry loop (unchanged from #346) ─────────
            for attempt in range(retry_cfg.max_attempts):
                if attempt > 0:
                    # ── Orphan WARNING + best-effort cancel on retry (#732) ──
                    # When the PREVIOUS attempt failed with a transport-class
                    # error (spawn socket timeout, or a spawned-but-promptless
                    # session), the gateway may have left an orphan session whose
                    # key we either captured (spawn-succeeded-then-grace-failed)
                    # or never received (spawn timed out). Emit a WARNING naming
                    # the previous attempt and the potential orphan key, and
                    # best-effort cancel it via the existing sessions_stop path.
                    if transport_timeout_failure:
                        orphan_key = self._active_session_key
                        logger.warning(
                            "Task %s: retry %d/%d after spawn transport failure — "
                            "previous attempt may have left an orphan session "
                            "(key=%s). Attempting best-effort cancel.",
                            task_id,
                            attempt,
                            retry_cfg.max_attempts - 1,
                            orphan_key or "unknown",
                        )
                        if orphan_key:
                            try:
                                self._invoke_tool("sessions_stop", {"sessionKey": orphan_key})
                                logger.info(
                                    "Best-effort sessions_stop succeeded for " "orphan session %s",
                                    orphan_key,
                                )
                            except Exception as stop_exc:  # noqa: BLE001
                                # Tolerate "unsupported"/any failure — non-fatal.
                                logger.warning(
                                    "sessions_stop not supported or failed for "
                                    "orphan session %s (non-fatal): %s",
                                    orphan_key,
                                    stop_exc,
                                )
                            finally:
                                # Abandon the orphan key — we will not adopt it.
                                self._active_session_key = None

                    # Exponential backoff: backoff_base * backoff_multiplier^(retry_index)
                    # where retry_index = attempt - 1 (0-based retry counter).
                    retry_index = attempt - 1
                    wait_seconds = min(
                        retry_cfg.backoff_base * (retry_cfg.backoff_multiplier**retry_index),
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

                # Reset the per-attempt transport-failure flags so they reflect
                # ONLY this attempt's outcome (a later real failure must clear
                # them and resume normal escalation semantics — issue #732).
                transport_timeout_failure = False
                no_prompt_failure = False

                # ── Per-attempt escalating spawn socket timeout (#732) ───────
                # 30 → 60 → 120 (capped) with default config, so the spawn HTTP
                # call gets a longer socket budget on each retry under degraded
                # API conditions. Polling keeps its own fixed default timeout.
                socket_timeout = min(
                    retry_cfg.socket_timeout_initial
                    * (retry_cfg.socket_timeout_multiplier**attempt),
                    retry_cfg.socket_timeout_max,
                )

                try:
                    # Thread #735 RC-2 kwargs through from the task payload.
                    # When both are provided AND a gateway-GC event happens
                    # after the agent committed its output, _run_session
                    # recovers via _check_git_committed_output rather than
                    # raising RuntimeError.
                    output_text, tokens_consumed = self._run_session(
                        prompt,
                        model,
                        thinking,
                        timeout=effective_timeout,
                        output_dir=task.payload.get("output_dir"),
                        output_artifact=task.payload.get("output_artifact"),
                        socket_timeout=socket_timeout,
                        startup_grace_seconds=retry_cfg.spawn_startup_grace_seconds,
                    )
                    # ── Success path ─────────────────────────────────────────
                    with _CIRCUIT_BREAKERS_LOCK:
                        _CIRCUIT_BREAKERS[model].record_success()
                    succeeded = True
                    break

                except Exception as exc:  # noqa: BLE001
                    last_exc = exc  # noqa: F841 — retains last retry exception (intentional)
                    error_type = classify_exception_error_type(exc)

                    # Record failure in the shared circuit breaker — EXCEPT for
                    # transport-class errors (HTTP socket timeout during spawn,
                    # or a spawned-but-promptless session). Those are gateway /
                    # transport symptoms, not task-level agent failures, so they
                    # must NOT open the circuit breaker (issue #732, Bug A).
                    if error_type != ErrorType.TRANSPORT_TIMEOUT:
                        with _CIRCUIT_BREAKERS_LOCK:
                            _CIRCUIT_BREAKERS[model].record_failure(
                                retry_cfg.circuit_breaker_threshold
                            )

                    # Preserve partial output if the sub-agent produced any before failing.
                    partial_output = getattr(exc, "partial_output", "") or ""
                    partial_tokens = getattr(exc, "partial_tokens", 0) or 0

                    # Determine error code and emit a log at the appropriate level.
                    # NOTE: SpawnTransportTimeout subclasses TimeoutError, so its
                    # branch MUST precede the generic TimeoutError branch below,
                    # else a transport timeout would be mislabelled "timeout".
                    if isinstance(exc, SpawnTransportTimeout):
                        last_error_code = "spawn_transport_timeout"
                        last_error_msg = str(exc)
                        transport_timeout_failure = True
                        logger.warning(
                            "Spawn transport timeout for task %s (attempt %d) — "
                            "not counting against the circuit breaker; will retry "
                            "with a longer socket timeout: %s",
                            task_id,
                            attempt + 1,
                            exc,
                        )
                    elif isinstance(exc, SpawnNoPromptDelivered):
                        last_error_code = "spawn_no_prompt_delivered"
                        last_error_msg = str(exc)
                        transport_timeout_failure = True
                        no_prompt_failure = True
                        logger.warning(
                            "Spawned session for task %s (attempt %d) delivered no "
                            "first message within the startup grace period — not "
                            "counting against the circuit breaker; will not retry "
                            "or escalate the model: %s",
                            task_id,
                            attempt + 1,
                            exc,
                        )
                    elif isinstance(exc, TimeoutError):
                        last_error_code = "timeout"
                        last_error_msg = str(exc)
                        logger.error(
                            "OpenClaw session timed out for task %s (attempt %d): %s",
                            task_id,
                            attempt + 1,
                            exc,
                        )
                    elif isinstance(exc, RateLimitError):
                        last_error_code = "rate_limited"
                        last_error_msg = str(exc)
                        retry_hint = (
                            f" retry after {exc.retry_after}s"
                            if exc.retry_after is not None
                            else " (no retry-after header)"
                        )
                        logger.warning(
                            "Rate limited (429) —%s for task %s (attempt %d)",
                            retry_hint,
                            task_id,
                            attempt + 1,
                        )
                    else:
                        last_error_code = "execution_error"
                        last_error_msg = str(exc)
                        logger.error(
                            "OpenClaw execution failed for task %s (attempt %d): %s",
                            task_id,
                            attempt + 1,
                            exc,
                        )

                    # Permanent errors should not be retried or escalated.
                    if error_type == ErrorType.PERMANENT:
                        logger.info(
                            "Task %s: not retrying after PERMANENT error (%s): %s",
                            task_id,
                            type(exc).__name__,
                            exc,
                        )
                        permanent_failure = True
                        break

                    # A promptless-spawn failure fails the task fast — retrying
                    # the same prompt on the same model is futile (#732, Bug B).
                    if no_prompt_failure:
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
                            model,
                            task_id,
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

            if transport_timeout_failure:
                # Issue #732 (Bug A): a tier whose retries were exhausted purely
                # by transport-class failures (spawn socket timeouts / promptless
                # spawns) must NOT escalate to another model — degraded API
                # transport is not a model-quality problem, and escalating risks
                # repeating a gateway delivery fault. Fall through to the failure
                # path carrying last_error_code (spawn_transport_timeout or
                # spawn_no_prompt_delivered). A MIX of transport + a real task
                # failure clears this flag (per-attempt reset), so normal
                # escalation resumes when the last failure was task-level.
                break

            if chain.has_next():
                # All retries exhausted on the current tier — escalate to next.
                prev_tier = chain.current()
                next_tier = chain.advance()
                new_model = MODEL_MAP.get(next_tier, MODEL_MAP["sonnet"])
                logger.warning(
                    "Task %s: all retries exhausted on model '%s' (tier '%s') — "
                    "escalating to tier '%s' (model '%s')",
                    task_id,
                    model,
                    prev_tier,
                    next_tier,
                    new_model,
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
                        model,
                        next_tier,
                        task_id,
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
            elapsed = (now_utc() - start_time).total_seconds()
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
                completed_at=now_utc(),
                model_used=model,
                execution_time_seconds=elapsed,
                tokens_consumed=partial_tokens,
            )

        # ── 4d. Post-success checks ──────────────────────────────────────────
        elapsed = (now_utc() - start_time).total_seconds()

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
                completed_at=now_utc(),
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
            completed_at=now_utc(),
            model_used=model,
            tokens_consumed=tokens_consumed,
            execution_time_seconds=elapsed,
            cost_usd=None,
        )

    # ------------------------------------------------------------------
    # Command task handling
    # ------------------------------------------------------------------

    def _execute_command_task(  # noqa: C901
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
                task_id,
                task,
                start_time,
                "command_missing",
                "No 'command' specified in task payload",
            )

        try:
            cmd_parts = shlex.split(raw_command)
        except ValueError as exc:
            return self._command_error(
                task_id,
                task,
                start_time,
                "command_parse_error",
                f"shlex.split failed: {exc}",
            )

        if not cmd_parts:
            return self._command_error(
                task_id,
                task,
                start_time,
                "command_empty",
                "Command is empty after parsing",
            )

        executable = cmd_parts[0]

        # Whitelist check — only enforce when a non-empty list is provided
        if allowed_commands:
            if executable not in allowed_commands:
                return self._command_error(
                    task_id,
                    task,
                    start_time,
                    "command_not_allowed",
                    f"Command '{executable}' is not in allowed_commands: {allowed_commands}",
                )

        logger.info(
            "OpenClawExecutor: COMMAND task=%s, executable=%s, cwd=%s",
            task_id,
            executable,
            working_dir or "<inherit>",
        )

        # ── Dry-run shortcut ──────────────────────────────────────────
        if self.dry_run:
            mock_output = (
                f"[dry-run] Would execute: {raw_command}\n"
                f"[dry-run] working_dir={working_dir}, allowed={allowed_commands}"
            )
            elapsed = (now_utc() - start_time).total_seconds()
            self._write_command_output(output_dir, task_id, raw_command, mock_output, 0)
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS,
                confidence=1.0,
                result={"output": mock_output, "dry_run": True, "command": raw_command},
                errors=[],
                started_at=start_time,
                completed_at=now_utc(),
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
                task_id,
                task,
                start_time,
                "timeout",
                f"Command timed out after {timeout_sec}s",
            )
        except FileNotFoundError:
            return self._command_error(
                task_id,
                task,
                start_time,
                "executable_not_found",
                f"Executable not found: {executable}",
            )
        except Exception as exc:  # noqa: BLE001
            return self._command_error(
                task_id,
                task,
                start_time,
                "execution_error",
                str(exc),
            )

        combined_output = (proc.stdout or "") + (proc.stderr or "")
        elapsed = (now_utc() - start_time).total_seconds()

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
                completed_at=now_utc(),
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
                completed_at=now_utc(),
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
        from . import (  # noqa: PLC0415 — lazy: avoids circular dep at module load
            test_runner,
        )

        output_dir: str = task.payload.get("output_dir", "")
        timeout_sec: int = (
            task.timeout_seconds
            if hasattr(task, "timeout_seconds") and task.timeout_seconds
            else 300
        )

        if not output_dir:
            return self._acceptance_run_error(
                task_id,
                task,
                start_time,
                "missing_output_dir",
                "No 'output_dir' in task payload for acceptance_run phase",
            )

        # ── Opt-in acceptance MATRIX branch (#985) ─────────────────────────
        # MUST precede the acceptance_tests.py existence guard below: matrix
        # entries are arbitrary allowlisted commands (one may itself be a
        # ``python3 -m pytest …``), so the matrix path does NOT require an
        # acceptance_tests.py file. An empty/absent matrix falls through to the
        # UNCHANGED legacy single-pytest path (byte-identical results file).
        matrix = task.payload.get("acceptance_matrix") or []
        if matrix:
            return self._execute_acceptance_matrix(
                task, start_time, task_id, output_dir, matrix, timeout_sec
            )

        test_file = os.path.join(output_dir, "acceptance_tests.py")

        if not os.path.exists(test_file):
            return self._acceptance_run_error(
                task_id,
                task,
                start_time,
                "test_file_not_found",
                f"acceptance_tests.py not found at: {test_file}",
            )

        # ── Dry-run shortcut ──────────────────────────────────────────
        if self.dry_run:
            mock_result = test_runner.TestRunResult(
                passed=3,
                failed=0,
                errors=0,
                total=3,
                pass_rate=1.0,
                failure_details="",
                full_output="[dry-run]",
                exit_code=0,
            )
            test_runner.write_acceptance_results(mock_result, output_dir)
            elapsed = (now_utc() - start_time).total_seconds()
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS,
                confidence=1.0,
                result={"text": "[dry-run] acceptance_run: 3 passed, 0 failed"},
                errors=[],
                started_at=start_time,
                completed_at=now_utc(),
                model_used="local-subprocess",
                execution_time_seconds=elapsed,
            )

        # ── Execute pytest ────────────────────────────────────────────
        try:
            result = test_runner.run_pytest(test_file, timeout_seconds=timeout_sec)
        except Exception as exc:  # noqa: BLE001
            return self._acceptance_run_error(
                task_id, task, start_time, "execution_error", str(exc)
            )

        test_runner.write_acceptance_results(result, output_dir)

        elapsed = (now_utc() - start_time).total_seconds()
        all_passed = result.failed == 0 and result.errors == 0 and result.total > 0
        state = TaskState.SUCCESS if all_passed else TaskState.FAILED

        summary = test_runner.format_failure_summary(result)

        logger.info(
            "OpenClawExecutor: ACCEPTANCE_RUN task=%s state=%s "
            "passed=%d failed=%d errors=%d total=%d",
            task_id,
            state.value,
            result.passed,
            result.failed,
            result.errors,
            result.total,
        )

        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=state,
            confidence=result.pass_rate,
            result={"text": summary},
            errors=[],
            started_at=start_time,
            completed_at=now_utc(),
            model_used="local-subprocess",
            execution_time_seconds=elapsed,
        )

    def _execute_acceptance_matrix(
        self,
        task: TaskSpec,
        start_time: datetime,
        task_id: str,
        output_dir: str,
        matrix: List[Dict[str, str]],
        timeout_sec: int,
    ) -> TaskResult:
        """Run an opt-in acceptance MATRIX (#985) — run-all-then-report.

        Every ``{"name", "command"}`` entry is dispatched IN ORDER through the
        shared ``command_executor`` security model (allowlist + dangerous-pattern
        denylist + MAX_OUTPUT_BYTES truncation + timeout, ``shell=False``) by
        building a per-entry ``TaskSpec(type=COMMAND, …)`` and calling
        ``CommandExecutor.execute``. The loop NEVER fails fast: a failing,
        timed-out, or security-blocked entry reddens the aggregate but every
        other entry still runs and is captured.

        Aggregate (top-level results fields): ``passed`` = entries with
        ``exit_code == 0``; ``failed`` = entries with ``exit_code != 0`` (a
        non-zero exit, a timeout, AND a security block all count as failed);
        ``errors`` = 0; ``total`` = ``len(matrix)``; ``pass_rate`` =
        ``passed / total``; ``status`` = ``"pass"`` iff every entry passed; the
        aggregate ``exit_code`` = 0 iff all passed else 1.

        The per-entry breakdown is persisted additively under a ``"matrix"`` key
        in ``acceptance_results.json``; the 10 legacy top-level keys are retained
        for the downstream ``confidence.py`` reader. Note: each entry stores its
        already-truncated output (≤ MAX_OUTPUT_BYTES + marker), so an N-entry
        matrix can produce an ~N × 1 MB results file (acceptable for the small
        matrices this feature targets — typically a handful of entries, only
        failing ones emitting large output).

        Args:
            task:        The ACCEPTANCE_RUN TaskSpec (its payload supplies the
                         shared ``allowed_commands`` allowlist and ``working_dir``).
            start_time:  Datetime when the task started.
            task_id:     Stable task identifier string.
            output_dir:  Directory to write ``acceptance_results.json`` into.
            matrix:      Ordered list of ``{"name", "command"}`` entries (already
                         interpolated by ``_build_command_extras``).
            timeout_sec: Per-entry timeout ceiling (the phase timeout).

        Returns:
            TaskResult with ``state=SUCCESS`` iff every entry passed,
            ``confidence`` = aggregate pass_rate, and a short matrix summary in
            ``result['text']`` (for downstream prompt feedback).
        """
        from . import (  # noqa: PLC0415 — lazy: avoids circular dep at module load
            test_runner,
        )
        from .command_executor import (  # noqa: PLC0415 — lazy: import-cycle safety
            CommandExecutor,
        )

        # The phase declares ONE shared allowlist for all entries. We pass
        # ``allowed_commands`` through verbatim — ``or []`` deliberately
        # SUPPRESSES CommandExecutor's DEFAULT_ALLOWED_COMMANDS fallback so a
        # matrix entry must be EXPLICITLY allowlisted by the phase (intentional
        # fail-closed behaviour for the CI-equivalent matrix, #985).
        allowed_commands: List[str] = task.payload.get("allowed_commands") or []
        working_dir: Optional[str] = task.payload.get("working_dir")

        executor = CommandExecutor(default_timeout=timeout_sec)

        entries: List[Dict[str, Any]] = []
        passed = 0
        failed = 0
        for item in matrix:
            name = item.get("name", "")
            command = item.get("command", "")

            entry_spec = TaskSpec(
                type=TaskType.COMMAND,
                payload={
                    "command": command,
                    "allowed_commands": allowed_commands,
                    "cwd": working_dir,
                },
            )
            entry_result = executor.execute(entry_spec)

            exit_code = int(entry_result.result.get("exit_code", -1))
            output = entry_result.result.get("text", "")
            entry_passed = entry_result.state == TaskState.SUCCESS
            if entry_passed:
                passed += 1
            else:
                failed += 1

            entry_record: Dict[str, Any] = {
                "name": name,
                "command": command,
                "status": "pass" if entry_passed else "fail",
                "exit_code": exit_code,
                "output": output,
            }
            if entry_result.started_at and entry_result.completed_at:
                entry_record["duration"] = (
                    entry_result.completed_at - entry_result.started_at
                ).total_seconds()
            entries.append(entry_record)

        total = len(matrix)
        all_passed = failed == 0
        pass_rate = (passed / total) if total > 0 else 0.0

        # Build the aggregate TestRunResult so the existing writer maps the 10
        # legacy top-level keys (status="pass" iff failed==0 and errors==0;
        # exit_code is the AGGREGATE 0/1, not any single entry's code).
        failure_details = "\n".join(f"{e['name']}: exit {e['exit_code']}" for e in entries)
        agg_result = test_runner.TestRunResult(
            passed=passed,
            failed=failed,
            errors=0,
            total=total,
            pass_rate=pass_rate,
            failure_details=failure_details,
            full_output=failure_details,
            exit_code=0 if all_passed else 1,
        )
        test_runner.write_acceptance_results(agg_result, output_dir, entries=entries)

        elapsed = (now_utc() - start_time).total_seconds()
        state = TaskState.SUCCESS if all_passed else TaskState.FAILED

        summary_lines = [f"{passed}/{total} matrix entries passed"]
        summary_lines += [f"- {e['name']}: {e['status']}" for e in entries]
        summary = "\n".join(summary_lines)

        logger.info(
            "OpenClawExecutor: ACCEPTANCE_RUN matrix task=%s state=%s "
            "passed=%d failed=%d total=%d",
            task_id,
            state.value,
            passed,
            failed,
            total,
        )

        return TaskResult(
            task_id=task_id,
            task_type=task.type,
            state=state,
            confidence=pass_rate,
            result={"text": summary},
            errors=[],
            started_at=start_time,
            completed_at=now_utc(),
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
            completed_at=now_utc(),
            model_used="local-subprocess",
            execution_time_seconds=(now_utc() - start_time).total_seconds(),
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
            completed_at=now_utc(),
            model_used="local-subprocess",
            execution_time_seconds=(now_utc() - start_time).total_seconds(),
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

    def _resolve_http_timeout(self, timeout: Optional[float]) -> float:
        """Resolve the effective HTTP socket timeout (seconds) for a request.

        Precedence (issue #732):
          1. An explicit ``timeout`` argument, when provided.
          2. :attr:`_spawn_socket_timeout` — set transiently by
             :meth:`_run_session` around the ``sessions_spawn`` call so the
             per-retry 30→60→120 ladder lengthens ONLY the spawn socket budget.
          3. The historical default of 30s (used by polling / ``sessions_list``
             / ``sessions_stop`` and the first spawn attempt).
        """
        if timeout is not None:
            return float(timeout)
        override = getattr(self, "_spawn_socket_timeout", None)
        if override is not None:
            return float(override)
        return 30.0

    def _http_post(
        self, url: str, body: Dict[str, Any], timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """POST JSON *body* to *url* and return the decoded response dict.

        Args:
            url:     The endpoint to POST to.
            body:    The JSON-serialisable request body.
            timeout: HTTP socket timeout in seconds. ``None`` (the default)
                     resolves via :meth:`_resolve_http_timeout` — the spawn
                     ladder (when active) or the historical 30s. ``None`` is the
                     default so that test doubles patching this method with a
                     ``(url, body)`` signature keep working (issue #732).

        Raises:
            SpawnTransportTimeout: when the socket times out *before* any HTTP
                response arrives (distinguished from a 4xx/5xx response and from
                the task-deadline ``TimeoutError`` so the circuit breaker is not
                tripped — issue #732, Bug A).
            GatewayHTTPError: when a 4xx/5xx response *is* received.
        """
        effective_timeout = self._resolve_http_timeout(timeout)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers=self._build_headers(),
            method="POST",
        )
        # HTTPError (a response DID arrive) must be caught FIRST — it is a
        # subclass of URLError. Only a genuine socket timeout (no response) is
        # converted to SpawnTransportTimeout.
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise classify_http_error(exc.code, error_body, exc.headers) from exc
        except urllib.error.URLError as exc:
            # urllib may wrap the socket timeout as URLError(reason=timeout).
            if _is_transport_timeout(exc.reason):
                raise SpawnTransportTimeout(str(exc)) from exc
            raise
        except (TimeoutError, socket.timeout) as exc:
            # Bare socket timeout (Py3.12: socket.timeout IS TimeoutError).
            raise SpawnTransportTimeout(str(exc)) from exc

    def _http_get(self, url: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """GET *url* and return the decoded response dict.

        Mirrors :meth:`_http_post` — including ``timeout`` resolution and the
        socket-timeout → :class:`SpawnTransportTimeout` seam re-raise (#732) —
        for symmetry/correctness.
        """
        effective_timeout = self._resolve_http_timeout(timeout)
        req = urllib.request.Request(url, headers=self._build_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise classify_http_error(exc.code, error_body, exc.headers) from exc
        except urllib.error.URLError as exc:
            if _is_transport_timeout(exc.reason):
                raise SpawnTransportTimeout(str(exc)) from exc
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise SpawnTransportTimeout(str(exc)) from exc

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
            from .db import Database  # noqa: PLC0415

            # Use injected _db if set (e.g., for testing), otherwise create a new instance.
            db = getattr(self, "_db", None) or Database()
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
                            "message": (f"No token progress for {stall_seconds:.0f}s"),
                        },
                    )
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not emit stall event: %s", exc)

    def _invoke_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an OpenClaw tool via the gateway's /tools/invoke endpoint.

        Returns the parsed response dict. Raises RuntimeError on failure.

        The HTTP socket timeout is taken from :attr:`_spawn_socket_timeout` when
        set (the spawn path threads the per-retry 30→60→120 ladder via that
        attribute, see :meth:`_run_session`); otherwise :meth:`_http_post`
        applies its default 30s. The timeout is intentionally NOT a parameter of
        this method or of ``_http_post``'s call here, so that test doubles which
        patch ``_http_post`` with a ``(url, body)`` signature keep working
        unchanged (issue #732).
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

    def _run_session(  # noqa: C901
        self,
        prompt: str,
        model: str,
        thinking: Optional[str],
        timeout: Optional[int] = None,
        output_dir: Optional[str] = None,
        output_artifact: Optional[str] = None,
        socket_timeout: Optional[float] = None,
        startup_grace_seconds: Optional[float] = None,
    ) -> Tuple[str, int]:
        """Spawn a sub-agent session and poll until completion.

        Uses the gateway's ``POST /tools/invoke`` endpoint to call
        ``sessions_spawn`` and ``sessions_history``.

        Args:
            prompt:          The prompt text to execute.
            model:           Full model string (e.g. "anthropic/claude-sonnet-4-6").
            thinking:        Thinking level string or None.
            timeout:         Timeout in seconds (overrides self.timeout_seconds).
            socket_timeout:  (#732) Optional HTTP socket timeout (seconds) applied
                             ONLY to the ``sessions_spawn`` call, so that the
                             per-retry 30→60→120 ladder lengthens the spawn
                             socket budget under degraded API conditions. The
                             ``sessions_history`` polling keeps its own fixed
                             (default 30s) timeout. ``None`` → spawn uses the
                             default 30s.
            startup_grace_seconds: (#732, Bug B) Optional grace window (seconds)
                             within which the spawned session must produce its
                             first message; otherwise the session fails fast with
                             :class:`~.errors.SpawnNoPromptDelivered`. ``None`` →
                             default of 60s.
            output_dir:      (#735 RC-2) Optional filesystem path to the agent's
                             working directory. When provided alongside
                             ``output_artifact``, enables git-output recovery
                             on gateway-GC events.
            output_artifact: (#735 RC-2) Optional expected output file name
                             (relative to ``output_dir``). When both kwargs
                             are present and a recent commit of this file
                             exists in the output_dir's git history, a GC
                             event is treated as success rather than failure.

        Returns:
            ``(output_text, tokens_consumed)`` from the completed session.

        Raises:
            TimeoutError: If the session does not complete within timeout.
            SpawnTransportTimeout: If the ``sessions_spawn`` HTTP call times out
                at the socket layer before any response arrives (#732, Bug A).
            SpawnNoPromptDelivered: If the spawn succeeds but no first message
                appears within ``startup_grace_seconds`` (#732, Bug B).
            RuntimeError: On HTTP errors or unexpected responses.
        """
        # Resolve the startup-grace window (#732, Bug B). Default 60s.
        effective_startup_grace: float = (
            float(startup_grace_seconds) if startup_grace_seconds is not None else 60.0
        )
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
        # Thread the per-retry escalating socket timeout into the spawn call
        # ONLY (#732). The override is read by _http_post and reset immediately
        # after, so the subsequent sessions_history polling keeps the default
        # 30s socket timeout.
        self._spawn_socket_timeout = socket_timeout
        try:
            spawn_result = self._invoke_tool("sessions_spawn", spawn_args)
        finally:
            self._spawn_socket_timeout = None

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

        # ── Startup-grace deadline (#732, Bug B) ─────────────────────────
        # Derived from loop_start (NO extra time.monotonic() call, preserving
        # existing tests' monotonic sequences). While the session has produced
        # NO message yet, exceeding this deadline fails the task fast with
        # SpawnNoPromptDelivered rather than polling until the full task
        # timeout. Once a first message is seen (had_messages), this no longer
        # applies and the normal task-deadline / 80%-warning logic governs.
        startup_grace_deadline: float = loop_start + effective_startup_grace

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
        # Track token progress to detect stalls (model may be thinking, initializing, or provider may be degraded).  # noqa: E501
        _last_token_count: int = 0
        _last_token_change_time: float = loop_start
        _stall_warned: bool = False
        _STALL_THRESHOLD_SECONDS: float = 60.0  # configurable threshold  # noqa: N806

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
                raise RuntimeError(f"Session {session_key} polling interrupted by shutdown request")

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
                hist_result = self._invoke_tool(
                    "sessions_history",
                    {
                        "sessionKey": session_key,
                        "limit": SESSIONS_HISTORY_LIMIT,
                    },
                )
            except (RuntimeError, GatewayHTTPError, TimeoutError) as exc:
                # Session may not be ready yet; includes transient HTTP errors.
                # TimeoutError covers SpawnTransportTimeout (#732): now that
                # _http_post wraps socket timeouts as SpawnTransportTimeout, a
                # transient socket timeout during polling must keep polling
                # rather than aborting the session (edge-case contract C12).
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
                    # #735 RC-2: when output_dir + output_artifact are explicit
                    # AND the agent committed its output before the GC fired,
                    # we can recover by reading the committed file directly.
                    # The success is recorded against the SPAWN-MODEL by the
                    # caller (see execute()'s post-_run_session record_success
                    # path) — git-commit metadata is opaque to the executor.
                    recovered = self._check_git_committed_output(
                        session_key, output_dir, output_artifact
                    )
                    if recovered is not None:
                        return recovered
                    raise RuntimeError(
                        f"Session {session_key} was garbage-collected: "
                        f"history previously contained messages but is now empty. "
                        f"The gateway may have evicted the session."
                    )
                # ── Startup-grace first-message check (#732, Bug B) ──────────
                # The session was spawned (key returned) but this poll still
                # shows NO message. If the startup-grace window has elapsed,
                # fail fast: the gateway accepted the spawn but never delivered
                # the prompt (the observed "Opus idle 24 min" symptom) — rather
                # than polling uselessly until the full task timeout. Checked
                # here (in the empty-history branch, after an actual poll) so a
                # session whose FIRST poll already carries a message is never
                # mislabelled, and so a genuine task-deadline timeout (which can
                # only occur once work has started) is classified as a timeout,
                # not a no-prompt failure (contract C13).
                if now >= startup_grace_deadline:
                    logger.error(
                        "Session %s produced no first message within %.0fs "
                        "startup grace — failing fast (no prompt delivered).",
                        session_key,
                        effective_startup_grace,
                    )
                    # Best-effort cancel the promptless session we just detected
                    # so it does not linger on the gateway (also clears
                    # _active_session_key).
                    self.cancel_active_session()
                    raise SpawnNoPromptDelivered(
                        f"Session {session_key} delivered no message within "
                        f"{effective_startup_grace:.0f}s startup grace period"
                    )
                # Session not yet started — treat as "not ready", keep polling.
                continue

            # Mark that this session has produced at least one message (AC-7).
            had_messages = True

            # ── Stall detection (#413) ───────────────────────────────────
            # Extract total token count from the last assistant message's
            # usage data to detect stalls (model may be thinking, initializing, or provider may be degraded).  # noqa: E501
            _current_tokens = 0
            for _msg in reversed(messages):
                if _msg.get("role") == "assistant":
                    _usage = _msg.get("usage", {})
                    _current_tokens = _usage.get("totalTokens", 0) or _usage.get(
                        "input", 0
                    ) + _usage.get("output", 0)
                    break

            if _current_tokens > _last_token_count:
                # Progress detected — reset stall timer
                _last_token_count = _current_tokens
                _last_token_change_time = time.monotonic()
                if _stall_warned:
                    logger.info(
                        "Session %s: token progress resumed (now %d tokens)",
                        session_key,
                        _current_tokens,
                    )
                    _stall_warned = False
            else:
                # No progress — check if stalled
                stall_seconds = time.monotonic() - _last_token_change_time
                if stall_seconds >= _STALL_THRESHOLD_SECONDS and not _stall_warned:
                    logger.warning(
                        "Session %s: no token progress for %.0fs (last tokens: %d)",
                        session_key,
                        stall_seconds,
                        _last_token_count,
                    )
                    _stall_warned = True
                    # Emit stall event for SSE consumers (#413)
                    self._emit_stall_event(session_key, stall_seconds, _last_token_count)

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
            _TERMINAL_REASONS = {"stop", "end_turn", "error", "max_tokens"}  # noqa: N806
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
                _GATEWAY_RETRYABLE_ERRORS = {  # noqa: N806
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
                list_result = self._invoke_tool(
                    "sessions_list",
                    {
                        "activeMinutes": 60,
                    },
                )
                list_text = self._parse_tool_text(list_result)
                if list_text:
                    sessions_data = json.loads(list_text)
                    sessions = (
                        sessions_data
                        if isinstance(sessions_data, list)
                        else sessions_data.get("sessions", [])
                    )
                    for s in sessions:
                        sk = s.get("sessionKey", "") or s.get("key", "")
                        if sk == session_key:
                            total_tokens = s.get("totalTokens", 0)
                            break
            except Exception as exc:  # noqa: BLE001
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
                "Session %s completed: %d chars captured, %d tokens consumed" "%s",
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
        elapsed = (now_utc() - start_time).total_seconds()
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
            completed_at=now_utc(),
            model_used=model,
            tokens_consumed=0,
            execution_time_seconds=elapsed,
            cost_usd=None,
        )
