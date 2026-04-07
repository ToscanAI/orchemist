"""OpenRouter executor — routes pipeline phases to any model via OpenRouter API.

OpenRouter exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint that
proxies to 200+ models from all major providers (Anthropic, OpenAI, Google, etc).
Auth is ``Authorization: Bearer <OPENROUTER_API_KEY>``.

This executor implements the ``TaskExecutor`` ABC so the ``PipelineSequencer``
can call ``executor.execute(task, model_tier="sonnet", thinking_level="high")``
and have it dynamically resolve to the right model.

Usage:
    executor = OpenRouterExecutor(api_key="sk-or-...")
    # or set OPENROUTER_API_KEY environment variable
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..schemas import (
    ConfidenceLevel,
    ModelTier,
    TaskError,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskType,
)

logger = logging.getLogger(__name__)

# Default model tier → OpenRouter model ID mapping (Anthropic models)
DEFAULT_MODEL_MAP: Dict[str, str] = {
    "haiku": "anthropic/claude-haiku-4.5",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6",
}

# Thinking level → budget tokens for Anthropic extended thinking
_THINKING_BUDGET: Dict[str, int] = {
    "off": 0,
    "low": 2048,
    "medium": 8192,
    "high": 32768,
}

# Models known to support extended thinking (Anthropic Claude 3.5+ and 4+)
_THINKING_SUPPORTED_PREFIXES = (
    "anthropic/claude-sonnet-4",
    "anthropic/claude-opus-4",
    "anthropic/claude-3-5-sonnet",
)

# Conservative fallback cost rate when usage.total_cost is absent
_FALLBACK_COST_PER_1K_TOKENS = 0.01


class OpenRouterExecutor:
    """Executor that calls the OpenRouter API (OpenAI-compatible).

    Supports model tier resolution, extended thinking for Anthropic models,
    and cost tracking from OpenRouter's ``usage.total_cost`` field.
    Uses only stdlib (urllib) — no third-party HTTP dependencies.
    """

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_map: Optional[Dict[str, str]] = None,
        timeout_seconds: int = 600,
        max_tokens: int = 16384,
    ):
        """Initialize the OpenRouter executor.

        Args:
            api_key: OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
            base_url: API base URL. Defaults to https://openrouter.ai/api/v1.
            model_map: Custom model tier → model ID mapping. Overrides defaults.
            timeout_seconds: HTTP request timeout in seconds. Default 300.
            max_tokens: Maximum output tokens per request. Default 16384.
        """
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model_map = {**DEFAULT_MODEL_MAP, **(model_map or {})}
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

        if not self.api_key:
            logger.warning(
                "No OpenRouter API key provided. Set OPENROUTER_API_KEY or pass "
                "api_key. The executor will fail on real calls."
            )

    # ── TaskExecutor ABC ─────────────────────────────────────────────

    def can_handle(self, task_type: TaskType) -> bool:
        """OpenRouter can handle all task types."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        """Rough cost estimate based on model tier."""
        tier = task.preferred_model or ModelTier.SONNET
        tier_str = tier.value if isinstance(tier, ModelTier) else str(tier)
        costs = {
            "haiku": 0.002,
            "sonnet": 0.015,
            "opus": 0.075,
        }
        return costs.get(tier_str, 0.02)

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "openrouter",
        model_tier: Optional[str] = None,
        thinking_level: Optional[str] = None,
    ) -> TaskResult:
        """Execute a task by calling the OpenRouter API.

        Args:
            task: Task specification with prompt in payload.
            worker_id: Identifier for this worker.
            model_tier: Model tier (haiku/sonnet/opus) or literal model name.
            thinking_level: Thinking level (off/low/medium/high) or None.

        Returns:
            TaskResult with execution outcome, token counts, and cost.
        """
        start_time = time.time()
        task_id = task.id if hasattr(task, "id") else str(uuid4())

        # Resolve model from tier
        tier_str = model_tier or "sonnet"
        if isinstance(tier_str, ModelTier):
            tier_str = tier_str.value
        model = self.model_map.get(tier_str, tier_str)

        # Extract prompt from payload
        prompt = task.payload.get("prompt", "")
        if not prompt:
            prompt = json.dumps(task.payload, indent=2)

        # Determine thinking config
        thinking = thinking_level or "off"
        use_thinking = (
            thinking != "off"
            and thinking is not None
            and any(model.startswith(p) for p in _THINKING_SUPPORTED_PREFIXES)
        )

        logger.info(
            "OpenRouterExecutor: task=%s, model=%s, thinking=%s, prompt_len=%d",
            task_id, model, thinking, len(prompt),
        )

        # Build request
        body = self._build_request_body(model, prompt, use_thinking, thinking)

        # First attempt
        result = self._call_api(body, task, worker_id, model, start_time)

        # Thinking retry: if we got 400 and were using thinking, retry without it
        if (
            result.state == TaskState.FAILED
            and use_thinking
            and any(e.code == "bad_request" for e in (result.errors or []))
        ):
            logger.info(
                "Task %s: retrying without thinking parameter (400 on thinking)",
                task_id,
            )
            body_no_thinking = self._build_request_body(model, prompt, False, "off")
            result = self._call_api(body_no_thinking, task, worker_id, model, start_time)

        return result

    # ── Internal helpers ─────────────────────────────────────────────

    def _build_request_body(
        self,
        model: str,
        prompt: str,
        use_thinking: bool,
        thinking_level: str,
    ) -> Dict[str, Any]:
        """Build the OpenAI-compatible request body."""
        body: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
        }

        if use_thinking:
            budget = _THINKING_BUDGET.get(thinking_level, 8192)
            if budget > 0:
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": budget,
                }

        return body

    def _call_api(
        self,
        body: Dict[str, Any],
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        """Make the HTTP call and parse the response into a TaskResult."""
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/ToscanAI/orchemist",
            "X-Title": "Orchemist Pipeline",
        }

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return self._handle_http_error(e, task, worker_id, model, start_time)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            duration = time.time() - start_time
            logger.error("OpenRouter network error for task: %s", e)
            return self._make_failed_result(
                task, worker_id, duration, "timeout", f"Network error: {e}"
            )

        # Parse successful response
        return self._parse_response(resp_data, task, worker_id, model, start_time)

    def _parse_response(
        self,
        resp_data: Dict[str, Any],
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        """Parse a successful API response into a TaskResult."""
        duration = time.time() - start_time
        choices = resp_data.get("choices", [])

        if not choices:
            return self._make_failed_result(
                task, worker_id, duration, "empty_response",
                "API returned empty choices array",
            )

        content = choices[0].get("message", {}).get("content", "")
        usage = resp_data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens

        # Cost: prefer OpenRouter's total_cost, fall back to estimation
        total_cost = usage.get("total_cost")
        if total_cost is None:
            total_cost = (total_tokens / 1000.0) * _FALLBACK_COST_PER_1K_TOKENS

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
            started_at=datetime.now(),
            completed_at=datetime.now(),
            metadata={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "worker_id": worker_id,
            },
        )

    def _handle_http_error(
        self,
        error: urllib.error.HTTPError,
        task: TaskSpec,
        worker_id: str,
        model: str,
        start_time: float,
    ) -> TaskResult:
        """Map HTTP errors to appropriate TaskResult error codes."""
        duration = time.time() - start_time
        status = error.code
        try:
            error_body = json.loads(error.read().decode("utf-8"))
            error_msg = (
                error_body.get("error", {}).get("message", "")
                or str(error_body)
            )
        except Exception:
            error_msg = str(error)

        error_map = {
            400: ("bad_request", f"Bad request: {error_msg}"),
            401: ("auth_error", "Invalid OpenRouter API key"),
            429: ("rate_limit", f"Rate limited: {error_msg}"),
            502: ("overloaded", f"Provider unavailable (502): {error_msg}"),
            503: ("overloaded", f"Provider unavailable (503): {error_msg}"),
        }

        code, message = error_map.get(
            status, ("api_error", f"HTTP {status}: {error_msg}")
        )

        logger.warning(
            "OpenRouter HTTP %d for model %s: %s", status, model, message
        )

        return self._make_failed_result(task, worker_id, duration, code, message)

    def _make_failed_result(
        self,
        task: TaskSpec,
        worker_id: str,
        duration: float,
        error_code: str,
        error_message: str,
    ) -> TaskResult:
        """Create a standardized failed TaskResult."""
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=TaskState.FAILED,
            confidence=0.0,
            result={},
            execution_time_seconds=duration,
            errors=[
                TaskError(
                    code=error_code,
                    message=error_message,
                    severity="error",
                )
            ],
            metadata={"worker_id": worker_id},
        )
