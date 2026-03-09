"""Composite confidence scoring model for orchestration pipeline runs.

This module provides a 3-tier ConfidenceLevel enum (HIGH/MEDIUM/LOW) and a
ConfidenceCalculator that aggregates multiple signals from task result JSON
files in a pipeline output directory into a single composite score.

Signal sources (derived from task result files):
    llm_judge              – Average confidence of review/judge task types.
    test_pass_rate         – Ratio of non-review tasks whose state == "success".
    review_quality         – Average confidence across ALL tasks.
    change_complexity      – Inverse of task count: 1 / (1 + num_task_files).
    review_catch_value     – Value delivered by review phase (verified fixes,
                             severity-weighted catch rate, false-positive
                             penalty).  Only included when review_outcomes are
                             provided.  See :mod:`~orchestration_engine.review_catch_value`.
    adversarial_audit      – Fraction of audit issues that the original reviewer
                             also caught (reviewer_accuracy_score from
                             AuditPhase).  Only included when audit_results are
                             provided.  See :mod:`~orchestration_engine.audit`.
    historical_calibration – Longitudinal reviewer accuracy from
                             ReviewerCalibrator.  Only included when passed via
                             the ``extra_signals`` parameter.

Two weight tables are provided:
    DEFAULT_WEIGHTS    – v1 weights for backward compatibility.
    DEFAULT_WEIGHTS_V2 – v2 weights that give equal prominence to
                         ``llm_judge`` and ``test_pass_rate``, and introduce
                         the ``historical_calibration`` signal.

Extra signals:
    Callers may inject pre-computed :class:`ConfidenceSignal` instances (e.g.
    a ``historical_calibration`` signal produced by
    :class:`~reviewer_calibration.ReviewerCalibrator`) via the ``extra_signals``
    parameter of :meth:`ConfidenceCalculator.compute_confidence`.  These are
    appended to the standard signals before the weighted average is computed.

Signal sources (derived from ReviewOutcome DB records — Issue #4.1.3):
    review_catch_value – Normalised score reflecting how much real value the
                         review phase delivered: fix verification rate,
                         severity-weighted catch rate, and false-positive
                         penalty.  Only included when ``review_outcomes`` is
                         supplied to ``compute_confidence``.

NOTE: This ConfidenceLevel is distinct from schemas.ConfidenceLevel, which is
a 5-tier (VERY_LOW->VERY_HIGH) enum scoped to individual task results. This
module's ConfidenceLevel is scoped to a full pipeline run and maps to three
coarse bands used for downstream routing and reporting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Imported only for type checking to avoid circular imports at runtime.
    # ReviewCatchValueCalculator is imported lazily inside compute_confidence.
    from .review_catch_value import ReviewCatchValueCalculator  # noqa: F401
    # ReviewerCalibrator and AuditResult are imported lazily inside compute_confidence.
    from .reviewer_calibration import ReviewerCalibrator  # noqa: F401
    from .audit import AuditResult  # noqa: F401

# ---------------------------------------------------------------------------
# Routing thresholds (authoritative source — referenced by routing.py)
# Issue #429.1: centralised here to avoid duplication with routing.py hardcodes.
# ---------------------------------------------------------------------------
AUTO_MERGE_THRESHOLD: float = 0.90    # ConfidenceLevel.HIGH boundary
HUMAN_REVIEW_THRESHOLD: float = 0.70  # Lowered from 0.75 post-calibration (Issue #429.1)

# ---------------------------------------------------------------------------
# Default signal weights (v1 — sum to 1.0 for the six standard signals)
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "llm_judge": 0.30,              # Updated in Issue #4.1.4: 0.35 → 0.30
    "test_pass_rate": 0.20,         # Updated in Issue #4.1.4: 0.25 → 0.20
    "review_quality": 0.15,
    "change_complexity": 0.10,
    "review_catch_value": 0.15,     # Issue #4.1.3
    "adversarial_audit": 0.10,      # Issue #4.1.4 — renamed from audit_catch_rate
    "historical_calibration": 0.05, # Issue #4.1.6 — only active via extra_signals
}

# ---------------------------------------------------------------------------
# v2 signal weights (Issue #4.1.6)
# Equal llm_judge / test_pass_rate split; adds historical_calibration signal.
# Weights are renormalised during aggregation based on which signals are present,
# so it is safe for the table to sum to 1.0 across all *possible* signals even
# though not all are always emitted.
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS_V2: dict[str, float] = {
    "llm_judge": 0.25,              # Issue #4.1.6: equal weight with test_pass_rate
    "test_pass_rate": 0.25,         # Issue #4.1.6: raised from 0.20
    "review_catch_value": 0.20,     # Issue #4.1.6: raised from 0.15
    "adversarial_audit": 0.15,      # Issue #4.1.6: raised from 0.10
    "change_complexity": 0.10,      # Issue #4.1.6: unchanged
    "historical_calibration": 0.05, # Issue #4.1.6: new signal via extra_signals
}

# ---------------------------------------------------------------------------
# v2 signal weights — calibrated with Sprint 1-4 data (Issue #429.1)
#
# Rationale for each weight:
#   llm_judge (0.40):           ↑ Primary quality discriminator. Rubric scores
#                                 from LLMJudgeGrader are the most accurate
#                                 measure of output quality (0.97+ on good runs).
#                                 Raised from 0.25 (Issue #4.1.6).
#   test_pass_rate (0.30):      ↑ Binary reliability signal — very trustworthy
#                                 and deterministic. Raised from 0.25.
#   review_catch_value (0.12):  ↓ Often absent in coding pipeline runs (no
#                                 ReviewOutcome records). Lowered from 0.20.
#   adversarial_audit (0.08):   ↓ Rarely present in Sprint 1-4 pipeline data.
#                                 Lowered from 0.15.
#   review_quality (0.06):      ↑ Restored: was dropped from v2 but still emits,
#                                 causing undocumented fallback to v1 weight 0.15.
#                                 Explicitly re-added at reduced weight.
#   change_complexity (0.02):   ↓ Sprint 1-4 analysis shows task count is
#                                 anti-correlated with quality: larger pipelines
#                                 with more tasks still produce high-quality output.
#                                 1/(1+N) for N=5–10 tasks gives 0.09–0.17, which
#                                 dragged the composite ~0.06–0.08 points below
#                                 the true quality. Drastically reduced.
#   historical_calibration (0.02): Unchanged: extra_signals only.
#
# Weights sum to 1.00; _weighted_average renormalises over present signals.
# Routing thresholds (AUTO_MERGE_THRESHOLD / HUMAN_REVIEW_THRESHOLD) are
# defined above and should be updated in lock-step when this table changes.
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS_V2: dict[str, float] = {
    "llm_judge": 0.40,              # ↑ Primary signal: rubric/review scores most discriminative
    "test_pass_rate": 0.30,         # ↑ Binary reliability signal — very trustworthy
    "review_catch_value": 0.12,     # ↓ Reduced: often absent in coding pipeline runs
    "adversarial_audit": 0.08,      # ↓ Reduced: rarely present in Sprint 1-4
    "review_quality": 0.06,         # ↑ Restored: documents v2 fallback behaviour
    "change_complexity": 0.02,      # ↓ Heavily reduced: task count ≠ quality indicator
    "historical_calibration": 0.02, # Unchanged: extra_signals only
}
# Note: weights sum to 1.00; renormalisation in _weighted_average handles absent signals.


# ---------------------------------------------------------------------------
# Enums & data structures
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    """3-tier confidence level for a full pipeline run.

    Distinct from ``schemas.ConfidenceLevel`` (5-tier, task-scoped).

    Thresholds:
        HIGH   >= 0.90
        MEDIUM >= 0.75
        LOW     < 0.75
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _score_to_level(score: float) -> ConfidenceLevel:
    """Map a composite score in [0, 1] to a coarse ConfidenceLevel.

    Args:
        score: Composite confidence score in the range [0.0, 1.0].

    Returns:
        ConfidenceLevel.HIGH   if score >= 0.90
        ConfidenceLevel.MEDIUM if score >= 0.75
        ConfidenceLevel.LOW    otherwise
    """
    if score >= 0.90:
        return ConfidenceLevel.HIGH
    if score >= 0.75:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


@dataclass
class ConfidenceSignal:
    """A single scored signal contributing to the composite confidence.

    Attributes:
        name:      Logical name of the signal (e.g. "llm_judge").
        value:     Normalised score in [0, 1] — clamped automatically.
        weight:    Non-negative weight used in the weighted average.
        raw_value: The original, un-normalised value before clamping/mapping.
        source:    Human-readable origin description.
    """

    name: str
    value: float
    weight: float
    raw_value: Any
    source: str

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(
                f"Signal '{self.name}' weight must be >= 0, got {self.weight}"
            )
        # Clamp value to [0, 1]
        self.value = max(0.0, min(1.0, float(self.value)))


@dataclass
class ConfidenceResult:
    """Aggregated confidence result for a pipeline run.

    Attributes:
        signals:          All signals that were successfully extracted.
        composite_score:  Weighted average of signal values in [0, 1].
        confidence_level: Coarse tier mapped from composite_score.
        explanation:      Human-readable breakdown of contributing signals.
    """

    signals: list[ConfidenceSignal] = field(default_factory=list)
    composite_score: float = 0.0
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    explanation: str = ""


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

class ConfidenceCalculator:
    """Computes composite confidence from task result JSON files in an output dir.

    The calculator reads all non-meta (non-underscore-prefixed) ``*.json`` files
    from the given directory, parses them as task results, and derives up to
    five signals:

    - ``llm_judge``         — average confidence of review/judge task types.
    - ``test_pass_rate``    — ratio of non-review tasks with state "success".
    - ``review_quality``    — average confidence across ALL tasks.
    - ``change_complexity`` — inverse of task count: 1 / (1 + num_task_files).
    - ``review_catch_value``— value delivered by the review phase (Issue #4.1.3).
      Only included when ``review_outcomes`` is supplied to
      ``compute_confidence``.

    Args:
        weights: Optional override dict merged with DEFAULT_WEIGHTS.
                 Unknown keys extend the weight table; known keys override.
    """

    def __init__(self, weights: Optional[dict[str, float]] = None) -> None:
        self.weights: dict[str, float] = {**DEFAULT_WEIGHTS}
        if weights:
            self.weights.update(weights)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_confidence(
        self,
        output_dir: Path,
        review_outcomes: Optional[list[dict[str, Any]]] = None,
        audit_results: Optional[list[dict[str, Any]]] = None,
        calibration_outcomes: Optional[list[dict[str, Any]]] = None,
        extra_signals: Optional[list["ConfidenceSignal"]] = None,
    ) -> ConfidenceResult:
        """Aggregate signals from task result files in *output_dir*.

        When *review_outcomes* is a non-empty list of ReviewOutcome dicts (as
        returned by ``db.get_review_outcomes_for_run()``), a
        ``review_catch_value`` signal is computed and added to the composite
        score.  If *review_outcomes* is ``None`` or an empty list the signal is
        omitted and the remaining signals are re-normalised accordingly.

        When *audit_results* is a non-empty list of AuditResult dicts (as
        returned by ``AuditResult.to_dict()``), an ``adversarial_audit`` signal
        is computed from the average ``reviewer_accuracy_score`` across all
        provided audit results.  If *audit_results* is ``None`` or empty the
        signal is omitted.

        When *calibration_outcomes* is a non-empty list of ReviewOutcome dicts
        (typically from ``db.list_review_outcomes(limit=500)``), dynamic per-model
        accuracy weights are computed via :class:`~reviewer_calibration.ReviewerCalibrator`
        and the ``llm_judge`` weight is scaled up/down based on the primary
        reviewer model's ``overall_accuracy``.  Weights are re-normalised to
        sum to 1.0.  When *calibration_outcomes* is ``None`` or empty, static
        ``DEFAULT_WEIGHTS`` are used unchanged.

        When *extra_signals* is a non-empty list of pre-computed
        :class:`ConfidenceSignal` instances, they are appended to the standard
        signal list before the weighted average is computed.  This allows
        callers to inject additional signals (e.g. a ``historical_calibration``
        signal produced by :class:`~reviewer_calibration.ReviewerCalibrator`)
        without modifying this method.  Each extra signal's ``weight`` is used
        as-is; the aggregation step re-normalises across all present signals.

        Args:
            output_dir:            Path to the pipeline output directory.
            review_outcomes:       Optional list of ReviewOutcome row dicts from
                                   the ``review_outcomes`` DB table (Issue #4.1.3).
            audit_results:         Optional list of AuditResult dicts (as returned
                                   by ``AuditResult.to_dict()``).  Used to compute
                                   the ``adversarial_audit`` signal (Issue #4.1.4).
            calibration_outcomes:  Optional list of ReviewOutcome dicts used to
                                   compute dynamic weights via ReviewerCalibrator.
                                   Typically the last 500 outcomes from the DB
                                   (Issue #4.1.5).
            extra_signals:         Optional list of pre-computed
                                   :class:`ConfidenceSignal` instances to append
                                   before computing the weighted average
                                   (Issue #4.1.6).

        Returns:
            A populated ConfidenceResult.

        Raises:
            ValueError: If *output_dir* does not exist.
        """
        if not output_dir.exists():
            raise ValueError(
                f"Output directory {output_dir} does not exist"
            )

        # ------------------------------------------------------------------
        # Dynamic weights via ReviewerCalibrator (Issue #4.1.5 / #4.1.6)
        # When calibration_outcomes are provided, adjust llm_judge weight
        # based on the primary reviewer model's longitudinal accuracy.
        # _eff_weights is a local copy so self.weights is never mutated;
        # a second call on the same instance always starts from the original
        # DEFAULT_WEIGHTS-derived weights, not from a previously scaled copy.
        # ------------------------------------------------------------------
        _eff_weights: dict[str, float] = dict(self.weights)
        if calibration_outcomes:
            try:
                from .reviewer_calibration import ReviewerCalibrator  # noqa: PLC0415
                _calibrator = ReviewerCalibrator()
                _metrics_map = _calibrator.compute(calibration_outcomes)
                _eff_weights = self._compute_dynamic_weights(_metrics_map)
            except Exception as _cal_exc:
                logger.warning(
                    "Dynamic weight calibration failed (falling back to static weights): %s",
                    _cal_exc,
                )

        # Collect all non-meta JSON files (skip files starting with "_")
        task_files = sorted(
            p for p in output_dir.glob("*.json")
            if not p.name.startswith("_")
        )

        # Parse each file as a task result dict
        tasks: list[tuple[str, dict]] = []
        for path in task_files:
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    tasks.append((path.name, data))
            except Exception:
                pass  # Malformed or unreadable files are silently skipped

        signals: list[ConfidenceSignal] = []

        if not tasks:
            return ConfidenceResult(
                signals=[],
                composite_score=0.0,
                confidence_level=ConfidenceLevel.LOW,
                explanation="No signals extracted -- defaulting to LOW.",
            )

        # Partition into review/judge tasks and regular (non-review) tasks
        review_tasks = [
            (f, d) for f, d in tasks if self._is_review_task(f, d)
        ]
        non_review_tasks = [
            (f, d) for f, d in tasks if not self._is_review_task(f, d)
        ]

        # ------------------------------------------------------------------
        # Signal: llm_judge
        # Average confidence of review/judge tasks.
        # Only emitted when at least one such task is present.
        # ------------------------------------------------------------------
        if review_tasks:
            confidences = [
                float(d["confidence"])
                for _, d in review_tasks
                if "confidence" in d
            ]
            if confidences:
                avg = sum(confidences) / len(confidences)
                signals.append(ConfidenceSignal(
                    name="llm_judge",
                    value=avg,
                    weight=_eff_weights.get("llm_judge", DEFAULT_WEIGHTS["llm_judge"]),
                    raw_value=confidences,
                    source=(
                        f"review/judge tasks: "
                        f"{[f for f, _ in review_tasks]}"
                    ),
                ))

        # ------------------------------------------------------------------
        # Signal: test_pass_rate
        # Ratio of non-review tasks with state == "success".
        # Only emitted when at least one non-review task is present.
        # ------------------------------------------------------------------
        if non_review_tasks:
            success_count = sum(
                1 for _, d in non_review_tasks if d.get("state") == "success"
            )
            rate = success_count / len(non_review_tasks)
            signals.append(ConfidenceSignal(
                name="test_pass_rate",
                value=rate,
                weight=_eff_weights.get(
                    "test_pass_rate", DEFAULT_WEIGHTS["test_pass_rate"]
                ),
                raw_value={"passed": success_count, "total": len(non_review_tasks)},
                source=(
                    f"{success_count}/{len(non_review_tasks)} "
                    f"non-review tasks succeeded"
                ),
            ))

        # ------------------------------------------------------------------
        # Signal: review_quality
        # Average confidence across ALL tasks (including review tasks).
        # Only emitted when at least one task has a confidence field.
        # ------------------------------------------------------------------
        all_confidences = [
            float(d["confidence"])
            for _, d in tasks
            if "confidence" in d
        ]
        if all_confidences:
            avg_all = sum(all_confidences) / len(all_confidences)
            signals.append(ConfidenceSignal(
                name="review_quality",
                value=avg_all,
                weight=_eff_weights.get(
                    "review_quality", DEFAULT_WEIGHTS["review_quality"]
                ),
                raw_value=all_confidences,
                source=f"average confidence over {len(all_confidences)} tasks",
            ))

        # ------------------------------------------------------------------
        # Signal: change_complexity
        # Inverse complexity: 1 / (1 + number_of_task_files).
        # Fewer files → higher score (lower complexity = more confidence).
        # ------------------------------------------------------------------
        num_tasks = len(tasks)
        complexity_score = 1.0 / (1.0 + num_tasks)
        signals.append(ConfidenceSignal(
            name="change_complexity",
            value=complexity_score,
            weight=_eff_weights.get(
                "change_complexity", DEFAULT_WEIGHTS["change_complexity"]
            ),
            raw_value=num_tasks,
            source=f"{num_tasks} task file(s) in output dir",
        ))

        # ------------------------------------------------------------------
        # Signal: review_catch_value  (Issue #4.1.3)
        # Only emitted when review_outcomes is a non-empty list.
        # Lazy import avoids circular dependency since ReviewCatchValueCalculator
        # imports ConfidenceSignal from this module.
        # ------------------------------------------------------------------
        if review_outcomes:
            from .review_catch_value import ReviewCatchValueCalculator  # noqa: PLC0415
            rcv_weight = _eff_weights.get(
                "review_catch_value", DEFAULT_WEIGHTS["review_catch_value"]
            )
            rcv_calc = ReviewCatchValueCalculator(weight=rcv_weight)
            signals.append(rcv_calc.compute(review_outcomes))

        # ------------------------------------------------------------------
        # Signal: adversarial_audit  (Issue #4.1.4 / #4.1.6)
        # Renamed from audit_catch_rate.  Only emitted when audit_results is
        # a non-empty list.  Computes the average reviewer_accuracy_score
        # across all audit results.
        # ------------------------------------------------------------------
        if audit_results:
            accuracy_scores = [
                float(r["reviewer_accuracy_score"])
                for r in audit_results
                if "reviewer_accuracy_score" in r
            ]
            if accuracy_scores:
                avg_accuracy = sum(accuracy_scores) / len(accuracy_scores)
                acr_weight = _eff_weights.get(
                    "adversarial_audit", DEFAULT_WEIGHTS["adversarial_audit"]
                )
                signals.append(ConfidenceSignal(
                    name="adversarial_audit",
                    value=avg_accuracy,
                    weight=acr_weight,
                    raw_value={
                        "reviewer_accuracy_scores": accuracy_scores,
                        "audit_count": len(accuracy_scores),
                    },
                    source=f"{len(accuracy_scores)} audit result(s)",
                ))

        # ------------------------------------------------------------------
        # Extra signals  (Issue #4.1.6)
        # Caller-provided pre-computed signals (e.g. historical_calibration).
        # Appended after all standard signals so they participate in the same
        # re-normalised weighted average without special-casing.
        # ------------------------------------------------------------------
        if extra_signals:
            signals.extend(extra_signals)
            logger.debug(
                "compute_confidence: appended %d extra signal(s): %s",
                len(extra_signals),
                [s.name for s in extra_signals],
            )

        composite = self._weighted_average(signals)
        level = _score_to_level(composite)
        explanation = self._build_explanation(signals, composite, level)

        return ConfidenceResult(
            signals=signals,
            composite_score=composite,
            confidence_level=level,
            explanation=explanation,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_review_task(filename: str, data: dict) -> bool:
        """Return True if the task is a review/judge task.

        A task is classified as review/judge when:
        - Its ``task_type`` field is "review" or "judge", OR
        - The filename contains the substring "review" (case-insensitive).
        """
        task_type = data.get("task_type", "")
        if task_type in ("review", "judge"):
            return True
        if "review" in filename.lower():
            return True
        return False

    def _weighted_average(self, signals: list[ConfidenceSignal]) -> float:
        """Compute renormalised weighted average over present signals.

        Uses each signal's own ``weight`` attribute, which is set at
        construction time from the effective (possibly calibration-adjusted)
        weight table.  This avoids coupling the aggregation step to the
        mutable ``self.weights`` dict.
        """
        if not signals:
            return 0.0

        total_weight = sum(s.weight for s in signals)
        if total_weight == 0.0:
            return sum(s.value for s in signals) / len(signals)

        return sum(s.value * s.weight for s in signals) / total_weight

    def _compute_dynamic_weights(
        self,
        metrics_map: dict[str, Any],
    ) -> dict[str, float]:
        """Compute dynamic signal weights adjusted by reviewer accuracy.

        Takes a ``metrics_map`` from :meth:`~reviewer_calibration.ReviewerCalibrator.compute`
        and returns an updated weights dict.  The primary reviewer model is
        determined by the model with the highest ``total_reviews`` count.

        The ``llm_judge`` weight is scaled between:
        - ``0.5 * base`` when ``overall_accuracy → 0`` (unreliable reviewer)
        - ``1.5 * base`` when ``overall_accuracy → 1`` (highly accurate reviewer)

        The weight delta (difference from the base weight) is redistributed
        proportionally across all other signals so the total weight always
        sums to 1.0.

        When no usable accuracy data is available, the original weights are
        returned unchanged.

        Args:
            metrics_map: Dict mapping ``reviewer_model → CalibrationMetrics``
                         as returned by :meth:`ReviewerCalibrator.compute`.

        Returns:
            A new weights dict with the same keys as ``self.weights``.
        """
        # Find primary model: one with most reviews and non-None overall_accuracy
        best_model = None
        best_reviews = 0
        for model_name, metrics in metrics_map.items():
            total = getattr(metrics, "total_reviews", 0) or 0
            accuracy = getattr(metrics, "overall_accuracy", None)
            if accuracy is not None and total > best_reviews:
                best_model = model_name
                best_reviews = total

        if best_model is None:
            # No usable calibration data — return unchanged weights
            return dict(self.weights)

        accuracy = metrics_map[best_model].overall_accuracy
        if accuracy is None:
            return dict(self.weights)

        # Scale llm_judge weight: 0.5x (accuracy=0) to 1.5x (accuracy=1)
        base_weight = self.weights.get("llm_judge", DEFAULT_WEIGHTS["llm_judge"])
        scaled_weight = base_weight * (0.5 + accuracy)  # accuracy in [0,1] → [0.5x, 1.5x]
        delta = scaled_weight - base_weight

        # Build new weights: start with current, apply delta to llm_judge
        new_weights = dict(self.weights)
        new_weights["llm_judge"] = scaled_weight

        # Redistribute the negative delta across all other signals proportionally
        other_keys = [k for k in new_weights if k != "llm_judge"]
        if not other_keys:
            return new_weights

        other_total = sum(new_weights[k] for k in other_keys)
        if other_total <= 0.0:
            return new_weights

        for k in other_keys:
            fraction = new_weights[k] / other_total
            new_weights[k] = max(0.0, new_weights[k] - delta * fraction)

        # Re-normalise to ensure sum == 1.0 despite floating-point drift
        total = sum(new_weights.values())
        if total > 0.0:
            for k in new_weights:
                new_weights[k] /= total

        return new_weights

    @staticmethod
    def _build_explanation(
        signals: list[ConfidenceSignal],
        composite: float,
        level: ConfidenceLevel,
    ) -> str:
        """Build a human-readable summary of contributing signals."""
        lines = [f"Composite score: {composite:.4f} -> {level.value.upper()}"]
        if signals:
            lines.append("Signals:")
            for s in signals:
                lines.append(
                    f"  [{s.name}] value={s.value:.4f}  "
                    f"weight={s.weight:.2f}  source={s.source}"
                )
        else:
            lines.append("No signals extracted -- defaulting to LOW.")
        return "\n".join(lines)
