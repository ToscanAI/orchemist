"""Adaptive retry strategy engine (Issue #3.2.1).

Maps DiagnosisResult → RetryPlan using a configurable FailureClass → RetryStrategy
table.  This is pure business logic with no I/O; callers are responsible for
persisting the resulting :class:`RetryPlan` and launching retry runs.

Typical usage::

    from orchestration_engine.adaptive_retry import AdaptiveRetryEngine
    from orchestration_engine.diagnosis import DiagnosisResult

    engine = AdaptiveRetryEngine()
    plan = engine.plan(diagnosis, original_run_id="run-abc123")
    if plan is None:
        # Non-retryable failure — escalate or abort
        ...
    else:
        # Apply plan.model_override, plan.extra_context, etc. when relaunching
        db.update_pipeline_run(new_run_id, retry_of_run_id=plan.original_run_id,
                               retry_strategy=plan.strategy.value)
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .diagnosis import DiagnosisResult, FailureClass, Remediation

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RetryStrategy enum
# ---------------------------------------------------------------------------


class RetryStrategy(str, Enum):
    """How to modify a failed pipeline run before retrying.

    Inherits from ``str`` so values can be stored/compared as plain strings,
    consistent with :class:`FailureClass` and :class:`Remediation`.
    """

    ESCALATE_MODEL = "escalate_model"
    """Use a higher model tier (e.g. Haiku → Sonnet → Opus)."""

    ADD_CONTEXT = "add_context"
    """Inject additional context into the failing phase prompt."""

    SPLIT_TASK = "split_task"
    """Decompose the failing phase into smaller sub-tasks (deferred to 3.2.2)."""

    REPHRASE_PROMPT = "rephrase_prompt"
    """Rewrite the failing phase prompt to reduce ambiguity."""

    RETRY_UNCHANGED = "retry_unchanged"
    """Retry with identical configuration (suitable for transient/flaky failures)."""

    INCREASE_TIMEOUT = "increase_timeout"
    """Multiply the phase timeout budget to allow more processing time."""


# ---------------------------------------------------------------------------
# RetryPlan dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetryPlan:
    """Concrete retry plan derived from a :class:`~orchestration_engine.diagnosis.DiagnosisResult`.

    Attributes:
        strategy:           The :class:`RetryStrategy` to apply on the next run.
        original_run_id:    The run ID of the failed run being retried.
        model_override:     If set, the retry run must use this model identifier
                            (e.g. ``"claude-opus-4-6"``).  ``None`` means keep
                            the original model.
        extra_context:      Additional text to inject into the phase prompt
                            before retrying.  ``None`` means no injection.
        timeout_multiplier: Factor by which to multiply the phase timeout.
                            Default ``1.0`` means no change.
    """

    strategy: RetryStrategy
    original_run_id: str
    model_override: Optional[str] = None
    extra_context: Optional[str] = None
    timeout_multiplier: float = 1.0

    def to_json(self) -> str:
        """Serialize this plan to a JSON string for DB storage.

        Returns:
            JSON string with ``strategy`` serialised as its string value.
        """
        d = asdict(self)
        d["strategy"] = self.strategy.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> "RetryPlan":
        """Deserialize a :class:`RetryPlan` from a JSON string.

        Args:
            raw: JSON string previously produced by :meth:`to_json`.

        Returns:
            Reconstructed :class:`RetryPlan` instance.

        Raises:
            ValueError: If *raw* is not valid JSON or missing required fields.
        """
        d = json.loads(raw)
        d["strategy"] = RetryStrategy(d["strategy"])
        return cls(**d)


# ---------------------------------------------------------------------------
# Failure-class → retry-strategy mapping
# ---------------------------------------------------------------------------

#: Default mapping from :class:`FailureClass` to :class:`RetryStrategy`.
#:
#: ``None`` means the failure is **non-retryable** — the caller must escalate
#: or abort rather than launching another run.
DEFAULT_STRATEGY_MAP: Dict[FailureClass, Optional[RetryStrategy]] = {
    FailureClass.QUALITY_GAP:          RetryStrategy.ESCALATE_MODEL,
    FailureClass.WRONG_MODEL:          RetryStrategy.ESCALATE_MODEL,
    FailureClass.INSUFFICIENT_CONTEXT: RetryStrategy.ADD_CONTEXT,
    FailureClass.BAD_PROMPT:           RetryStrategy.REPHRASE_PROMPT,
    FailureClass.FLAKY_TEST:           RetryStrategy.RETRY_UNCHANGED,
    FailureClass.INFRA_ISSUE:          RetryStrategy.RETRY_UNCHANGED,
    FailureClass.TIMEOUT:              RetryStrategy.INCREASE_TIMEOUT,
    FailureClass.BUDGET_EXCEEDED:      None,  # Non-retryable
}

#: Model escalation ladder — when strategy is ``ESCALATE_MODEL`` and no
#: explicit ``model_override`` is provided by the caller, we ascend this list
#: to pick the next tier above the current model.
#:
#: Index 0 is the lightest (cheapest) tier; higher indices are more capable.
MODEL_ESCALATION_LADDER: list[str] = [
    "claude-haiku-4-5-20241022",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]

#: Default timeout multiplier applied when strategy is ``INCREASE_TIMEOUT``.
DEFAULT_TIMEOUT_MULTIPLIER: float = 2.0


# ---------------------------------------------------------------------------
# AdaptiveRetryEngine
# ---------------------------------------------------------------------------


class AdaptiveRetryEngine:
    """Translates a :class:`DiagnosisResult` into an actionable :class:`RetryPlan`.

    This class is stateless and dependency-free; all configuration is supplied
    at construction time.  It contains no I/O — callers are responsible for
    persisting the plan and relaunching runs.

    Usage::

        engine = AdaptiveRetryEngine()
        plan = engine.plan(diagnosis, original_run_id="run-abc123")
        if plan is None:
            logger.error("Non-retryable failure for run %s", run_id)
        else:
            # Relaunch with plan.model_override / plan.extra_context / etc.
            ...
    """

    def __init__(
        self,
        strategy_map: Optional[Dict[FailureClass, Optional[RetryStrategy]]] = None,
        timeout_multiplier: float = DEFAULT_TIMEOUT_MULTIPLIER,
    ) -> None:
        """Initialise the engine.

        Args:
            strategy_map:       Custom :class:`FailureClass` → :class:`RetryStrategy`
                                mapping.  Defaults to :data:`DEFAULT_STRATEGY_MAP`.
            timeout_multiplier: Multiplier applied to the phase timeout when
                                strategy is ``INCREASE_TIMEOUT``.  Default ``2.0``.
        """
        self._strategy_map = strategy_map if strategy_map is not None else DEFAULT_STRATEGY_MAP
        self._timeout_multiplier = timeout_multiplier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        diagnosis: DiagnosisResult,
        original_run_id: str,
        current_model: Optional[str] = None,
    ) -> Optional[RetryPlan]:
        """Derive a :class:`RetryPlan` from a failed-run diagnosis.

        Args:
            diagnosis:        The :class:`DiagnosisResult` produced by the
                              :class:`~orchestration_engine.diagnosis.DiagnosisEngine`.
            original_run_id:  Run ID of the failed run being retried.
            current_model:    Model identifier used by the original run (e.g.
                              ``"claude-haiku-4-5-20241022"``).  Required for
                              meaningful model escalation; ``None`` falls back
                              to the top of the escalation ladder.

        Returns:
            A :class:`RetryPlan` when the failure is retryable, or ``None``
            when the failure is terminal (e.g. :attr:`FailureClass.BUDGET_EXCEEDED`).
        """
        failure_class = diagnosis.failure_class
        strategy = self._strategy_map.get(failure_class)

        if strategy is None:
            _logger.info(
                "Failure class %s is non-retryable for run %s — no retry plan produced.",
                failure_class.value,
                original_run_id,
            )
            return None

        _logger.info(
            "Producing retry plan for run %s: failure_class=%s strategy=%s",
            original_run_id,
            failure_class.value,
            strategy.value,
        )

        model_override: Optional[str] = None
        extra_context: Optional[str] = None
        timeout_multiplier: float = 1.0

        if strategy == RetryStrategy.ESCALATE_MODEL:
            model_override = self._next_model(current_model)

        elif strategy == RetryStrategy.ADD_CONTEXT:
            extra_context = self._build_extra_context(diagnosis)

        elif strategy == RetryStrategy.INCREASE_TIMEOUT:
            timeout_multiplier = self._timeout_multiplier

        return RetryPlan(
            strategy=strategy,
            original_run_id=original_run_id,
            model_override=model_override,
            extra_context=extra_context,
            timeout_multiplier=timeout_multiplier,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _next_model(current_model: Optional[str]) -> str:
        """Return the next model tier above *current_model* in the escalation ladder.

        If *current_model* is not found in the ladder, or is already at the
        top, returns the top-tier model.

        Args:
            current_model: Model identifier of the original run, or ``None``.

        Returns:
            Model identifier string for the escalated tier.
        """
        ladder = MODEL_ESCALATION_LADDER
        if current_model is None or current_model not in ladder:
            return ladder[-1]
        idx = ladder.index(current_model)
        next_idx = min(idx + 1, len(ladder) - 1)
        return ladder[next_idx]

    @staticmethod
    def _build_extra_context(diagnosis: DiagnosisResult) -> str:
        """Build an extra context string to inject when strategy is ADD_CONTEXT.

        Uses the diagnosis explanation (when available) to provide the model
        with insight into why the previous run failed.

        Args:
            diagnosis: The :class:`DiagnosisResult` for the failed run.

        Returns:
            A formatted context string for prompt injection.
        """
        explanation = diagnosis.explanation or (
            "The previous run failed due to insufficient context. "
            "Please request all necessary files and information before proceeding."
        )
        return (
            f"[RETRY CONTEXT] The previous run failed with diagnosis: "
            f"{diagnosis.failure_class.value}. "
            f"Details: {explanation}"
        )

    # ------------------------------------------------------------------
    # Strategy executor methods (Issue #395, 3.2.2)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_retry_unchanged(plan: RetryPlan, input_json: Dict[str, Any]) -> Dict[str, Any]:
        """Return a deep copy of *input_json* with no modifications.

        Used when the failure is transient or flaky and the best action is
        to rerun with an identical configuration.

        Args:
            plan:       The :class:`RetryPlan` (unused, kept for interface consistency).
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json*.
        """
        return copy.deepcopy(input_json)

    @staticmethod
    def _apply_escalate_model(plan: RetryPlan, input_json: Dict[str, Any]) -> Dict[str, Any]:
        """Return *input_json* with the model escalated to ``plan.model_override``.

        Sets the ``model_override`` key so that the daemon integration (#3.2.3)
        can pass it to the pipeline runner when relaunching the retry run.

        Args:
            plan:       The :class:`RetryPlan` carrying the target model identifier
                        in :attr:`~RetryPlan.model_override`.
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json* with ``model_override`` set.
        """
        result = copy.deepcopy(input_json)
        result["model_override"] = plan.model_override
        return result

    @staticmethod
    def _apply_increase_timeout(plan: RetryPlan, input_json: Dict[str, Any]) -> Dict[str, Any]:
        """Return *input_json* with timeout fields scaled by ``plan.timeout_multiplier``.

        Modifies ``timeout_seconds`` (and ``timeout_override`` when present) by
        multiplying their current value by :attr:`~RetryPlan.timeout_multiplier`.
        When neither key exists the multiplier is stored under
        ``timeout_override`` so the runner can apply it on relaunch.

        Args:
            plan:       The :class:`RetryPlan` carrying the desired multiplier.
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json* with timeout fields scaled.
        """
        result = copy.deepcopy(input_json)
        multiplier = plan.timeout_multiplier

        if "timeout_seconds" in result and isinstance(result["timeout_seconds"], (int, float)):
            result["timeout_seconds"] = int(result["timeout_seconds"] * multiplier)
        elif "timeout_override" in result and isinstance(result["timeout_override"], (int, float)):
            result["timeout_override"] = int(result["timeout_override"] * multiplier)
        else:
            # No existing timeout key — record multiplier so downstream can apply it
            result["timeout_multiplier"] = multiplier

        return result

    @staticmethod
    def _apply_rephrase_prompt(
        plan: RetryPlan,
        input_json: Dict[str, Any],
        diagnosis: DiagnosisResult,
    ) -> Dict[str, Any]:
        """Return *input_json* with failure context injected into prompt fields.

        Appends a structured ``[RETRY CONTEXT]`` block to the ``extra_context``
        key (creating it when absent).  Downstream phase prompts that include
        ``{{ extra_context }}`` or ``{{ input.extra_context }}`` will
        automatically receive the retry hint.

        Args:
            plan:       The :class:`RetryPlan` (unused, kept for interface
                        consistency; callers that pre-computed context can pass
                        it via *input_json* directly).
            input_json: The original pipeline input configuration dict.
            diagnosis:  The :class:`DiagnosisResult` for the failed run, used
                        to explain *why* the prompt was problematic.

        Returns:
            Deep copy of *input_json* with ``extra_context`` set/appended.
        """
        result = copy.deepcopy(input_json)

        explanation = diagnosis.explanation or (
            "The previous run failed due to a poorly structured prompt. "
            "Please clarify ambiguous requirements and add concrete examples."
        )
        retry_block = (
            f"\n\n[RETRY CONTEXT] Previous run failed with diagnosis: "
            f"{diagnosis.failure_class.value}. "
            f"Details: {explanation}. "
            f"Please rephrase or clarify your response to address this issue."
        )

        existing = result.get("extra_context") or ""
        result["extra_context"] = (existing + retry_block).strip()
        return result

    # ------------------------------------------------------------------
    # Public dispatcher (Issue #395, 3.2.2)
    # ------------------------------------------------------------------

    def build_retry_input(
        self,
        plan: RetryPlan,
        original_run: Dict[str, Any],
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> Dict[str, Any]:
        """Translate a :class:`RetryPlan` into a modified input config dict.

        This is the primary entry point for the daemon integration (#3.2.3).
        It reads the ``input_json`` field from *original_run* (a DB pipeline-run
        row), deep-copies it, applies the appropriate executor for
        ``plan.strategy``, and returns the result ready for
        :func:`~orchestration_engine.pipeline_runner.run_pipeline`.

        Args:
            plan:         The :class:`RetryPlan` produced by :meth:`plan`.
            original_run: DB row dict for the failed run.  Must contain an
                          ``input_json`` key holding either a JSON string or an
                          already-parsed dict.
            diagnosis:    The :class:`DiagnosisResult` for the failed run.
                          Required when ``plan.strategy`` is
                          :attr:`RetryStrategy.REPHRASE_PROMPT` or
                          :attr:`RetryStrategy.ADD_CONTEXT`.  ``None`` is
                          accepted for other strategies.

        Returns:
            Modified input configuration dict ready for the pipeline runner.

        Raises:
            ValueError: If ``original_run`` is missing the ``input_json`` key,
                        or if *diagnosis* is ``None`` when required by the
                        chosen strategy.
        """
        if "input_json" not in original_run:
            raise ValueError(
                "original_run must contain an 'input_json' key; "
                f"got keys: {list(original_run.keys())}"
            )

        raw = original_run["input_json"]
        if isinstance(raw, str):
            input_json: Dict[str, Any] = json.loads(raw)
        else:
            input_json = copy.deepcopy(raw)

        strategy = plan.strategy

        if strategy == RetryStrategy.RETRY_UNCHANGED:
            return self._apply_retry_unchanged(plan, input_json)

        if strategy == RetryStrategy.ESCALATE_MODEL:
            return self._apply_escalate_model(plan, input_json)

        if strategy == RetryStrategy.INCREASE_TIMEOUT:
            return self._apply_increase_timeout(plan, input_json)

        if strategy in (RetryStrategy.REPHRASE_PROMPT, RetryStrategy.ADD_CONTEXT):
            if diagnosis is None:
                raise ValueError(
                    f"strategy={strategy.value!r} requires a DiagnosisResult; "
                    "pass diagnosis= to build_retry_input()"
                )
            return self._apply_rephrase_prompt(plan, input_json, diagnosis)

        # SPLIT_TASK is deferred to a future issue; fall back to unchanged.
        _logger.warning(
            "Strategy %s has no executor implementation yet; falling back to RETRY_UNCHANGED.",
            strategy.value,
        )
        return self._apply_retry_unchanged(plan, input_json)
