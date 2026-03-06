"""Composite confidence scoring for completed pipeline runs (Issue #331.1).

This module provides run-level quality assessment via a multi-signal,
weighted composite score.  It is **distinct** from the
:class:`~orchestration_engine.schemas.ConfidenceLevel` enum defined in
``schemas.py``, which is a 5-tier enum used for individual task results.

The :class:`ConfidenceLevel` defined here is a 3-tier enum (HIGH/MEDIUM/LOW)
scoped to pipeline runs, with different thresholds:

- ``HIGH``   — composite_score >= 0.90
- ``MEDIUM`` — composite_score >= 0.75
- ``LOW``    — composite_score  < 0.75

Typical usage::

    from pathlib import Path
    from orchestration_engine.confidence import ConfidenceCalculator

    calc = ConfidenceCalculator()
    result = calc.compute_confidence(Path("output/my-run-dir"))
    print(result.confidence_level, result.composite_score)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_THRESHOLD_HIGH = 0.90
_THRESHOLD_MEDIUM = 0.75


# ---------------------------------------------------------------------------
# ConfidenceLevel (3-tier, run-scoped)
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    """Run-level confidence tier.

    Purposefully distinct from ``schemas.ConfidenceLevel`` (5-tier,
    task-scoped).  Do not conflate the two.
    """

    HIGH = "high"      # composite_score >= 0.90
    MEDIUM = "medium"  # composite_score >= 0.75
    LOW = "low"        # composite_score  < 0.75


def _score_to_level(score: float) -> ConfidenceLevel:
    """Map a composite score (0.0–1.0) to a :class:`ConfidenceLevel`."""
    if score >= _THRESHOLD_HIGH:
        return ConfidenceLevel.HIGH
    if score >= _THRESHOLD_MEDIUM:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


# ---------------------------------------------------------------------------
# ConfidenceSignal
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceSignal:
    """A single extracted quality signal with its weight and raw data.

    Attributes:
        name:       Signal identifier (e.g. ``"llm_judge"``).
        value:      Normalized score in ``[0.0, 1.0]`` (clamped on init).
        weight:     Contribution weight (must be >= 0).
        raw_value:  Original value before normalization.
        source:     Human-readable description of the data source
                    (e.g. ``"review.json"``).
    """

    name: str
    value: float
    weight: float
    raw_value: Any
    source: str

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(
                f"Signal '{self.name}': weight must be >= 0, got {self.weight}"
            )
        # Clamp value to [0.0, 1.0]
        self.value = max(0.0, min(1.0, float(self.value)))


# ---------------------------------------------------------------------------
# ConfidenceResult
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceResult:
    """The output of :meth:`ConfidenceCalculator.compute_confidence`.

    Attributes:
        signals:          All extracted :class:`ConfidenceSignal` objects.
        composite_score:  Weighted average of signal values in ``[0.0, 1.0]``.
        confidence_level: :class:`ConfidenceLevel` derived from the score.
        explanation:      Human-readable summary of the scoring.
    """

    signals: List[ConfidenceSignal]
    composite_score: float
    confidence_level: ConfidenceLevel
    explanation: str


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: Dict[str, float] = {
    "llm_judge": 0.4,
    "test_pass_rate": 0.3,
    "review_quality": 0.2,
    "change_complexity": 0.1,
}


# ---------------------------------------------------------------------------
# ConfidenceCalculator
# ---------------------------------------------------------------------------

class ConfidenceCalculator:
    """Computes a weighted composite confidence score for a pipeline run.

    Extracts up to four signals from the ``output/<run-id>/`` directory tree:

    1. **llm_judge** (weight 0.4) — Average confidence of any judge/review
       task results found in the directory.
    2. **test_pass_rate** (weight 0.3) — Fraction of non-review task results
       whose ``state == "success"``.
    3. **review_quality** (weight 0.2) — Average ``confidence`` value from
       all task result JSON files.
    4. **change_complexity** (weight 0.1) — Inverse complexity proxy: more
       task files → lower raw complexity bonus.  Specifically,
       ``1 / (1 + n_tasks)`` where *n_tasks* is the number of non-prefixed
       JSON files.

    Signals with no data are omitted; the composite is a weighted mean of
    the *present* signals (weights are renormalized if any are absent).

    Args:
        weights: Optional override dict merged on top of :data:`DEFAULT_WEIGHTS`.
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        self.weights: Dict[str, float] = {**DEFAULT_WEIGHTS, **(weights or {})}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_task_results(self, output_dir: Path) -> List[Dict[str, Any]]:
        """Load all task-result JSON files from *output_dir*.

        Skips files whose names start with ``_`` (meta files like
        ``_final_output.json``).
        """
        results: List[Dict[str, Any]] = []
        for json_file in sorted(output_dir.glob("*.json")):
            if json_file.name.startswith("_"):
                continue
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict):
                    data["_source_file"] = json_file.name
                    results.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read %s: %s", json_file, exc)
        return results

    def _extract_llm_judge(
        self, task_results: List[Dict[str, Any]]
    ) -> Optional[ConfidenceSignal]:
        """LLM judge signal: average confidence of review/judge tasks."""
        judge_results = [
            r
            for r in task_results
            if r.get("task_type") in ("review", "judge")
            or "judge" in r.get("_source_file", "")
            or "review" in r.get("_source_file", "")
        ]
        if not judge_results:
            return None

        scores = [
            float(r["confidence"])
            for r in judge_results
            if isinstance(r.get("confidence"), (int, float))
        ]
        if not scores:
            return None

        avg = sum(scores) / len(scores)
        sources = ", ".join(r["_source_file"] for r in judge_results)
        return ConfidenceSignal(
            name="llm_judge",
            value=avg,
            weight=self.weights.get("llm_judge", DEFAULT_WEIGHTS["llm_judge"]),
            raw_value=scores,
            source=sources,
        )

    def _extract_test_pass_rate(
        self, task_results: List[Dict[str, Any]]
    ) -> Optional[ConfidenceSignal]:
        """Test pass rate signal: success fraction of non-review tasks."""
        non_review = [
            r
            for r in task_results
            if r.get("task_type") not in ("review", "judge")
            and "judge" not in r.get("_source_file", "")
            and "review" not in r.get("_source_file", "")
        ]
        if not non_review:
            return None

        successes = sum(
            1 for r in non_review if r.get("state") == "success"
        )
        rate = successes / len(non_review)
        return ConfidenceSignal(
            name="test_pass_rate",
            value=rate,
            weight=self.weights.get(
                "test_pass_rate", DEFAULT_WEIGHTS["test_pass_rate"]
            ),
            raw_value={"successes": successes, "total": len(non_review)},
            source=f"{len(non_review)} task result(s)",
        )

    def _extract_review_quality(
        self, task_results: List[Dict[str, Any]]
    ) -> Optional[ConfidenceSignal]:
        """Review quality signal: average confidence across *all* tasks."""
        scores = [
            float(r["confidence"])
            for r in task_results
            if isinstance(r.get("confidence"), (int, float))
        ]
        if not scores:
            return None

        avg = sum(scores) / len(scores)
        return ConfidenceSignal(
            name="review_quality",
            value=avg,
            weight=self.weights.get(
                "review_quality", DEFAULT_WEIGHTS["review_quality"]
            ),
            raw_value=scores,
            source=f"{len(scores)} task result(s) with confidence scores",
        )

    def _extract_change_complexity(
        self, task_results: List[Dict[str, Any]]
    ) -> Optional[ConfidenceSignal]:
        """Complexity signal: inverse proxy — fewer tasks → higher score.

        Formula: ``1 / (1 + n_tasks)`` where *n_tasks* is the number of
        task result files loaded.  A run with a single phase scores 0.5;
        10 phases score ~0.09.
        """
        n = len(task_results)
        if n == 0:
            return None

        complexity_score = 1.0 / (1.0 + n)
        return ConfidenceSignal(
            name="change_complexity",
            value=complexity_score,
            weight=self.weights.get(
                "change_complexity", DEFAULT_WEIGHTS["change_complexity"]
            ),
            raw_value=n,
            source=f"{n} task file(s) in output directory",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_confidence(self, output_dir: Path) -> ConfidenceResult:
        """Compute a composite confidence score for a completed pipeline run.

        Args:
            output_dir: Path to the run's output directory (must exist).

        Returns:
            A :class:`ConfidenceResult` with all signals, composite score,
            confidence level, and explanation.

        Raises:
            ValueError: If *output_dir* does not exist.
        """
        output_dir = Path(output_dir)
        if not output_dir.exists():
            raise ValueError(f"output_dir does not exist: {output_dir}")

        task_results = self._load_task_results(output_dir)

        # Extract candidate signals (may be None if data is absent)
        candidates = [
            self._extract_llm_judge(task_results),
            self._extract_test_pass_rate(task_results),
            self._extract_review_quality(task_results),
            self._extract_change_complexity(task_results),
        ]
        signals: List[ConfidenceSignal] = [s for s in candidates if s is not None]

        if not signals:
            # No usable data — return low confidence with zero score
            return ConfidenceResult(
                signals=[],
                composite_score=0.0,
                confidence_level=ConfidenceLevel.LOW,
                explanation=(
                    "No task results found in output directory; "
                    "confidence cannot be determined."
                ),
            )

        # Compute weighted mean (renormalize weights of present signals)
        total_weight = sum(s.weight for s in signals)
        if total_weight == 0.0:
            composite = 0.0
        else:
            composite = sum(s.value * s.weight for s in signals) / total_weight

        composite = max(0.0, min(1.0, composite))
        level = _score_to_level(composite)

        # Build human-readable explanation
        signal_lines = ", ".join(
            f"{s.name}={s.value:.2f}(w={s.weight})" for s in signals
        )
        explanation = (
            f"Composite score {composite:.3f} → {level.value.upper()}. "
            f"Signals: [{signal_lines}]. "
            f"Effective weight sum: {total_weight:.2f}."
        )

        return ConfidenceResult(
            signals=signals,
            composite_score=composite,
            confidence_level=level,
            explanation=explanation,
        )
