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
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from ..cost_tracker import PricingTable
from ..schemas import ModelTier, TaskError, TaskResult, TaskSpec, TaskState, TaskType

logger = logging.getLogger(__name__)

# Single source of truth for token pricing (Issue #908). Loaded once at import
# time from the bundled pricing.yaml; CostTracker.record_phase computes cost via
# the same PricingTable.compute_cost, so the executor and the ledger agree by
# construction. cost_tracker imports only .db, so this introduces no cycle.
_PRICING = PricingTable()

# Model tier → Anthropic model ID mapping
_MODEL_MAP = {
    ModelTier.HAIKU: "claude-haiku-4-5-20251001",
    ModelTier.SONNET: "claude-sonnet-4-6",
    ModelTier.OPUS: "claude-opus-4-6",
    # String fallbacks
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

# Thinking level → budget tokens (approximate)
_THINKING_BUDGET = {
    "off": 0,
    "low": 2048,
    "medium": 8192,
    "high": 32768,
}


class AnthropicExecutor:
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

    def can_handle(self, task_type: TaskType) -> bool:
        """This executor can handle all task types."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:
        """Rough cost estimate based on model tier."""
        # Very rough: input tokens ~500, output tokens ~2000
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
        worker_id: str = "anthropic-worker",
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
        start_time = datetime.now()
        task_id = task.id if hasattr(task, "id") else str(uuid4())

        # Resolve model
        tier = model_tier or (
            task.preferred_model.value
            if hasattr(task.preferred_model, "value")
            else task.preferred_model
        ) or "sonnet"
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
        budget = _THINKING_BUDGET.get(thinking, 0)
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
            elapsed = (datetime.now() - start_time).total_seconds()

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
                completed_at=datetime.now(),
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

        except Exception as exc:
            elapsed = (datetime.now() - start_time).total_seconds()
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
                completed_at=datetime.now(),
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
            raise RuntimeError(
                f"Anthropic API error {e.code}: {error_body}"
            ) from e

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
