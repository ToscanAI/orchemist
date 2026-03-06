"""Tests for AdaptiveRetryEngine strategy executors (Issue #395, 3.2.2).

Covers:
- _apply_retry_unchanged: deep copy, no mutations
- _apply_escalate_model: model_override injection
- _apply_increase_timeout: timeout field scaling (all variants)
- _apply_rephrase_prompt: extra_context injection and append
- build_retry_input: strategy dispatch, input_json parsing (str + dict),
  error paths
"""
from __future__ import annotations

import json

import pytest

from orchestration_engine.adaptive_retry import (
    AdaptiveRetryEngine,
    RetryPlan,
    RetryStrategy,
)
from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan(
    strategy: RetryStrategy,
    model_override: str | None = None,
    extra_context: str | None = None,
    timeout_multiplier: float = 1.0,
) -> RetryPlan:
    return RetryPlan(
        strategy=strategy,
        original_run_id="run-test-001",
        model_override=model_override,
        extra_context=extra_context,
        timeout_multiplier=timeout_multiplier,
    )


def _diagnosis(
    failure_class: FailureClass = FailureClass.BAD_PROMPT,
    explanation: str = "Test explanation",
) -> DiagnosisResult:
    return DiagnosisResult(
        failure_class=failure_class,
        remediation=Remediation.RETRY_WITH_CONTEXT,
        confidence=0.9,
        explanation=explanation,
    )


def _run(input_dict: dict | None = None, as_string: bool = False) -> dict:
    """Build a fake DB pipeline-run row with input_json."""
    payload = input_dict or {"issue_title": "Fix bug", "repo_path": "/tmp/repo"}
    return {
        "run_id": "run-test-001",
        "input_json": json.dumps(payload) if as_string else payload,
    }


@pytest.fixture
def engine() -> AdaptiveRetryEngine:
    return AdaptiveRetryEngine()


# ===========================================================================
# TestApplyRetryUnchanged
# ===========================================================================


class TestApplyRetryUnchanged:
    def test_returns_equal_dict(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        input_json = {"key": "value", "nested": {"x": 1}}
        result = engine._apply_retry_unchanged(plan, input_json)
        assert result == input_json

    def test_returns_deep_copy_not_same_object(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        input_json = {"key": "value", "nested": {"x": 1}}
        result = engine._apply_retry_unchanged(plan, input_json)
        assert result is not input_json

    def test_mutation_does_not_affect_original(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        input_json = {"nested": {"x": 1}}
        result = engine._apply_retry_unchanged(plan, input_json)
        result["nested"]["x"] = 999
        assert input_json["nested"]["x"] == 1

    def test_empty_dict(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        result = engine._apply_retry_unchanged(plan, {})
        assert result == {}

    def test_plan_arg_is_ignored(self, engine):
        """Plan fields are not applied — method is pass-through."""
        plan = _plan(
            RetryStrategy.RETRY_UNCHANGED,
            model_override="claude-opus-4-6",
            timeout_multiplier=3.0,
        )
        input_json = {"key": "value"}
        result = engine._apply_retry_unchanged(plan, input_json)
        assert "model_override" not in result
        assert result == {"key": "value"}


# ===========================================================================
# TestApplyEscalateModel
# ===========================================================================


class TestApplyEscalateModel:
    def test_sets_model_override_key(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-sonnet-4-6")
        result = engine._apply_escalate_model(plan, {"issue_title": "Fix"})
        assert result["model_override"] == "claude-sonnet-4-6"

    def test_preserves_existing_keys(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-sonnet-4-6")
        input_json = {"issue_title": "Fix", "repo_path": "/tmp/r"}
        result = engine._apply_escalate_model(plan, input_json)
        assert result["issue_title"] == "Fix"
        assert result["repo_path"] == "/tmp/r"

    def test_overwrites_existing_model_override(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-opus-4-6")
        input_json = {"model_override": "claude-haiku-4-5-20241022"}
        result = engine._apply_escalate_model(plan, input_json)
        assert result["model_override"] == "claude-opus-4-6"

    def test_none_model_override_is_set(self, engine):
        """model_override=None means 'keep original' — it is still written."""
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override=None)
        result = engine._apply_escalate_model(plan, {})
        assert "model_override" in result
        assert result["model_override"] is None

    def test_returns_deep_copy_not_same_object(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-sonnet-4-6")
        input_json = {"key": "val"}
        result = engine._apply_escalate_model(plan, input_json)
        assert result is not input_json

    def test_does_not_mutate_original(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-sonnet-4-6")
        input_json = {"key": "val"}
        engine._apply_escalate_model(plan, input_json)
        assert "model_override" not in input_json


# ===========================================================================
# TestApplyIncreaseTimeout
# ===========================================================================


class TestApplyIncreaseTimeout:
    def test_scales_timeout_seconds(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        result = engine._apply_increase_timeout(plan, {"timeout_seconds": 600})
        assert result["timeout_seconds"] == 1200

    def test_scales_timeout_override_when_no_timeout_seconds(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=3.0)
        result = engine._apply_increase_timeout(plan, {"timeout_override": 100})
        assert result["timeout_override"] == 300

    def test_stores_multiplier_when_no_timeout_key(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.5)
        result = engine._apply_increase_timeout(plan, {"issue_title": "No timeout"})
        assert result["timeout_multiplier"] == 2.5

    def test_timeout_seconds_takes_precedence_over_timeout_override(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        input_json = {"timeout_seconds": 300, "timeout_override": 100}
        result = engine._apply_increase_timeout(plan, input_json)
        # timeout_seconds must be scaled; timeout_override is left untouched
        assert result["timeout_seconds"] == 600
        assert result["timeout_override"] == 100

    def test_result_is_int(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=1.5)
        result = engine._apply_increase_timeout(plan, {"timeout_seconds": 400})
        assert isinstance(result["timeout_seconds"], int)

    def test_multiplier_one_leaves_value_unchanged(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=1.0)
        result = engine._apply_increase_timeout(plan, {"timeout_seconds": 600})
        assert result["timeout_seconds"] == 600

    def test_does_not_mutate_original(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        input_json = {"timeout_seconds": 600}
        engine._apply_increase_timeout(plan, input_json)
        assert input_json["timeout_seconds"] == 600

    def test_returns_deep_copy(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        input_json = {"timeout_seconds": 600}
        result = engine._apply_increase_timeout(plan, input_json)
        assert result is not input_json

    def test_non_numeric_timeout_seconds_ignored(self, engine):
        """Non-numeric timeout_seconds must not raise; fallback to multiplier key."""
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        input_json = {"timeout_seconds": "unlimited"}
        result = engine._apply_increase_timeout(plan, input_json)
        # Non-numeric timeout_seconds is not scaled; multiplier stored instead
        assert result.get("timeout_multiplier") == 2.0 or result["timeout_seconds"] == "unlimited"


# ===========================================================================
# TestApplyRephrasePrompt
# ===========================================================================


class TestApplyRephrasePrompt:
    def test_sets_extra_context_key(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(explanation="Prompt was ambiguous")
        result = engine._apply_rephrase_prompt(plan, {}, diag)
        assert "extra_context" in result
        assert len(result["extra_context"]) > 0

    def test_includes_failure_class_in_context(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(failure_class=FailureClass.BAD_PROMPT, explanation="Too vague")
        result = engine._apply_rephrase_prompt(plan, {}, diag)
        assert FailureClass.BAD_PROMPT.value in result["extra_context"]

    def test_includes_explanation_in_context(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(explanation="Missing acceptance criteria")
        result = engine._apply_rephrase_prompt(plan, {}, diag)
        assert "Missing acceptance criteria" in result["extra_context"]

    def test_appends_to_existing_extra_context(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(explanation="Too vague")
        input_json = {"extra_context": "Original context here"}
        result = engine._apply_rephrase_prompt(plan, input_json, diag)
        assert "Original context here" in result["extra_context"]
        assert FailureClass.BAD_PROMPT.value in result["extra_context"]

    def test_null_explanation_uses_fallback(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = DiagnosisResult(
            failure_class=FailureClass.BAD_PROMPT,
            remediation=Remediation.RETRY_WITH_CONTEXT,
            confidence=0.7,
            explanation=None,
        )
        result = engine._apply_rephrase_prompt(plan, {}, diag)
        assert "extra_context" in result
        assert len(result["extra_context"]) > 0

    def test_does_not_mutate_original(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(explanation="Bad prompt")
        input_json = {"extra_context": "old"}
        engine._apply_rephrase_prompt(plan, input_json, diag)
        assert input_json["extra_context"] == "old"

    def test_returns_deep_copy(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis()
        input_json = {"key": "val"}
        result = engine._apply_rephrase_prompt(plan, input_json, diag)
        assert result is not input_json


# ===========================================================================
# TestBuildRetryInput
# ===========================================================================


class TestBuildRetryInput:

    # ------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------

    def test_accepts_input_json_as_string(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        run = _run({"key": "value"}, as_string=True)
        result = engine.build_retry_input(plan, run)
        assert result["key"] == "value"

    def test_accepts_input_json_as_dict(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        run = _run({"key": "value"}, as_string=False)
        result = engine.build_retry_input(plan, run)
        assert result["key"] == "value"

    def test_missing_input_json_raises_value_error(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        with pytest.raises(ValueError, match="input_json"):
            engine.build_retry_input(plan, {"run_id": "x"})

    # ------------------------------------------------------------------
    # Strategy dispatch: RETRY_UNCHANGED
    # ------------------------------------------------------------------

    def test_dispatches_retry_unchanged(self, engine):
        plan = _plan(RetryStrategy.RETRY_UNCHANGED)
        run = _run({"issue_title": "Fix"})
        result = engine.build_retry_input(plan, run)
        assert result == {"issue_title": "Fix"}
        assert "model_override" not in result

    # ------------------------------------------------------------------
    # Strategy dispatch: ESCALATE_MODEL
    # ------------------------------------------------------------------

    def test_dispatches_escalate_model(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-opus-4-6")
        run = _run({"issue_title": "Fix"})
        result = engine.build_retry_input(plan, run)
        assert result["model_override"] == "claude-opus-4-6"

    # ------------------------------------------------------------------
    # Strategy dispatch: INCREASE_TIMEOUT
    # ------------------------------------------------------------------

    def test_dispatches_increase_timeout(self, engine):
        plan = _plan(RetryStrategy.INCREASE_TIMEOUT, timeout_multiplier=2.0)
        run = _run({"issue_title": "Fix", "timeout_seconds": 300})
        result = engine.build_retry_input(plan, run)
        assert result["timeout_seconds"] == 600

    # ------------------------------------------------------------------
    # Strategy dispatch: REPHRASE_PROMPT
    # ------------------------------------------------------------------

    def test_dispatches_rephrase_prompt(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        diag = _diagnosis(explanation="Too vague")
        run = _run({"issue_title": "Fix"})
        result = engine.build_retry_input(plan, run, diagnosis=diag)
        assert "extra_context" in result
        assert "Too vague" in result["extra_context"]

    def test_rephrase_prompt_requires_diagnosis(self, engine):
        plan = _plan(RetryStrategy.REPHRASE_PROMPT)
        run = _run({"issue_title": "Fix"})
        with pytest.raises(ValueError, match="DiagnosisResult"):
            engine.build_retry_input(plan, run, diagnosis=None)

    # ------------------------------------------------------------------
    # Strategy dispatch: ADD_CONTEXT
    # ------------------------------------------------------------------

    def test_dispatches_add_context(self, engine):
        plan = _plan(RetryStrategy.ADD_CONTEXT)
        diag = _diagnosis(
            failure_class=FailureClass.INSUFFICIENT_CONTEXT,
            explanation="Missing file list",
        )
        run = _run({"issue_title": "Fix"})
        result = engine.build_retry_input(plan, run, diagnosis=diag)
        assert "extra_context" in result
        assert "Missing file list" in result["extra_context"]

    def test_add_context_requires_diagnosis(self, engine):
        plan = _plan(RetryStrategy.ADD_CONTEXT)
        run = _run({"issue_title": "Fix"})
        with pytest.raises(ValueError, match="DiagnosisResult"):
            engine.build_retry_input(plan, run, diagnosis=None)

    # ------------------------------------------------------------------
    # Strategy dispatch: SPLIT_TASK (deferred)
    # ------------------------------------------------------------------

    def test_split_task_falls_back_to_unchanged(self, engine):
        plan = _plan(RetryStrategy.SPLIT_TASK)
        run = _run({"issue_title": "Fix"})
        result = engine.build_retry_input(plan, run)
        # Must return a valid dict without raising
        assert result == {"issue_title": "Fix"}

    # ------------------------------------------------------------------
    # Isolation: original_run is not mutated
    # ------------------------------------------------------------------

    def test_does_not_mutate_original_run(self, engine):
        plan = _plan(RetryStrategy.ESCALATE_MODEL, model_override="claude-opus-4-6")
        run = _run({"issue_title": "Fix"})
        original_input_json = run["input_json"].copy() if isinstance(run["input_json"], dict) else run["input_json"]
        engine.build_retry_input(plan, run)
        assert run["input_json"] == original_input_json
