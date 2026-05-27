"""Tests for issue #347: Model fallback chain on executor errors.

Covers:
- ModelFallbackChain class: construction, current(), has_next(), advance()
- Default chain behaviour (["sonnet", "opus"] when nothing configured)
- OpenClawExecutor escalates to next chain tier when retries are exhausted
- PhaseDefinition.model_chain field and __post_init__ normalisation
- YAML template parser passes model_chain to PhaseDefinition
- Sequencer propagates model_chain to task.payload
- TemplateValidator warns on unknown model_chain entries
"""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.model_fallback import (
    ModelFallbackChain,
    DEFAULT_MODEL_CHAIN,
)
from orchestration_engine.openclaw_executor import (
    OpenClawExecutor,
    MODEL_MAP,
    _CIRCUIT_BREAKERS,
    _CIRCUIT_BREAKERS_LOCK,
)
from orchestration_engine.schemas import (
    ModelTier,
    Priority,
    TaskSpec,
    TaskState,
    TaskType,
)
import yaml

from orchestration_engine.templates import (
    PhaseDefinition,
    PipelineTemplate,
    TemplateEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Clear the module-level circuit breaker registry before each test."""
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()
    yield
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()


@pytest.fixture
def executor():
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
    )


def _make_task(model_chain=None, preferred_model=None):
    """Helper to build a minimal TaskSpec with optional model_chain payload."""
    payload = {"prompt": "Write a test."}
    if model_chain is not None:
        payload["model_chain"] = model_chain
    return TaskSpec(
        type=TaskType.CONTENT,
        payload=payload,
        priority=Priority.NORMAL,
        preferred_model=preferred_model or ModelTier.SONNET,
    )


# ===========================================================================
# ModelFallbackChain unit tests
# ===========================================================================


class TestModelFallbackChainBasics:

    def test_default_chain_when_none(self):
        chain = ModelFallbackChain(None)
        assert chain.tiers == DEFAULT_MODEL_CHAIN

    def test_default_chain_when_empty(self):
        chain = ModelFallbackChain([])
        assert chain.tiers == DEFAULT_MODEL_CHAIN

    def test_custom_chain(self):
        chain = ModelFallbackChain(["haiku", "sonnet", "opus"])
        assert chain.tiers == ["haiku", "sonnet", "opus"]

    def test_current_starts_at_index_zero(self):
        chain = ModelFallbackChain(["sonnet", "opus"])
        assert chain.current() == "sonnet"
        assert chain.index == 0

    def test_has_next_when_more_tiers(self):
        chain = ModelFallbackChain(["sonnet", "opus"])
        assert chain.has_next() is True

    def test_has_next_false_at_last_tier(self):
        chain = ModelFallbackChain(["opus"])
        assert chain.has_next() is False

    def test_advance_returns_next_tier(self):
        chain = ModelFallbackChain(["sonnet", "opus"])
        result = chain.advance()
        assert result == "opus"
        assert chain.current() == "opus"
        assert chain.index == 1

    def test_advance_raises_at_end(self):
        chain = ModelFallbackChain(["opus"])
        with pytest.raises(IndexError, match="cannot advance past the last tier"):
            chain.advance()

    def test_reset_returns_to_start(self):
        chain = ModelFallbackChain(["sonnet", "haiku", "opus"])
        chain.advance()
        chain.advance()
        assert chain.current() == "opus"
        chain.reset()
        assert chain.current() == "sonnet"
        assert chain.index == 0

    def test_tiers_returns_copy(self):
        chain = ModelFallbackChain(["sonnet", "opus"])
        tiers = chain.tiers
        tiers.append("haiku")
        # Internal state should not be mutated
        assert chain.tiers == ["sonnet", "opus"]

    def test_repr_contains_key_info(self):
        chain = ModelFallbackChain(["sonnet", "opus"])
        r = repr(chain)
        assert "sonnet" in r
        assert "index=0" in r

    def test_three_tier_chain_traversal(self):
        chain = ModelFallbackChain(["haiku", "sonnet", "opus"])
        assert chain.current() == "haiku"
        assert chain.has_next() is True
        chain.advance()
        assert chain.current() == "sonnet"
        assert chain.has_next() is True
        chain.advance()
        assert chain.current() == "opus"
        assert chain.has_next() is False


# ===========================================================================
# OpenClawExecutor fallback chain integration tests
# ===========================================================================


class TestExecutorFallbackChain:
    """Test that executor escalates model on retry exhaustion."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self):
        """Patch time.sleep to skip backoff delays in all tests in this class."""
        with patch("orchestration_engine.openclaw_executor.time.sleep"):
            yield

    def _make_failing_run_session(self, fail_model: str, success_model: str):
        """Return a _run_session mock that fails on *fail_model* and succeeds on *success_model*."""
        def _run(prompt, model, thinking, timeout=None, **kwargs):
            if model == fail_model:
                raise RuntimeError(f"Model {model} unavailable")
            if model == success_model:
                return "fallback output", 42
            raise RuntimeError(f"Unexpected model: {model}")
        return _run

    def test_escalates_to_second_tier_when_first_fails(self, executor):
        """When sonnet exhausts retries, executor tries opus and succeeds."""
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]

        call_counts = {"sonnet": 0, "opus": 0}

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            if model == sonnet_model:
                call_counts["sonnet"] += 1
                raise RuntimeError("Sonnet unavailable")
            if model == opus_model:
                call_counts["opus"] += 1
                return "opus output", 100
            raise RuntimeError(f"Unexpected model: {model}")

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.SUCCESS
        assert result.result.get("text") == "opus output"
        # sonnet was tried max_attempts times before escalating
        assert call_counts["sonnet"] >= 1
        # opus was tried and succeeded
        assert call_counts["opus"] == 1

    def test_returns_failed_when_all_tiers_exhausted(self, executor):
        """When all tiers in the chain fail all retries, state is FAILED."""
        def _always_fail(prompt, model, thinking, timeout=None, **kwargs):
            raise RuntimeError("All models down")

        with patch.object(executor, "_run_session", side_effect=_always_fail):
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.FAILED

    def test_single_tier_chain_fails_without_escalation(self, executor):
        """A single-tier chain fails after retries without escalating."""
        call_models = []

        def _fail_always(prompt, model, thinking, timeout=None, **kwargs):
            call_models.append(model)
            raise RuntimeError("down")

        with patch.object(executor, "_run_session", side_effect=_fail_always):
            task = _make_task(model_chain=["sonnet"])
            result = executor.execute(task)

        assert result.state == TaskState.FAILED
        # All calls should be to sonnet only
        unique_models = set(call_models)
        assert len(unique_models) == 1
        assert MODEL_MAP["sonnet"] in unique_models

    def test_no_chain_in_payload_uses_implicit_fallback(self, executor):
        """When model_chain is absent, executor builds implicit chain from tier_key."""
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]

        call_models = []

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            call_models.append(model)
            if model == sonnet_model:
                raise RuntimeError("Sonnet down")
            if model == opus_model:
                return "opus success", 50
            raise RuntimeError(f"Unexpected: {model}")

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            task = _make_task()  # no model_chain in payload
            result = executor.execute(task)

        assert result.state == TaskState.SUCCESS
        assert opus_model in call_models

    def test_success_on_first_tier_skips_escalation(self, executor):
        """When first tier succeeds, second tier is never called."""
        call_models = []

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            call_models.append(model)
            return "success", 10

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.SUCCESS
        # Only sonnet should have been called
        assert all(m == MODEL_MAP["sonnet"] for m in call_models)
        assert MODEL_MAP["opus"] not in call_models

    def test_model_used_in_result_reflects_successful_tier(self, executor):
        """model_used in the result should reflect the tier that succeeded."""
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            if model == sonnet_model:
                raise RuntimeError("Sonnet down")
            return "opus output", 20

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.SUCCESS
        assert result.model_used == opus_model

    def test_three_tier_chain_tries_all_on_failure(self, executor):
        """With three tiers, executor tries all before returning FAILED."""
        call_models = []

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            call_models.append(model)
            raise RuntimeError("down")

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            task = _make_task(model_chain=["haiku", "sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.FAILED
        unique_models = set(call_models)
        assert MODEL_MAP["haiku"] in unique_models
        assert MODEL_MAP["sonnet"] in unique_models
        assert MODEL_MAP["opus"] in unique_models

    def test_cb_blocked_tier_is_skipped_and_next_tried(self, executor):
        """When escalated model's CB is open, that tier is skipped entirely."""
        from orchestration_engine.recovery import CircuitBreakerState, ExecutorRetryConfig
        from orchestration_engine.openclaw_executor import _CIRCUIT_BREAKERS, _CIRCUIT_BREAKERS_LOCK

        retry_cfg = ExecutorRetryConfig()
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]
        haiku_model = MODEL_MAP["haiku"]

        # Pre-open the circuit breaker for opus
        with _CIRCUIT_BREAKERS_LOCK:
            cb = CircuitBreakerState(name=opus_model)
            # Force failures to open the breaker
            for _ in range(retry_cfg.circuit_breaker_threshold):
                cb.record_failure(retry_cfg.circuit_breaker_threshold)
            _CIRCUIT_BREAKERS[opus_model] = cb

        call_models = []

        def _mock_run(prompt, model, thinking, timeout=None, **kwargs):
            call_models.append(model)
            if model == sonnet_model:
                raise RuntimeError("Sonnet down")
            if model == haiku_model:
                return "haiku success", 5
            raise RuntimeError(f"Unexpected: {model}")

        with patch.object(executor, "_run_session", side_effect=_mock_run):
            # Chain: sonnet → opus (CB open, skip) → haiku
            task = _make_task(model_chain=["sonnet", "opus", "haiku"])
            result = executor.execute(task)

        assert result.state == TaskState.SUCCESS
        assert MODEL_MAP["opus"] not in call_models
        assert MODEL_MAP["haiku"] in call_models


# ===========================================================================
# PhaseDefinition model_chain field tests
# ===========================================================================


class TestPhaseDefinitionModelChain:

    def test_default_is_empty_list(self):
        phase = PhaseDefinition(id="p1", name="Phase 1")
        assert phase.model_chain == []

    def test_explicit_chain_stored(self):
        phase = PhaseDefinition(id="p1", name="Phase 1", model_chain=["sonnet", "opus"])
        assert phase.model_chain == ["sonnet", "opus"]

    def test_none_normalised_to_empty_list(self):
        phase = PhaseDefinition(id="p1", name="Phase 1", model_chain=None)
        assert phase.model_chain == []

    def test_three_tier_chain(self):
        phase = PhaseDefinition(id="p1", name="Phase 1", model_chain=["haiku", "sonnet", "opus"])
        assert len(phase.model_chain) == 3


# ===========================================================================
# YAML template parsing tests
# ===========================================================================


def _write_yaml(tmp_path, data, filename="test.yaml"):
    """Helper: write a dict as YAML to a temp file and return its Path."""
    p = tmp_path / filename
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


def _make_template_data(**phase_overrides):
    """Build a minimal valid template data dict with one phase."""
    phase = {
        "id": "phase1",
        "name": "Phase One",
        "prompt_template": "Hello {input}",
    }
    phase.update(phase_overrides)
    return {
        "id": "test-pipeline",
        "name": "Test Pipeline",
        "description": "Test",
        "phases": [phase],
    }


class TestYAMLModelChainParsing:

    def test_model_chain_parsed_from_yaml(self, tmp_path):
        engine = TemplateEngine()
        data = _make_template_data(
            model_tier="sonnet",
            model_chain=["sonnet", "opus"],
        )
        path = _write_yaml(tmp_path, data)
        template = engine.load_template(path)
        phase = template.phases[0]
        assert phase.model_chain == ["sonnet", "opus"]

    def test_model_chain_absent_defaults_to_empty_list(self, tmp_path):
        engine = TemplateEngine()
        data = _make_template_data(model_tier="haiku")
        path = _write_yaml(tmp_path, data)
        template = engine.load_template(path)
        phase = template.phases[0]
        assert phase.model_chain == []

    def test_three_tier_chain_parsed(self, tmp_path):
        engine = TemplateEngine()
        data = _make_template_data(
            model_chain=["haiku", "sonnet", "opus"],
        )
        path = _write_yaml(tmp_path, data)
        template = engine.load_template(path)
        assert template.phases[0].model_chain == ["haiku", "sonnet", "opus"]


# ===========================================================================
# TemplateValidator model_chain validation tests
# ===========================================================================


class TestTemplateValidatorModelChain:

    def test_valid_chain_no_warnings(self, tmp_path):
        engine = TemplateEngine()
        data = _make_template_data(model_chain=["sonnet", "opus"])
        path = _write_yaml(tmp_path, data)
        template = engine.load_template(path)
        errors, warnings = engine.validate_template_extended(template, data)
        chain_warnings = [w for w in warnings if "model_chain" in w]
        assert chain_warnings == []

    def test_invalid_chain_entry_produces_warning(self, tmp_path):
        engine = TemplateEngine()
        data = _make_template_data(model_chain=["sonnet", "invalidmodel"])
        path = _write_yaml(tmp_path, data)
        template = engine.load_template(path)
        errors, warnings = engine.validate_template_extended(template, data)
        chain_warnings = [w for w in warnings if "model_chain" in w]
        assert len(chain_warnings) >= 1
        assert "invalidmodel" in chain_warnings[0]
