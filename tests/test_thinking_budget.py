"""Tests for the shared extended-thinking budget map (#919, item 2).

The map and its fail-safe fallback live in
``orchestration_engine.executors._thinking`` so the Anthropic and OpenRouter
executors share a single source of truth. The unknown-level fallback is the
fail-safe ``0`` (thinking disabled) for BOTH executors — the openrouter
executor was changed from a silent ``8192`` (medium) fallback to ``0``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine.executors._thinking import (
    THINKING_BUDGET,
    DEFAULT_THINKING_BUDGET,
)
from orchestration_engine.executors.openrouter_executor import OpenRouterExecutor


def test_thinking_budget_map_values():
    """The four recognized levels map to their canonical budgets."""
    assert THINKING_BUDGET == {
        "off": 0,
        "low": 2048,
        "medium": 8192,
        "high": 32768,
    }


def test_default_thinking_budget_is_failsafe_zero():
    """An unrecognized level resolves to the fail-safe (disabled) budget."""
    assert DEFAULT_THINKING_BUDGET == 0


def test_unknown_level_falls_back_to_zero():
    """The consolidated fallback contract: unknown key -> 0, never a paid budget."""
    assert THINKING_BUDGET.get("ultra", DEFAULT_THINKING_BUDGET) == 0
    assert THINKING_BUDGET.get("max", DEFAULT_THINKING_BUDGET) == 0


def test_shared_identity_across_executors():
    """Both executors import the SAME map object (single source of truth)."""
    from orchestration_engine.executors import _thinking
    from orchestration_engine.executors import anthropic_executor
    from orchestration_engine.executors import openrouter_executor

    assert anthropic_executor.THINKING_BUDGET is _thinking.THINKING_BUDGET
    assert openrouter_executor.THINKING_BUDGET is _thinking.THINKING_BUDGET
    assert (
        anthropic_executor.DEFAULT_THINKING_BUDGET
        is _thinking.DEFAULT_THINKING_BUDGET
    )
    assert (
        openrouter_executor.DEFAULT_THINKING_BUDGET
        is _thinking.DEFAULT_THINKING_BUDGET
    )


def test_openrouter_unknown_level_omits_thinking_block():
    """Behavioral coverage for the 8192 -> 0 change.

    Driving ``_build_request_body`` with ``use_thinking=True`` and an unknown
    ``thinking_level`` must now yield a body with NO ``"thinking"`` key (budget
    resolves to 0, so ``if budget > 0`` is False). Previously the silent 8192
    fallback would have ADDED a thinking block.
    """
    executor = OpenRouterExecutor(api_key="sk-or-test")
    body = executor._build_request_body(
        model="anthropic/claude-sonnet-4",
        prompt="hi",
        use_thinking=True,
        thinking_level="ultra",  # unknown level
        tools_enabled=False,
    )
    assert "thinking" not in body


def test_openrouter_known_level_keeps_thinking_block():
    """Sanity: a known level still produces a thinking block with its budget."""
    executor = OpenRouterExecutor(api_key="sk-or-test")
    body = executor._build_request_body(
        model="anthropic/claude-sonnet-4",
        prompt="hi",
        use_thinking=True,
        thinking_level="high",
        tools_enabled=False,
    )
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] == 32768
