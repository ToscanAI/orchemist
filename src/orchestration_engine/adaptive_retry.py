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

import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, Optional

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
