"""Diagnosis data model for the Orchestration Engine.

Provides the vocabulary and data structures used to classify pipeline-run
failures and prescribe remediations.  These types are produced by the
LLM-based diagnostician (phase 3.1.2+) and persisted via the Database
CRUD methods in db.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


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
                          diagnosis (e.g. ``'claude-haiku-4-5-20251001'``).
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


# ---------------------------------------------------------------------------
# Prompt template (Issue #3.1.2)
# ---------------------------------------------------------------------------

DIAGNOSIS_PROMPT_TEMPLATE = """\
You are a pipeline failure analyst. Classify the failure below.

## Error Message
{error_message}

## Phase Outputs (truncated to 4000 chars each)
{phase_context}

## Classification Task
Respond ONLY with valid JSON matching this exact schema:
{{
  "failure_class": "<one of: bad_prompt | insufficient_context | wrong_model | flaky_test | infra_issue | quality_gap | timeout | budget_exceeded>",
  "remediation": "<one of: retry_same | retry_escalated_model | retry_with_context | split_task | escalate_to_human | no_action>",
  "confidence": <float between 0.0 and 1.0>,
  "explanation": "<one sentence explanation>"
}}

Return only the JSON object. No markdown, no preamble.
"""

_logger = logging.getLogger(__name__)


class DiagnosisEngine:
    """LLM-powered pipeline failure diagnostician (Issue #3.1.2).

    Analyses failed pipeline runs by:

    1. Collecting phase output files from the run's output directory.
    2. Building a structured prompt for a lightweight Haiku model.
    3. Calling the provided executor and parsing the JSON response.
    4. Persisting the result via ``Database.insert_diagnosis()``.

    Usage::

        engine = DiagnosisEngine(executor=my_executor, db=my_db)
        result = engine.diagnose(
            run_id="run-abc123",
            error_message="Phase 'build' timed out after 600s",
            output_dir="/tmp/output/run-abc123",
        )
        print(result.failure_class, result.remediation)
    """

    #: Model tier used for diagnosis calls — lightweight Haiku keeps costs low.
    DEFAULT_MODEL_TIER: str = "haiku"

    def __init__(self, executor: Any, db: Any) -> None:
        """Initialise the engine.

        Args:
            executor: A ``TaskExecutor``-compatible object whose ``execute()``
                      method accepts a ``TaskSpec`` and returns a ``TaskResult``.
            db:       A ``Database`` instance used to persist diagnosis records.
        """
        self._executor = executor
        self._db = db

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_phase_context(output_dir: Optional[str]) -> str:
        """Read output files from *output_dir* and return formatted context.

        Reads all ``*.txt``, ``*.md``, and ``*.json`` files (non-recursive,
        sorted for determinism).  Each file is truncated to 4 000 characters
        with a ``... [truncated]`` marker when exceeded.

        Args:
            output_dir: Path to the pipeline run's output directory.
                        ``None`` or empty string → placeholder message.

        Returns:
            A formatted multi-block string, or a placeholder when no files
            are available.
        """
        if not output_dir:
            return "(no phase outputs available)"

        path = Path(output_dir)
        if not path.exists():
            return "(no phase outputs available)"

        # Collect matching files across all three patterns, deduplicate, sort.
        files: list[Path] = sorted(
            {
                f
                for pattern in ("*.txt", "*.md", "*.json")
                for f in path.glob(pattern)
                if f.is_file()
            }
        )

        if not files:
            return "(no phase output files found)"

        blocks: list[str] = []
        for f in files:
            content = f.read_text(encoding="utf-8", errors="replace")
            if len(content) > 4000:
                content = content[:4000] + "... [truncated]"
            blocks.append(f"### {f.name}\n{content}\n")

        return "\n".join(blocks)

    @staticmethod
    def _build_prompt(error_message: str, phase_context: str) -> str:
        """Render the diagnosis prompt template.

        Args:
            error_message: The failure error message from the pipeline run.
            phase_context: Formatted phase output context string.

        Returns:
            The fully rendered prompt string ready for the LLM.
        """
        return DIAGNOSIS_PROMPT_TEMPLATE.format(
            error_message=error_message or "(no error message provided)",
            phase_context=phase_context,
        )

    @staticmethod
    def _parse_llm_response(response_text: str) -> DiagnosisResult:
        """Parse an LLM JSON response into a :class:`DiagnosisResult`.

        Accepts a raw string that should be a JSON object.  On any parse or
        validation error, returns a fallback result with
        ``Remediation.ESCALATE_TO_HUMAN`` so failures are never silently
        dropped.

        Args:
            response_text: Raw text from the LLM (expected to be a JSON object).

        Returns:
            A ``DiagnosisResult`` populated from the parsed response, or a
            safe fallback on any error.
        """
        logger = logging.getLogger(__name__)
        try:
            text = response_text.strip()
            # Extract JSON from between markdown code fences if present.
            # Handles: ```json\n{...}\n```, ```\n{...}\n```,
            # and responses with preamble prose before the fence.
            fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
            if fence_match:
                text = fence_match.group(1).strip()
            data = json.loads(text)
            failure_class = FailureClass(data["failure_class"])
            remediation = Remediation(data["remediation"])
            confidence = float(data["confidence"])
            explanation = data.get("explanation")
            return DiagnosisResult(
                failure_class=failure_class,
                remediation=remediation,
                confidence=confidence,
                explanation=explanation,
            )
        except Exception as exc:
            logger.warning(
                "Failed to parse LLM diagnosis response: %s — raw text: %.200s",
                exc,
                response_text,
            )
            return DiagnosisResult(
                failure_class=FailureClass.INFRA_ISSUE,
                remediation=Remediation.ESCALATE_TO_HUMAN,
                confidence=0.0,
                explanation=f"Failed to parse LLM response: {exc}",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def diagnose(
        self,
        run_id: str,
        error_message: Optional[str] = None,
        output_dir: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> DiagnosisResult:
        """Diagnose a failed pipeline run.

        Orchestrates the full analysis flow:

        1. Collect phase output files from *output_dir*.
        2. Build and send a structured prompt to the LLM via *executor*.
        3. Parse and validate the JSON response.
        4. Persist the :class:`DiagnosisResult` via ``db.insert_diagnosis()``.
        5. Track the failure pattern via :class:`FailurePatternTracker` when
           *template_id* is provided (Issue #3.1.3).
        6. Return the persisted result.

        On any executor or parse failure, a safe fallback
        ``ESCALATE_TO_HUMAN`` result is persisted and returned so callers
        always receive a valid :class:`DiagnosisResult`.

        Args:
            run_id:        Pipeline run ID to associate with the diagnosis.
            error_message: The primary error message from the failed run.
            output_dir:    Path to the run's output directory for phase context.

        Returns:
            A persisted :class:`DiagnosisResult` describing the failure and
            recommended remediation.
        """
        # Lazy import to avoid circular dependency at module load time.
        from .schemas import Priority, TaskSpec, TaskType

        phase_context = self._collect_phase_context(output_dir)
        prompt = self._build_prompt(error_message or "", phase_context)

        task = TaskSpec(
            type=TaskType.ANALYSIS,
            payload={"prompt": prompt},
            priority=Priority.NORMAL,
        )

        # Call the executor — guard against any exception.
        try:
            exec_result = self._executor.execute(
                task,
                worker_id="diagnosis-engine",
                model_tier=self.DEFAULT_MODEL_TIER,
            )
        except Exception as exc:
            _logger.error(
                "Executor call failed during diagnosis for run %s: %s", run_id, exc
            )
            fallback = DiagnosisResult(
                failure_class=FailureClass.INFRA_ISSUE,
                remediation=Remediation.ESCALATE_TO_HUMAN,
                confidence=0.0,
                explanation=f"Executor call failed: {exc}",
            )
            self._db.insert_diagnosis(fallback.to_db_dict(run_id))
            return fallback

        # Handle non-success executor states.
        if exec_result.state not in ("success", "SUCCESS") and getattr(exec_result.state, "value", None) not in ("success",):
            _logger.warning(
                "Executor returned non-success state %s for run %s",
                exec_result.state,
                run_id,
            )
            fallback = DiagnosisResult(
                failure_class=FailureClass.INFRA_ISSUE,
                remediation=Remediation.ESCALATE_TO_HUMAN,
                confidence=0.0,
                explanation=f"Executor returned state: {exec_result.state}",
                model_used=exec_result.model_used,
                tokens_consumed=exec_result.tokens_consumed,
            )
            self._db.insert_diagnosis(fallback.to_db_dict(run_id))
            return fallback

        # Extract text.  AnthropicExecutor wraps plain text as {"text": ...};
        # if the LLM returned valid JSON it will have been auto-parsed and the
        # result dict already contains the diagnosis fields.
        result_data = exec_result.result or {}
        if "text" in result_data:
            raw_text = result_data["text"]
        else:
            # Re-serialise so _parse_llm_response can JSON-decode it uniformly.
            raw_text = json.dumps(result_data)

        diagnosis = self._parse_llm_response(raw_text)

        # Attach model metadata from the executor result.
        final = DiagnosisResult(
            failure_class=diagnosis.failure_class,
            remediation=diagnosis.remediation,
            confidence=diagnosis.confidence,
            explanation=diagnosis.explanation,
            model_used=exec_result.model_used,
            tokens_consumed=exec_result.tokens_consumed,
        )

        self._db.insert_diagnosis(final.to_db_dict(run_id))

        # Track failure pattern per template for systemic-failure detection (Issue #3.1.3).
        if template_id:
            try:
                tracker = FailurePatternTracker(db=self._db)
                tracker.track(
                    template_id=template_id,
                    failure_class=final.failure_class.value,
                    error_message=error_message or "",
                )
            except Exception as exc:
                _logger.warning(
                    "FailurePatternTracker.track failed (non-fatal): %s", exc
                )

        return final


# ---------------------------------------------------------------------------
# Failure pattern tracking (Issue #3.1.3)
# ---------------------------------------------------------------------------

# Regex patterns used to normalise error messages before hashing.
# Stripping these volatile tokens ensures that two errors with the same
# root cause but different paths, IDs, or addresses hash to the same bucket.
_NORMALISE_PATTERNS: list[tuple[str, str]] = [
    # UUIDs (e.g. run-abc123-def456)
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>"),
    # Hex addresses / hashes (8+ hex digits)
    (r"\b[0-9a-fA-F]{8,}\b", "<hex>"),
    # File-system paths
    (r"(/[\w.\-]+)+", "<path>"),
    # Windows-style paths
    (r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]*", "<path>"),
    # Bare integers (line numbers, ports, etc.)
    (r"\b\d{2,}\b", "<int>"),
]


def _normalise_error(error_message: str) -> str:
    """Return a normalised, lower-cased version of *error_message*.

    Strips volatile tokens (UUIDs, paths, hex addresses, large integers) so
    that semantically identical errors from different runs hash consistently.

    Args:
        error_message: Raw error string from a failed pipeline run.

    Returns:
        Normalised string suitable for stable hashing.
    """
    msg = error_message.lower().strip()
    for pattern, replacement in _NORMALISE_PATTERNS:
        msg = re.sub(pattern, replacement, msg)
    # Collapse runs of whitespace for stability
    return re.sub(r"\s+", " ", msg)


class FailurePatternTracker:
    """Tracks recurring failure signatures per template (Issue #3.1.3).

    Each call to :meth:`track` normalises the error message, derives a
    stable SHA-256 hash, and upserts a record in the ``failure_patterns``
    table via :meth:`Database.insert_or_update_failure_pattern`.

    A pattern is flagged as **systemic** when the same (hash, template_id)
    pair accumulates more than :attr:`SYSTEMIC_THRESHOLD` occurrences within
    :attr:`SYSTEMIC_WINDOW_DAYS` days.  A warning is logged whenever a
    pattern crosses into systemic territory.

    Usage::

        tracker = FailurePatternTracker(db=db)
        record = tracker.track(
            template_id="coding-pipeline-v1",
            failure_class="timeout",
            error_message="Phase 'build' timed out after 600s",
        )
        if record.get("is_systemic"):
            alert_operator(record)
    """

    #: Minimum occurrence count within the window to flag a pattern as systemic.
    SYSTEMIC_THRESHOLD: int = 3
    #: Sliding window (days) within which occurrences must cluster to be systemic.
    SYSTEMIC_WINDOW_DAYS: int = 7

    def __init__(self, db: Any) -> None:
        """Initialise the tracker.

        Args:
            db: A :class:`~orchestration_engine.db.Database` instance used to
                persist and query failure pattern records.
        """
        self._db = db
        self._logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def track(
        self,
        template_id: str,
        failure_class: str,
        error_message: str,
    ) -> Dict[str, Any]:
        """Record an occurrence of a failure pattern and return the upserted row.

        The error message is normalised and hashed before being stored so that
        minor variations (different paths, run IDs, line numbers) are treated
        as the same pattern.

        Args:
            template_id:   Identifier of the pipeline template that failed.
            failure_class: String value of the :class:`FailureClass` for this
                           failure (e.g. ``"timeout"``, ``"infra_issue"``).
            error_message: Raw error message from the failed pipeline run.

        Returns:
            The upserted ``failure_patterns`` row as a dict.  Key fields:

            * ``pattern_hash`` — SHA-256 of the normalised message.
            * ``occurrence_count`` — total occurrences so far.
            * ``is_systemic`` — ``1`` if the pattern is considered systemic.
        """
        normalised = _normalise_error(error_message)
        pattern_hash = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
        now_iso = datetime.now(timezone.utc).isoformat()

        record = self._db.insert_or_update_failure_pattern(
            pattern_hash=pattern_hash,
            template_id=template_id,
            failure_class=failure_class,
            now_iso=now_iso,
            systemic_threshold=self.SYSTEMIC_THRESHOLD,
            systemic_window_days=self.SYSTEMIC_WINDOW_DAYS,
        )

        if record.get("is_systemic"):
            self._logger.warning(
                "Systemic failure detected — template=%s  class=%s  "
                "occurrences=%s  hash=%s",
                template_id,
                failure_class,
                record.get("occurrence_count"),
                pattern_hash[:12],
            )

        return record
