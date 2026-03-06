"""Diagnosis data model for the Orchestration Engine.

Provides the vocabulary and data structures used to classify pipeline-run
failures and prescribe remediations.  These types are produced by the
LLM-based diagnostician (phase 3.1.2+) and persisted via the Database
CRUD methods in db.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FailureClass(str, Enum):
    """Classification of why a pipeline run failed.

    Inherits from ``str`` so values can be stored/compared as plain strings
    (consistent with the rest of the codebase, e.g. ``TaskType``, ``TaskState``).
    """

    BAD_PROMPT = "bad_prompt"
    """The phase prompt was ambiguous, incomplete, or incorrectly specified."""

    INSUFFICIENT_CONTEXT = "insufficient_context"
    """The model lacked necessary context (files, history, domain knowledge)."""

    WRONG_MODEL = "wrong_model"
    """The selected model tier was unsuitable for the task complexity."""

    FLAKY_TEST = "flaky_test"
    """A test failed non-deterministically, not due to a real regression."""

    INFRA_ISSUE = "infra_issue"
    """Infrastructure problem: API timeout, rate limit, network failure, etc."""

    QUALITY_GAP = "quality_gap"
    """Output was produced but fell below the required quality threshold."""

    TIMEOUT = "timeout"
    """The phase or run exceeded its allotted time budget."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """The run exceeded its token or cost budget."""


class Remediation(str, Enum):
    """Prescribed remediation action following a failure diagnosis."""

    RETRY_SAME = "retry_same"
    """Retry the failing phase with the same configuration."""

    RETRY_ESCALATED_MODEL = "retry_escalated_model"
    """Retry with a more capable (higher-tier) model."""

    RETRY_WITH_CONTEXT = "retry_with_context"
    """Retry after injecting additional context into the prompt."""

    SPLIT_TASK = "split_task"
    """Decompose the failing phase into smaller sub-tasks."""

    ESCALATE_TO_HUMAN = "escalate_to_human"
    """Queue the run for human review; automated recovery is not feasible."""

    NO_ACTION = "no_action"
    """The failure is terminal or expected; no automated remediation."""


@dataclass
class DiagnosisResult:
    """Result of diagnosing a failed pipeline run.

    Attributes:
        failure_class:    Classification of the root cause.
        remediation:      Recommended remediation action.
        confidence:       Confidence score in [0.0, 1.0] for the diagnosis.
        explanation:      Human-readable explanation produced by the
                          diagnostician model.  May be None for programmatic
                          diagnoses that don't generate an explanation.
        model_used:       Identifier of the model used to produce the
                          diagnosis (e.g. ``'claude-haiku-4-5-20241022'``).
                          None for rule-based diagnostics.
        tokens_consumed:  Token count used by the diagnostician call.
                          0 for rule-based diagnostics.
    """

    failure_class: FailureClass
    remediation: Remediation
    confidence: float
    explanation: Optional[str] = None
    model_used: Optional[str] = None
    tokens_consumed: int = 0

    def to_db_dict(self, run_id: str) -> dict:
        """Serialise to a dict suitable for ``Database.insert_diagnosis()``.

        Args:
            run_id: The pipeline run ID this diagnosis belongs to.

        Returns:
            Dict with string values for enum fields, ready for DB insertion.
        """
        return {
            "run_id": run_id,
            "failure_class": self.failure_class.value,
            "remediation": self.remediation.value,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "model_used": self.model_used,
            "tokens_consumed": self.tokens_consumed,
        }
