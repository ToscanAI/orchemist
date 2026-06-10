"""Anthropic API executor — calls Claude models directly via the Messages API.

This is the primary executor for users who have an Anthropic API key.
No OpenClaw dependency required. Works with any Claude model.

Usage:
    executor = AnthropicExecutor(api_key="your-api-key-here")
    # or set ANTHROPIC_API_KEY environment variable
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..model_registry import bare_id
from ..schemas import ModelTier, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from ..timestamps import now_utc
from ._common import _PRICING, BaseExecutor
from ._thinking import DEFAULT_THINKING_BUDGET, THINKING_BUDGET

logger = logging.getLogger(__name__)

# Token pricing is the single shared ``_PRICING`` instance from ``_common``
# (Issue #927). It is re-exported here (imported above) so existing importers —
# e.g. ``tests/test_anthropic_executor.py`` — keep working unchanged. The former
# module-scope ``_PRICING = PricingTable()`` is gone; CostTracker.record_phase
# still computes cost via the same PricingTable.compute_cost, so the executor and
# the ledger agree by construction.

# Model tier → Anthropic (bare) model ID mapping, built from the canonical
# model_registry (#916). Both ModelTier enum keys and SHORT string keys resolve
# to the same canonical bare id; the OPUS tier emits claude-opus-4-8.
_MODEL_MAP = {
    **{tier: bare_id(tier) for tier in ModelTier},
    # String fallbacks
    "haiku": bare_id("haiku"),
    "sonnet": bare_id("sonnet"),
    "opus": bare_id("opus"),
}


class AnthropicExecutor(BaseExecutor):
    """Executor that calls the Anthropic Messages API directly.

    Supports all Claude models with optional extended thinking.
    Uses only stdlib (urllib) — no third-party HTTP dependencies.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: Optional[str] = None, max_tokens: int = 4096):
        """Initialize the executor.

        Args:
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            max_tokens: Maximum output tokens per request.
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens

        if not self.api_key:
            logger.warning(
                "No Anthropic API key provided. Set ANTHROPIC_API_KEY or pass api_key. "
                "The executor will fail on real calls."
            )

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: ARG002
        """This executor can handle all task types."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        """Rough cost estimate based on model tier.

        Delegates to the canonical PricingTable (#916) using a representative
        token assumption (input ~500, output ~2000), so estimates stay in sync
        with the same pricing.yaml the ledger bills against.
        """
        tier = task.preferred_model or ModelTier.SONNET
        return _PRICING.compute_cost(bare_id(tier), 500, 2000)

    def execute(
        self,
        task: TaskSpec,
        worker_id: str = "anthropic-worker",  # noqa: ARG002
        model_tier: str = None,
        thinking_level: str = None,
    ) -> TaskResult:
        """Execute a task by calling the Anthropic Messages API.

        Args:
            task: The task specification with prompt in payload.
            worker_id: Identifier for this worker.
            model_tier: Override model tier (haiku/sonnet/opus).
            thinking_level: Thinking budget (off/low/medium/high).

        Returns:
            TaskResult with the API response or error details.
        """
        start_time = self._capture_start_time()
        task_id = self._resolve_task_id(task)

        # Resolve model
        tier = (
            model_tier
            or (
                task.preferred_model.value
                if hasattr(task.preferred_model, "value")
                else task.preferred_model
            )
            or "sonnet"
        )
        model = _MODEL_MAP.get(tier, _MODEL_MAP.get(ModelTier.SONNET))

        # Extract prompt from payload
        prompt = task.payload.get("prompt", "")
        if not prompt:
            prompt = json.dumps(task.payload, indent=2)

        # Build request body
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        # Add thinking if requested
        thinking = thinking_level or "off"
        budget = THINKING_BUDGET.get(thinking, DEFAULT_THINKING_BUDGET)
        if budget > 0:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
            # Anthropic requires higher max_tokens when thinking is enabled
            body["max_tokens"] = max(self.max_tokens, budget + self.max_tokens)

        # Make the API call
        logger.info(
            f"Executing task {task_id}: model={model}, thinking={thinking}, "
            f"prompt_len={len(prompt)}"
        )

        try:
            response = self._call_api(body)
            elapsed = (now_utc() - start_time).total_seconds()

            # Extract text from response
            output_text = ""
            for block in response.get("content", []):
                if block.get("type") == "text":
                    output_text += block["text"]

            # Parse usage
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = input_tokens + output_tokens

            # Try to parse structured output (JSON) from the response
            output_data = self._try_parse_json(output_text)
            if output_data is None:
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
                tokens_consumed=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                execution_time_seconds=elapsed,
                cost_usd=_PRICING.compute_cost(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )

        except Exception as exc:  # noqa: BLE001
            elapsed = (now_utc() - start_time).total_seconds()
            logger.error(f"API call failed for task {task_id}: {exc}")

            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    TaskError(
                        code="api_error",
                        message=str(exc),
                        severity="error",
                    )
                ],
                started_at=start_time,
                completed_at=now_utc(),
                model_used=model,
                execution_time_seconds=elapsed,
            )

    def _call_api(self, body: dict) -> dict:
        """Make a raw HTTP call to the Anthropic Messages API.

        Uses stdlib urllib — no external dependencies.
        """
        data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic API error {e.code}: {error_body}") from e

    @staticmethod
    def _try_parse_json(text: str) -> Optional[dict]:
        """Try to parse JSON from the response text.

        Handles both raw JSON and JSON wrapped in markdown code blocks.
        """
        text = text.strip()

        # Try raw JSON
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

        # Try extracting from markdown code block
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

        if "```" in text:
            start = text.index("```") + 3
            # Skip optional language identifier
            newline = text.index("\n", start)
            end = text.index("```", newline)
            try:
                return json.loads(text[newline:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

        return None
