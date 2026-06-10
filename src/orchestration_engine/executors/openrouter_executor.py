"""OpenRouter executor — routes pipeline phases to any model via OpenRouter API.

OpenRouter exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint that
proxies to 200+ models from all major providers (Anthropic, OpenAI, Google, etc).
Auth is ``Authorization: Bearer <OPENROUTER_API_KEY>``.

This executor implements the ``TaskExecutor`` ABC so the ``PipelineSequencer``
can call ``executor.execute(task, model_tier="sonnet", thinking_level="high")``
and have it dynamically resolve to the right model.

Tool calling (ToscanAI/orchemist#794): when ``task.payload["disable_tools"]``
is not truthy, the executor advertises a tool schema to the model and runs an
agentic multi-turn loop that executes tool calls client-side and feeds the
results back as subsequent ``role: "tool"`` messages until the model returns
plain text. The sandbox for tool file/shell access is defined by
``task.payload["sandbox_roots"]`` populated by the sequencer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..command_security import check_shell_command
from ..config import _DEFAULT_OR_TIMEOUT
from ..model_registry import prefixed_id
from ..schemas import (
    ModelTier,
    TaskError,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskType,
)
from ..timestamps import now_utc
from ._common import _PRICING, BaseExecutor
from ._thinking import DEFAULT_THINKING_BUDGET, THINKING_BUDGET
from .openrouter_tools import (
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
    iso_now,
    normalise_sandbox_roots,
    summarise_args,
    summarise_result,
    truncate_tool_content,
)

logger = logging.getLogger(__name__)

# Token pricing is the single shared ``_PRICING`` instance from ``_common``
# (#927; imported above). The no-`total_cost` fallback (#913) routes through this
# PricingTable.compute_cost instead of a blended $/1K heuristic, using the real
# per-direction prompt/completion token counts.

# Default model tier → OpenRouter model ID mapping (Anthropic models), built
# from the canonical model_registry (#916). Every value has an exact
# pricing.yaml key; the OPUS tier emits anthropic/claude-opus-4-8.
DEFAULT_MODEL_MAP: Dict[str, str] = {
    "haiku": prefixed_id("haiku"),
    "sonnet": prefixed_id("sonnet"),
    "opus": prefixed_id("opus"),
}

# Models known to support extended thinking (Anthropic Claude 3.5+ and 4+)
_THINKING_SUPPORTED_PREFIXES = (
    "anthropic/claude-sonnet-4",
    "anthropic/claude-opus-4",
    "anthropic/claude-3-5-sonnet",
)

# When usage.total_cost is absent (the dominant case for Anthropic models on
# OpenRouter), the cost is computed from the first-party PricingTable using the
# real per-direction prompt/completion token counts (#913). This supersedes the
# former blended `$/1K` heuristic (issue #801: run 6bb0349c reported $61.01 vs
# the dashboard's actual $20.69); exact-match pricing with separate input/output
# rates is more accurate than a single blended rate. See `_PRICING` above.

# Tool-loop limits
MAX_TOOL_ITERATIONS = 100
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # before retries 1, 2, 3

# XML-leak detection pattern — model emitting Anthropic-style tool_use XML as text
_XML_LEAK_RE = re.compile(r"<tool_call\b|<tool_use\b")


class _CancellationContext:
    """Installs a SIGINT handler that sets an Event, enabling cancellable sleeps.

    ``sleep(seconds)`` returns True if cancelled mid-sleep. ``cancelled`` is
    True after any SIGINT. The previous SIGINT handler is restored on exit.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._prev_handler: Any = None
        self._installed = False

    def __enter__(self) -> "_CancellationContext":
        try:
            self._prev_handler = signal.signal(signal.SIGINT, self._on_sigint)
            self._installed = True
        except (ValueError, OSError):
            # signal.signal() only works in the main thread; skip silently elsewhere.
            self._installed = False
        return self

    def __exit__(self, *args: Any) -> None:
        if self._installed:
            try:
                signal.signal(signal.SIGINT, self._prev_handler)
            except (ValueError, OSError):
                pass

    def _on_sigint(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def sleep(self, seconds: float) -> bool:
        """Cancellable sleep. Returns True if SIGINT arrived during the sleep."""
        if seconds <= 0:
            return self.cancelled
        return self._event.wait(seconds)


class OpenRouterExecutor(BaseExecutor):
    """Executor that calls the OpenRouter API (OpenAI-compatible)."""

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_map: Optional[Dict[str, str]] = None,
        timeout_seconds: int = _DEFAULT_OR_TIMEOUT,
        max_tokens: int = 16384,
        tool_result_cap: int = 10000,
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model_map = {**DEFAULT_MODEL_MAP, **(model_map or {})}
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        # Issue #800: char cap on the JSON-stringified tool-result CONTENT stored in
        # the message history (configurable; default 10,000). Mirrors the
        # ``timeout_seconds`` / ``max_tokens`` constructor-tunable pattern. The full
        # result is spilled to disk and the JSONL summary stays derived-from-full.
        self.tool_result_cap = tool_result_cap
        # Warn-once flag for the "sandbox_roots missing/empty → tmp-only fallback" case.
        self._sandbox_fallback_warned = False
        # Warn-once SETS keyed by resolved model id (#967). A single bool would
        # under-report when a multi-model run drops thinking / estimates cost for
        # several distinct models; the set fires exactly once per (instance, model).
        self._thinking_drop_warned: set[str] = set()
        self._cost_estimated_warned: set[str] = set()

        if not self.api_key:
            logger.warning(
                "No OpenRouter API key provided. Set OPENROUTER_API_KEY or pass "
                "api_key. The executor will fail on real calls."
            )

    # ── TaskExecutor ABC ─────────────────────────────────────────────

    _COMMAND_TASK_TYPES = frozenset({TaskType.COMMAND, TaskType.ACCEPTANCE_RUN})

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: ARG002
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        # Rough cost estimate via the canonical PricingTable (#916), using a
        # representative token assumption (input ~500, output ~2000).
        tier = task.preferred_model or ModelTier.SONNET
        return _PRICING.compute_cost(prefixed_id(tier), 500, 2000)

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "openrouter",
        model_tier: Optional[str] = None,
        thinking_level: Optional[str] = None,
    ) -> TaskResult:
        # NOTE: start_time is a float epoch (time.time()) used across the tool
        # loop's float arithmetic. OpenRouter's `started_at=datetime.now()` bug
        # (set at completion, not entry) is DEFERRED per #927 scope — converting
        # this to a datetime touches 10+ internal call sites in the tool loop.
        start_time = time.time()
        task_id = self._resolve_task_id(task)

        tier_str = model_tier or "sonnet"
        if isinstance(tier_str, ModelTier):
            tier_str = tier_str.value
        model = self.model_map.get(tier_str, tier_str)

        payload = task.payload or {}
        prompt = payload.get("prompt", "")
        if not prompt:
            prompt = json.dumps(payload, indent=2)

        thinking = thinking_level or "off"
        # Thinking honesty (#967): keep `use_thinking` truth byte-identical to the
        # historical gate (the same two predicates ANDed), but when a level was
        # *requested* against a model we cannot confirm supports extended thinking,
        # degrade LOUDLY — one WARNING per (executor-instance, resolved-model) — then
        # proceed with NO `thinking` body. We do NOT send an Anthropic-shaped thinking
        # block to non-Anthropic models on the speculation that OpenRouter forwards it:
        # a model that silently accepts it returns 200 and may bill reasoning tokens
        # with no signal (the exact silent-cost degradation this issue removes). The
        # 400-with-thinking strip stays the backstop for the Anthropic-supported set.
        _thinking_requested = thinking != "off" and thinking is not None
        _thinking_supported = any(model.startswith(p) for p in _THINKING_SUPPORTED_PREFIXES)
        use_thinking = _thinking_requested and _thinking_supported
        if (
            _thinking_requested
            and not _thinking_supported
            and model not in self._thinking_drop_warned
        ):
            logger.warning(
                "openrouter executor: thinking_level=%r requested but extended thinking is "
                "not confirmed-supported for model %r via OpenRouter — omitted; set "
                "thinking_level to 'off' to silence this warning.",
                thinking,
                model,
            )
            self._thinking_drop_warned.add(model)

        disable_tools = bool(payload.get("disable_tools", False))
        phase_id = payload.get("phase_id") or task_id

        # Fast-path: COMMAND/ACCEPTANCE_RUN task types with a concrete command
        # run locally via subprocess — zero LLM tokens, zero cost.
        # Note: ACCEPTANCE_RUN phases in the template often have NO command field
        # (the engine is supposed to handle them internally). When command is empty,
        # the fast-path is skipped and the task falls through to the LLM path.
        task_type = task.type if hasattr(task, "type") else None
        if task_type in self._COMMAND_TASK_TYPES:
            cmd = payload.get("command", "")
            if cmd:
                return self._execute_command_locally(
                    cmd,
                    payload.get("working_dir") or os.getcwd(),
                    task,
                    worker_id,
                    start_time,
                    phase_id,
                    payload,
                )

        # Normalise sandbox_roots once up front; detect fallback and warn ONCE per instance.
        roots, fallback_triggered = normalise_sandbox_roots(payload.get("sandbox_roots"))
        if fallback_triggered and not self._sandbox_fallback_warned:
            logger.warning(
                "openrouter executor: sandbox_roots absent/empty; falling back to tmp_dir only"
            )
            self._sandbox_fallback_warned = True

        output_dir = roots.get("output_dir")
        jsonl_path: Optional[Path] = None
        if output_dir:
            jsonl_path = Path(output_dir) / f"{_safe_phase_id(phase_id)}_toolcalls.jsonl"

        logger.info(
            "OpenRouterExecutor: task=%s, model=%s, thinking=%s, disable_tools=%s, prompt_len=%d",
            task_id,
            model,
            thinking,
            disable_tools,
            len(prompt),
        )

        if disable_tools:
            # Legacy single-shot path — byte-identical to pre-#794 behaviour.
            body = self._build_request_body(
                model,
                prompt,
                use_thinking,
                thinking,
                tools_enabled=False,
            )
            result = self._call_api(body, task, worker_id, model, start_time)
            if (
                result.state == TaskState.FAILED
                and use_thinking
                and any(e.code == "bad_request" for e in (result.errors or []))
            ):
                body = self._build_request_body(model, prompt, False, "off", tools_enabled=False)
                result = self._call_api(body, task, worker_id, model, start_time)
            # Annotate metadata with zeroed-out tool-loop fields for shape consistency.
            result = _enrich_single_shot_metadata(result)
            return result

        # Tool-enabled multi-turn loop.
        return self._run_tool_loop(
            task=task,
            worker_id=worker_id,
            model=model,
            prompt=prompt,
            use_thinking=use_thinking,
            thinking_level=thinking,
            start_time=start_time,
            roots=roots,
            jsonl_path=jsonl_path,
            phase_id=phase_id,
        )

    # ── Tool loop ─────────────────────────────────────────────────────

    def _run_tool_loop(  # noqa: C901
        self,
        task: TaskSpec,
        worker_id: str,
        model: str,
        prompt: str,
        use_thinking: bool,
        thinking_level: str,
        start_time: float,
        roots: Dict[str, str],
        jsonl_path: Optional[Path],
        phase_id: str,
    ) -> TaskResult:
        messages: List[dict] = [{"role": "user", "content": prompt}]

        tool_call_count = 0
        round_trip_count = 0
        retry_count_total = 0
        xml_leak_detected = False
        xml_leak_snippet: Optional[str] = None
        parallel_tool_calls_observed = False
        jsonl_write_failed = False
        pending_tool_calls: Optional[list] = None
        total_cost = Decimal("0")
        total_tokens = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        # Cost honesty (#967): True if ANY round-trip fell back to `default` pricing
        # (no usage.total_cost + no exact key). `model` is loop-invariant, so once
        # flipped it stays True for the whole loop result.
        cost_estimated = False
        final_text = ""
        final_captured = False  # True only on a clean break (no-leak, no-tool_calls response)

        with _CancellationContext() as cancel:
            for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
                if cancel.cancelled:
                    return self._aborted_result(
                        task,
                        worker_id,
                        start_time,
                        {
                            "tool_call_count": tool_call_count,
                            "round_trip_count": round_trip_count,
                            "retry_count": retry_count_total,
                            "xml_leak_detected": xml_leak_detected,
                            "parallel_tool_calls_observed": parallel_tool_calls_observed,
                            "jsonl_write_failed": jsonl_write_failed,
                        },
                    )

                body = self._build_request_body(
                    model,
                    prompt,
                    use_thinking,
                    thinking_level,
                    tools_enabled=True,
                    messages=messages,
                )

                call_result = self._call_api_with_retry(
                    body=body,
                    model=model,
                    use_thinking=use_thinking,
                    thinking_level=thinking_level,
                    cancel=cancel,
                )
                retry_count_total += call_result.retries
                round_trip_count += 1

                if call_result.aborted:
                    return self._aborted_result(
                        task,
                        worker_id,
                        start_time,
                        {
                            "tool_call_count": tool_call_count,
                            "round_trip_count": round_trip_count,
                            "retry_count": retry_count_total,
                            "xml_leak_detected": xml_leak_detected,
                            "parallel_tool_calls_observed": parallel_tool_calls_observed,
                            "jsonl_write_failed": jsonl_write_failed,
                        },
                    )

                if call_result.error is not None:
                    return self._failed_mid_loop_result(
                        task,
                        worker_id,
                        start_time,
                        error_code=call_result.error_code or "openrouter_error_mid_loop",
                        error_message=call_result.error or "",
                        metadata={
                            "tool_call_count": tool_call_count,
                            "round_trip_count": round_trip_count,
                            "retry_count": retry_count_total,
                            "xml_leak_detected": xml_leak_detected,
                            "parallel_tool_calls_observed": parallel_tool_calls_observed,
                            "jsonl_write_failed": jsonl_write_failed,
                        },
                    )

                resp = call_result.response or {}
                usage = resp.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens", 0))
                completion_tokens = int(usage.get("completion_tokens", 0))
                total_prompt_tokens += prompt_tokens
                total_completion_tokens += completion_tokens
                total_tokens += prompt_tokens + completion_tokens
                call_cost = usage.get("total_cost")
                cost_estimated = self._cost_is_estimated(model, call_cost) or cost_estimated
                if call_cost is None:
                    call_cost = _PRICING.compute_cost(model, prompt_tokens, completion_tokens)
                total_cost += Decimal(str(call_cost))

                choice = (resp.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls") or None

                # XML-leak detection
                if content and _XML_LEAK_RE.search(content):
                    xml_leak_detected = True
                    if xml_leak_snippet is None:
                        xml_leak_snippet = content[:500]

                rt_has_xml_leak = bool(content) and bool(_XML_LEAK_RE.search(content))

                if not tool_calls:
                    if rt_has_xml_leak:
                        # Contract: XML leak without tool_calls must NOT terminate the loop.
                        # Append the assistant turn + a short nudge and go another round.
                        messages.append(_assistant_message_from_choice(message))
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response included <tool_call> XML in the text. "
                                    "Please use the native tool-calling API (tool_calls field) or, "
                                    "if no tool is needed, return only plain text."
                                ),
                            }
                        )
                        continue
                    final_text = content
                    final_captured = True
                    break

                # Parallel tool_calls observed when the model returns >1 despite parallel_tool_calls=false  # noqa: E501
                if len(tool_calls) > 1:
                    parallel_tool_calls_observed = True
                    logger.warning(
                        "model returned %d tool_calls despite parallel_tool_calls: false; processing sequentially",  # noqa: E501
                        len(tool_calls),
                    )

                # Iteration cap: if we would need iteration+1 to process, abort.
                if iteration == MAX_TOOL_ITERATIONS:
                    pending_tool_calls = tool_calls
                    return self._failed_iteration_cap_result(
                        task,
                        worker_id,
                        start_time,
                        metadata={
                            "tool_call_count": tool_call_count,
                            "round_trip_count": round_trip_count,
                            "retry_count": retry_count_total,
                            "xml_leak_detected": xml_leak_detected,
                            "parallel_tool_calls_observed": parallel_tool_calls_observed,
                            "jsonl_write_failed": jsonl_write_failed,
                            "pending_tool_calls": pending_tool_calls,
                        },
                    )

                # Append the assistant message verbatim, then append tool_result messages.
                messages.append(_assistant_message_from_choice(message))
                for tc in tool_calls:
                    if cancel.cancelled:
                        return self._aborted_result(
                            task,
                            worker_id,
                            start_time,
                            {
                                "tool_call_count": tool_call_count,
                                "round_trip_count": round_trip_count,
                                "retry_count": retry_count_total,
                                "xml_leak_detected": xml_leak_detected,
                                "parallel_tool_calls_observed": parallel_tool_calls_observed,
                                "jsonl_write_failed": jsonl_write_failed,
                            },
                        )
                    tool_id = tc.get("id") or f"call_{tool_call_count}"
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name") or ""
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        tool_args = (
                            json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        )
                    except json.JSONDecodeError:
                        tool_args = {}

                    tool_t0 = time.monotonic()
                    tool_result = self._execute_tool(tool_name, tool_args, roots, cancel)
                    tool_dt_ms = int((time.monotonic() - tool_t0) * 1000)
                    tool_call_count += 1

                    if jsonl_path is not None:
                        ok = _append_jsonl(
                            jsonl_path,
                            {
                                "iteration": tool_call_count,
                                "ts": iso_now(),
                                "tool": tool_name,
                                "args_summary": summarise_args(tool_args),
                                "result_summary": summarise_result(tool_name, tool_result),
                                "duration_ms": tool_dt_ms,
                            },
                        )
                        if not ok:
                            jsonl_write_failed = True

                    # Issue #800: cap the stored tool-result content. The full
                    # serialization is spilled to disk so the model can read it back;
                    # a marker is emitted ONLY when that spill write succeeds (Option
                    # A — a marker always implies a readable file). The JSONL block
                    # above stays derived from the FULL untruncated result dict.
                    full_content = json.dumps(tool_result)
                    content = full_content
                    output_dir = roots.get("output_dir")
                    if output_dir and len(full_content) > self.tool_result_cap:
                        spill_path = Path(output_dir) / (
                            f"{_safe_phase_id(phase_id)}_toolcall_{tool_call_count}.txt"
                        )
                        if _write_spill_file(spill_path, full_content):
                            content, _ = truncate_tool_content(
                                full_content, self.tool_result_cap, str(spill_path)
                            )
                        # else: spill write failed -> keep full_content, no marker.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": content,
                        }
                    )
            # End for loop.
            # If we got here without final_captured, every iteration was an XML-leak
            # nudge that never produced a real final response. Surface as failure
            # rather than returning empty output masquerading as success.
            if not final_captured:
                return self._failed_mid_loop_result(
                    task,
                    worker_id,
                    start_time,
                    error_code="xml_leak_loop_exhausted",
                    error_message=(
                        f"model emitted <tool_call> XML as text for all {MAX_TOOL_ITERATIONS} "
                        "iterations without producing a valid response or using the real tool API"
                    ),
                    metadata={
                        "tool_call_count": tool_call_count,
                        "round_trip_count": round_trip_count,
                        "retry_count": retry_count_total,
                        "xml_leak_detected": xml_leak_detected,
                        "xml_leak_content_snippet": xml_leak_snippet or "",
                        "parallel_tool_calls_observed": parallel_tool_calls_observed,
                        "jsonl_write_failed": jsonl_write_failed,
                    },
                )

        duration = time.time() - start_time
        success_metadata: Dict[str, Any] = {
            "worker_id": worker_id,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "tool_call_count": tool_call_count,
            "round_trip_count": round_trip_count,
            "retry_count": retry_count_total,
            "xml_leak_detected": xml_leak_detected,
            "xml_leak_content_snippet": xml_leak_snippet or "",
            "parallel_tool_calls_observed": parallel_tool_calls_observed,
            "jsonl_write_failed": jsonl_write_failed,
            "cost_estimated": cost_estimated,
        }
        if cost_estimated:
            success_metadata["cost_estimated_model"] = model
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"output": final_text},
            model_used=model,
            execution_time_seconds=duration,
            tokens_consumed=total_tokens,
            cost_usd=total_cost,
            started_at=now_utc(),
            completed_at=now_utc(),
            metadata=success_metadata,
        )

    def _execute_tool(
        self, tool_name: str, args: dict, roots: Dict[str, str], cancel: "_CancellationContext"
    ) -> dict:
        """Dispatch one tool call. Returns a JSON-serialisable result dict (never raises)."""
        handler = TOOL_DISPATCH.get(tool_name)
        if handler is None:
            return {
                "error": "invalid_tool_call",
                "message": f"tool {tool_name} does not exist",
            }
        try:
            return handler(args, roots, is_cancelled=lambda: cancel.cancelled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool handler %s raised: %s", tool_name, exc)
            return {"error": "tool_internal_error", "message": str(exc)}

    # ── HTTP call helpers ─────────────────────────────────────────────

    def _call_api_with_retry(  # noqa: C901
        self,
        body: Dict[str, Any],
        model: str,  # noqa: ARG002
        use_thinking: bool,
        thinking_level: str,  # noqa: ARG002
        cancel: _CancellationContext,
    ) -> "_CallResult":
        """Single logical round-trip: initial attempt + up to 3 retries on 5xx/429.

        Per-RT 400-with-thinking retry is "free" — it does NOT consume a retry slot.
        `retries` in the returned _CallResult counts RETRIES ACTUALLY COMPLETED (not
        the initial attempt; not aborted-during-backoff attempts that never ran).
        """
        attempt_body = body
        thinking_stripped = False
        last_error_msg = ""
        retries_completed = 0
        MAX_RETRIES = 3  # noqa: N806

        while True:  # manual attempt counting so free thinking-strip doesn't cost a slot
            if cancel.cancelled:
                return _CallResult(
                    response=None,
                    error=None,
                    error_code=None,
                    retries=retries_completed,
                    aborted=True,
                )
            try:
                response = self._do_post(attempt_body)
                return _CallResult(
                    response=response,
                    error=None,
                    error_code=None,
                    retries=retries_completed,
                    aborted=False,
                )
            except urllib.error.HTTPError as http_err:
                code = http_err.code
                err_text = _read_http_error_body(http_err)
                last_error_msg = f"HTTP {code}: {err_text}"

                # 400 with thinking: retry once WITHOUT thinking. FREE retry — does NOT
                # consume a retry slot (retries_completed unchanged).
                if code == 400 and use_thinking and not thinking_stripped:
                    logger.info("400 from OpenRouter with thinking; retrying without thinking")
                    attempt_body = dict(attempt_body)
                    attempt_body.pop("thinking", None)
                    thinking_stripped = True
                    continue

                if code in (429, 500, 502, 503, 504):
                    if retries_completed >= MAX_RETRIES:
                        return _CallResult(
                            response=None,
                            error=last_error_msg,
                            error_code="openrouter_error_mid_loop",
                            retries=retries_completed,
                            aborted=False,
                        )
                    backoff = RETRY_BACKOFF_SECONDS[retries_completed]
                    cancelled = cancel.sleep(backoff)
                    if cancelled:
                        return _CallResult(
                            response=None,
                            error=None,
                            error_code=None,
                            retries=retries_completed,
                            aborted=True,
                        )
                    retries_completed += 1
                    continue
                # Non-retriable (other 4xx).
                return _CallResult(
                    response=None,
                    error=last_error_msg,
                    error_code="openrouter_error_mid_loop",
                    retries=retries_completed,
                    aborted=False,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as net_err:
                last_error_msg = f"network error: {net_err}"
                if retries_completed >= MAX_RETRIES:
                    return _CallResult(
                        response=None,
                        error=last_error_msg,
                        error_code="openrouter_error_mid_loop",
                        retries=retries_completed,
                        aborted=False,
                    )
                backoff = RETRY_BACKOFF_SECONDS[retries_completed]
                cancelled = cancel.sleep(backoff)
                if cancelled:
                    return _CallResult(
                        response=None,
                        error=None,
                        error_code=None,
                        retries=retries_completed,
                        aborted=True,
                    )
                retries_completed += 1
                continue

    # ── Command fast-path (no LLM) ─────────────────────────────────

    _MAX_COMMAND_OUTPUT_BYTES = 1_000_000  # 1 MB — matches CommandExecutor.MAX_OUTPUT_BYTES

    # Exit-code sentinels (disambiguate failure modes in metadata["exit_code"]).
    _EXIT_TIMEOUT: int = -1  # TimeoutExpired (preserves pre-existing -1 contract)
    _EXIT_EXEC_ERR: int = -2  # other exec-level exception (OSError, etc.)
    _EXIT_SECURITY: int = -1  # security gate blocked before the shell ran

    def _execute_command_locally(
        self,
        command: str,
        working_dir: str,
        task: TaskSpec,
        worker_id: str,
        start_time: float,
        phase_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TaskResult:
        """Run a shell command locally via subprocess — zero token cost.

        A security gate (:func:`command_security.check_shell_command`) runs
        BEFORE the shell. The denylist is the always-on floor (both modes); the
        allowlist (from ``payload["allowed_commands"]``) is best-effort
        defense-in-depth applied only when non-empty. ``shell=True`` is retained
        for the actual run because the shipped tamper/maintenance gates depend on
        shell operators.
        """
        import subprocess as _sp  # noqa: PLC0415

        logger.info("OpenRouterExecutor: local command for phase %s: %s", phase_id, command[:200])

        # ── Security gate (denylist floor always; allowlist if non-empty) ────
        # [] and None both collapse to denylist-only mode.
        allowed = (payload or {}).get("allowed_commands") or None
        security_block = check_shell_command(command, allowed)
        if security_block is not None:
            error_code, security_message = security_block
            logger.warning(
                "OpenRouterExecutor: SECURITY — command blocked for phase %s: %s",
                phase_id,
                command[:200],
            )
            duration = time.time() - start_time
            return TaskResult(
                task_id=task.id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={"output": security_message},
                model_used="local-subprocess",
                execution_time_seconds=duration,
                tokens_consumed=0,
                cost_usd=Decimal("0"),
                started_at=now_utc(),
                completed_at=now_utc(),
                errors=[
                    TaskError(
                        code=error_code,
                        message=security_message,
                        severity="error",
                    )
                ],
                metadata={
                    "worker_id": worker_id,
                    "exit_code": self._EXIT_SECURITY,
                    "stdout_chars": 0,
                    "stderr_chars": len(security_message),
                    "command": command[:200],
                    "tool_call_count": 0,
                    "round_trip_count": 0,
                    "retry_count": 0,
                    "xml_leak_detected": False,
                    "xml_leak_content_snippet": "",
                    "parallel_tool_calls_observed": False,
                    "jsonl_write_failed": False,
                },
            )

        cwd = working_dir or os.getcwd()
        error_code: Optional[str] = None
        try:
            proc = _sp.run(
                command, shell=True, cwd=cwd, capture_output=True, timeout=self.timeout_seconds
            )
            stdout_raw = proc.stdout[: self._MAX_COMMAND_OUTPUT_BYTES]
            stderr_raw = proc.stderr[: self._MAX_COMMAND_OUTPUT_BYTES]
            stdout = stdout_raw.decode("utf-8", errors="replace")
            stderr = stderr_raw.decode("utf-8", errors="replace")
            exit_code = proc.returncode
            state = TaskState.SUCCESS if exit_code == 0 else TaskState.FAILED
            if state == TaskState.FAILED:
                error_code = "command_failed"
        except _sp.TimeoutExpired as exc:
            stdout = ((exc.stdout or b"")[: self._MAX_COMMAND_OUTPUT_BYTES]).decode(
                "utf-8", errors="replace"
            )
            stderr = ((exc.stderr or b"")[: self._MAX_COMMAND_OUTPUT_BYTES]).decode(
                "utf-8", errors="replace"
            )
            exit_code = self._EXIT_TIMEOUT
            state = TaskState.FAILED
            error_code = "command_timeout"
        except Exception as exc:  # noqa: BLE001
            stdout, stderr = "", str(exc)
            exit_code = self._EXIT_EXEC_ERR
            state = TaskState.FAILED
            error_code = "command_error"

        duration = time.time() - start_time
        output_text = stdout if stdout else stderr
        errors = []
        if state == TaskState.FAILED:
            errors.append(
                TaskError(
                    code=error_code or "command_failed",
                    message=f"exit {exit_code}: {stderr[:500]}" if stderr else f"exit {exit_code}",
                    severity="error",
                )
            )
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=state,
            confidence=1.0 if state == TaskState.SUCCESS else 0.0,
            result={"output": output_text},
            model_used="local-subprocess",
            execution_time_seconds=duration,
            tokens_consumed=0,
            cost_usd=Decimal("0"),
            started_at=now_utc(),
            completed_at=now_utc(),
            errors=errors if errors else [],
            metadata={
                "worker_id": worker_id,
                "exit_code": exit_code,
                "stdout_chars": len(stdout),
                "stderr_chars": len(stderr),
                "command": command[:200],
                "tool_call_count": 0,
                "round_trip_count": 0,
                "retry_count": 0,
                "xml_leak_detected": False,
                "xml_leak_content_snippet": "",
                "parallel_tool_calls_observed": False,
                "jsonl_write_failed": False,
            },
        )

    # ── HTTP call helpers ─────────────────────────────────────────────

    def _do_post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/ToscanAI/orchemist",
            "X-Title": "Orchemist Pipeline",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            parsed: Dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return parsed

    def _call_api(
        self,
        body: Dict[str, Any],
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        """Legacy single-shot call used only when disable_tools is True."""
        try:
            resp_data = self._do_post(body)
        except urllib.error.HTTPError as e:
            return self._handle_http_error(e, task, worker_id, model, start_time)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            duration = time.time() - start_time
            logger.error("OpenRouter network error for task: %s", e)
            return self._make_failed_result(
                task, worker_id, duration, "timeout", f"Network error: {e}"
            )
        return self._parse_response(resp_data, task, worker_id, model, start_time)

    # ── Cost-estimate flagging (#967) ─────────────────────────────────

    def _cost_is_estimated(self, model: str, api_total_cost: Optional[float]) -> bool:
        """Cost honesty (#967): True iff this call's cost was computed off the
        `default` pricing fallback — i.e. OpenRouter returned no ``usage.total_cost``
        AND the model has no exact ``pricing.yaml`` key.

        Emits ONE ``logger.warning`` per (executor-instance, resolved-model) the first
        time the flag flips True for a model, upgrading the invisible ``get_pricing``
        debug log to a visible signal WITHOUT touching the shared ``get_pricing`` (which
        the Anthropic/ledger paths rely on staying quiet). ``usage.total_cost`` is the
        source of truth when present and short-circuits estimation even for key-less
        models.
        """
        if api_total_cost is not None:
            return False
        if _PRICING.has_model(model):
            return False
        if model not in self._cost_estimated_warned:
            logger.warning(
                "openrouter executor: no exact pricing key for model %r and OpenRouter "
                "returned no usage.total_cost — cost computed from the 'default' estimate "
                "and flagged metadata['cost_estimated']=True.",
                model,
            )
            self._cost_estimated_warned.add(model)
        return True

    # ── Request body ──────────────────────────────────────────────────

    def _build_request_body(
        self,
        model: str,
        prompt: str,
        use_thinking: bool,
        thinking_level: str,
        tools_enabled: bool,
        messages: Optional[List[dict]] = None,
    ) -> Dict[str, Any]:
        if messages is None:
            messages = [{"role": "user", "content": prompt}]
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if tools_enabled:
            body["tools"] = TOOL_SCHEMAS
            body["parallel_tool_calls"] = False
            body["stream"] = False
        if use_thinking:
            budget = THINKING_BUDGET.get(thinking_level, DEFAULT_THINKING_BUDGET)
            if budget > 0:
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return body

    # ── Failure result constructors ───────────────────────────────────

    def _aborted_result(
        self, task: TaskSpec, worker_id: str, start_time: float, metadata: dict
    ) -> TaskResult:
        duration = time.time() - start_time
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            execution_time_seconds=duration,
            errors=[
                TaskError(code="tool_iteration_aborted", message="user cancelled", severity="error")
            ],
            metadata={
                "worker_id": worker_id,
                **metadata,
                "xml_leak_content_snippet": metadata.get("xml_leak_content_snippet", ""),
            },
        )

    def _failed_mid_loop_result(
        self,
        task: TaskSpec,
        worker_id: str,
        start_time: float,
        error_code: str,
        error_message: str,
        metadata: dict,
    ) -> TaskResult:
        duration = time.time() - start_time
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            execution_time_seconds=duration,
            errors=[TaskError(code=error_code, message=error_message, severity="error")],
            metadata={
                "worker_id": worker_id,
                **metadata,
                "xml_leak_content_snippet": metadata.get("xml_leak_content_snippet", ""),
            },
        )

    def _failed_iteration_cap_result(
        self,
        task: TaskSpec,
        worker_id: str,
        start_time: float,
        metadata: dict,
    ) -> TaskResult:
        duration = time.time() - start_time
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            execution_time_seconds=duration,
            errors=[
                TaskError(
                    code="tool_iteration_limit_exceeded",
                    message=f"hit {MAX_TOOL_ITERATIONS} tool-call iterations without final response",  # noqa: E501
                    severity="error",
                )
            ],
            metadata={
                "worker_id": worker_id,
                **metadata,
                "xml_leak_content_snippet": metadata.get("xml_leak_content_snippet", ""),
            },
        )

    # ── Legacy single-shot helpers ────────────────────────────────────

    def _parse_response(
        self,
        resp_data: Dict[str, Any],
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        duration = time.time() - start_time
        choices = resp_data.get("choices", [])
        if not choices:
            return self._make_failed_result(
                task,
                worker_id,
                duration,
                "empty_response",
                "API returned empty choices array",
            )
        content = choices[0].get("message", {}).get("content", "")
        usage = resp_data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens
        _api_cost = usage.get("total_cost")
        cost_estimated = self._cost_is_estimated(model, _api_cost)
        total_cost = _api_cost
        if total_cost is None:
            total_cost = _PRICING.compute_cost(model, prompt_tokens, completion_tokens)

        metadata: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "worker_id": worker_id,
            "cost_estimated": cost_estimated,
        }
        if cost_estimated:
            metadata["cost_estimated_model"] = model

        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"output": content},
            model_used=model,
            execution_time_seconds=duration,
            tokens_consumed=total_tokens,
            cost_usd=Decimal(str(total_cost)),
            started_at=now_utc(),
            completed_at=now_utc(),
            metadata=metadata,
        )

    def _handle_http_error(
        self,
        error: urllib.error.HTTPError,
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        duration = time.time() - start_time
        status = error.code
        error_msg = _read_http_error_body(error)

        error_map = {
            400: ("bad_request", f"Bad request: {error_msg}"),
            401: ("auth_error", "Invalid OpenRouter API key"),
            429: ("rate_limit", f"Rate limited: {error_msg}"),
            502: ("overloaded", f"Provider unavailable (502): {error_msg}"),
            503: ("overloaded", f"Provider unavailable (503): {error_msg}"),
        }
        code, message = error_map.get(status, ("api_error", f"HTTP {status}: {error_msg}"))
        logger.warning("OpenRouter HTTP %d for model %s: %s", status, model, message)
        return self._make_failed_result(task, worker_id, duration, code, message)

    def _make_failed_result(
        self,
        task: TaskSpec,
        worker_id: str,
        duration: float,
        error_code: str,
        error_message: str,
    ) -> TaskResult:
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            execution_time_seconds=duration,
            errors=[TaskError(code=error_code, message=error_message, severity="error")],
            metadata={"worker_id": worker_id},
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class _CallResult:
    __slots__ = ("response", "error", "error_code", "retries", "aborted")

    def __init__(
        self,
        response: Optional[dict],
        error: Optional[str],
        error_code: Optional[str],
        retries: int,
        aborted: bool,
    ):
        self.response = response
        self.error = error
        self.error_code = error_code
        self.retries = retries
        self.aborted = aborted


def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read().decode("utf-8"))
        if isinstance(body, dict):
            inner = body.get("error")
            if isinstance(inner, dict):
                return inner.get("message", "") or str(body)
            return str(body)
        return str(body)
    except Exception:  # noqa: BLE001
        return str(err)


def _assistant_message_from_choice(message: dict) -> dict:
    """Preserve the assistant message verbatim for the next round-trip's messages[]."""
    out = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out


def _append_jsonl(path: Path, record: dict) -> bool:
    """Append one JSONL record. Returns False on any OSError."""
    line = json.dumps(record, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        return True
    except OSError as exc:
        logger.warning("JSONL write failed at %s: %s (subsequent failures suppressed)", path, exc)
        return False


def _write_spill_file(path: Path, text: str) -> bool:
    """Write the FULL tool-result content to ``path`` (issue #800 disk spill).

    Mirrors :func:`_append_jsonl` resilience: mkdir parents, swallow ``OSError``,
    return a bool. Unconditional overwrite — NOT ``safe_write_phase_output``'s
    skip-if-larger guard, since the spill must always hold the complete output for
    read-back. Returns True on a confirmed write, False on any ``OSError`` (a
    read-only FS, a full disk, etc.); the caller emits no marker on False.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
        return True
    except OSError as exc:
        logger.warning("tool-result spill write failed at %s: %s", path, exc)
        return False


def _safe_phase_id(phase_id: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(phase_id))


def _enrich_single_shot_metadata(result: TaskResult) -> TaskResult:
    """For disable_tools=True path, ensure metadata has the tool-loop shape fields."""
    meta = dict(result.metadata or {})
    meta.setdefault("tool_call_count", 0)
    meta.setdefault("round_trip_count", 1 if result.state == TaskState.SUCCESS else 0)
    meta.setdefault("retry_count", 0)
    meta.setdefault("xml_leak_detected", False)
    meta.setdefault("xml_leak_content_snippet", "")
    meta.setdefault("parallel_tool_calls_observed", False)
    meta.setdefault("jsonl_write_failed", False)
    result.metadata = meta
    return result
