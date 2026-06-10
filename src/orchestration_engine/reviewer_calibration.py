"""Reviewer calibration — longitudinal accuracy tracking (Issue #4.1.5).

This module provides :class:`ReviewerCalibrator` and :class:`CalibrationMetrics`,
which compute per-model accuracy statistics from a list of ``ReviewOutcome``
dicts (as returned by ``db.list_review_outcomes()``).

The calibrator answers:
  - How often does a model APPROVE runs that later prove problematic?
  - How often are its REQUEST_CHANGES validated by a verified fix?
  - What is its overall accuracy across both decision types?

These metrics feed into reviewer weighting in composite scoring.

Metric definitions
------------------
``approve_accuracy``
    Fraction of APPROVE verdicts where no subsequent issue was found
    (i.e. the approval was "held up" — no fix needed after the fact).
    ``approve_held_up_count / approve_count``

``request_changes_accuracy``
    Fraction of REQUEST_CHANGES verdicts where the fix was later verified
    (``fix_verified=True``), confirming the issue was real.
    ``request_changes_valid_count / request_changes_count``

``overall_accuracy``
    Combined accuracy over all reviews.
    ``(approve_held_up_count + request_changes_valid_count) / total_reviews``

Edge cases
----------
When the denominator for a rate is zero the metric is returned as ``None``
(not ``0.0``, which would misrepresent "no data" as "always wrong").

Empty histories
---------------
When no outcomes exist for a model, :meth:`ReviewerCalibrator.compute` returns
a :class:`CalibrationMetrics` with all counts at zero and all accuracy fields
``None``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------


@dataclass
class CalibrationMetrics:
    """Computed accuracy metrics for a single reviewer model.

    Attributes:
        reviewer_model:               Model name/tier (e.g. ``"opus"``).
        total_reviews:                Total number of review outcomes observed.
        approve_count:                Number of APPROVE verdicts.
        request_changes_count:        Number of REQUEST_CHANGES verdicts.
        approve_held_up_count:        APPROVE verdicts where no fix was later
                                      needed (``fix_verified=False``).
        request_changes_valid_count:  REQUEST_CHANGES verdicts confirmed real
                                      by a subsequent verified fix
                                      (``fix_verified=True``).
        approve_accuracy:             ``approve_held_up_count / approve_count``
                                      or ``None`` when no APPROVEs observed.
        request_changes_accuracy:     ``request_changes_valid_count /
                                      request_changes_count`` or ``None``.
        overall_accuracy:             Combined accuracy or ``None`` when no
                                      reviews observed.
        computed_at:                  UTC ISO-8601 timestamp of computation.
        aggregation_window:           Optional label for the time window used
                                      (e.g. ``"30d"``).  Purely informational.
    """

    reviewer_model: str
    total_reviews: int = 0
    approve_count: int = 0
    request_changes_count: int = 0
    approve_held_up_count: int = 0
    request_changes_valid_count: int = 0
    approve_accuracy: Optional[float] = None
    request_changes_accuracy: Optional[float] = None
    overall_accuracy: Optional[float] = None
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    aggregation_window: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation suitable for DB insertion."""
        return {
            "reviewer_model":              self.reviewer_model,
            "total_reviews":               self.total_reviews,
            "approve_count":               self.approve_count,
            "request_changes_count":       self.request_changes_count,
            "approve_held_up_count":       self.approve_held_up_count,
            "request_changes_valid_count": self.request_changes_valid_count,
            "approve_accuracy":            self.approve_accuracy,
            "request_changes_accuracy":    self.request_changes_accuracy,
            "overall_accuracy":            self.overall_accuracy,
            "computed_at":                 self.computed_at,
            "aggregation_window":          self.aggregation_window,
        }


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


class ReviewerCalibrator:
    """Compute per-model calibration metrics from historical review outcomes.

    Reads a flat list of ``ReviewOutcome`` dicts, groups them by
    ``reviewer_model``, and computes :class:`CalibrationMetrics` for each
    model.  Optionally persists the computed snapshots to a
    :class:`~db.Database` instance.

    Args:
        db: Optional :class:`~db.Database` instance.  When provided, calling
            :meth:`calibrate_and_save` will persist each snapshot via
            :meth:`~db.Database.insert_calibration_snapshot`.
        aggregation_window: Optional label for the time window represented
                            by the supplied outcomes (e.g. ``"30d"``).
                            Stored as-is in :class:`CalibrationMetrics`.

    Example::

        outcomes = db.list_review_outcomes(limit=500)
        calibrator = ReviewerCalibrator(db=db, aggregation_window="all-time")
        metrics_map = calibrator.calibrate_and_save(outcomes)
        for model, m in metrics_map.items():
            print(model, m.overall_accuracy)
    """

    def __init__(
        self,
        db: "Optional[Database]" = None,
        aggregation_window: Optional[str] = None,
    ) -> None:
        self._db = db
        self.aggregation_window = aggregation_window

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        outcomes: List[Dict[str, Any]],
    ) -> Dict[str, CalibrationMetrics]:
        """Compute calibration metrics grouped by reviewer model.

        Args:
            outcomes: List of review outcome dicts.  Each dict should contain:
                      - ``"reviewer_model"`` (str | None)
                      - ``"verdict"`` (str | None) — ``"APPROVE"`` or
                        ``"REQUEST_CHANGES"``
                      - ``"fix_verified"`` (bool | int) — whether the
                        subsequent fix was verified

        Returns:
            Mapping of ``reviewer_model → CalibrationMetrics``.  Outcomes
            whose ``reviewer_model`` is ``None`` or empty are grouped under
            the key ``"unknown"``.
        """
        # Group outcomes by model
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for outcome in outcomes:
            model = outcome.get("reviewer_model") or "unknown"
            grouped.setdefault(model, []).append(outcome)

        result: Dict[str, CalibrationMetrics] = {}
        for model, model_outcomes in grouped.items():
            result[model] = self._compute_for_model(model, model_outcomes)
        return result

    def calibrate_and_save(
        self,
        outcomes: List[Dict[str, Any]],
    ) -> Dict[str, CalibrationMetrics]:
        """Compute metrics and persist snapshots to the database.

        Equivalent to calling :meth:`compute` and then persisting each
        resulting :class:`CalibrationMetrics` snapshot via
        :meth:`~db.Database.insert_calibration_snapshot`.  When no
        :class:`~db.Database` was supplied at construction time, this method
        behaves identically to :meth:`compute` (no persistence).

        Args:
            outcomes: See :meth:`compute`.

        Returns:
            Same mapping as :meth:`compute`.
        """
        metrics_map = self.compute(outcomes)
        if self._db is not None:
            for metrics in metrics_map.values():
                try:
                    self._db.insert_calibration_snapshot(metrics.to_dict())
                except Exception:
                    logger.exception(
                        "Failed to persist calibration snapshot for model %r",
                        metrics.reviewer_model,
                    )
        return metrics_map

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_for_model(
        self,
        reviewer_model: str,
        outcomes: List[Dict[str, Any]],
    ) -> CalibrationMetrics:
        """Compute :class:`CalibrationMetrics` for a single model."""
        total_reviews = len(outcomes)
        approve_count = 0
        request_changes_count = 0
        approve_held_up_count = 0
        request_changes_valid_count = 0

        for o in outcomes:
            verdict = (o.get("verdict") or "").upper()
            fix_verified = bool(o.get("fix_verified"))

            if verdict == "APPROVE":
                approve_count += 1
                # Approval is "held up" when no fix was needed (fix_verified=False)
                if not fix_verified:
                    approve_held_up_count += 1
            elif verdict == "REQUEST_CHANGES":
                request_changes_count += 1
                # Request-changes is valid when the fix was actually verified
                if fix_verified:
                    request_changes_valid_count += 1

        # Compute rates — None when denominator is zero
        approve_accuracy: Optional[float] = (
            approve_held_up_count / approve_count
            if approve_count > 0
            else None
        )
        request_changes_accuracy: Optional[float] = (
            request_changes_valid_count / request_changes_count
            if request_changes_count > 0
            else None
        )
        overall_accuracy: Optional[float] = (
            (approve_held_up_count + request_changes_valid_count) / total_reviews
            if total_reviews > 0
            else None
        )

        return CalibrationMetrics(
            reviewer_model=reviewer_model,
            total_reviews=total_reviews,
            approve_count=approve_count,
            request_changes_count=request_changes_count,
            approve_held_up_count=approve_held_up_count,
            request_changes_valid_count=request_changes_valid_count,
            approve_accuracy=approve_accuracy,
            request_changes_accuracy=request_changes_accuracy,
            overall_accuracy=overall_accuracy,
            aggregation_window=self.aggregation_window,
        )
