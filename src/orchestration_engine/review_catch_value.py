"""Review catch-value signal for composite confidence scoring (Issue #387 / 4.1.3).

This module provides :class:`ReviewCatchValueCalculator`, which reads a list of
``ReviewOutcome`` dicts (as returned by ``db.get_review_outcomes_for_run()`` or
``db.list_review_outcomes()``) and produces a single normalised
:class:`~confidence.ConfidenceSignal` named ``"review_catch_value"``.

The signal answers: **did the review phase deliver real value?**  It combines
three sub-scores:

* ``fix_verification_rate`` — what fraction of reviewed runs had their fixes
  confirmed?
* ``weighted_catch_rate`` — what fraction of the *severity-weighted* issue
  mass was confirmed real (by a subsequent verified fix)?
* ``false_positive_penalty`` — were there contradictory outcomes (issues
  reported on an APPROVE verdict)?  A high false-positive rate drives the
  score down.

Composite formula::

    score = (
        0.40 * fix_verification_rate
        + 0.40 * weighted_catch_rate
        + 0.20 * (1.0 - false_positive_rate)
    )

The result is clamped to ``[0, 1]`` before being returned in a
:class:`~confidence.ConfidenceSignal`.

Empty outcomes
--------------
When ``outcomes`` is an empty list, :meth:`ReviewCatchValueCalculator.compute`
returns a **neutral** signal with ``value = 0.5`` rather than raising.  This
matches the ``weighted_catch_rate`` neutral value chosen when no issues are
found — "no data" is neither a good nor a bad outcome.

Module-level constant
---------------------
``SEVERITY_WEIGHTS``
    Maps issue severity labels (``"BLOCKER"``, ``"MAJOR"``, ``"MINOR"``,
    ``"NITPICK"``) to numeric weights used in ``weighted_catch_rate``.
    Unknown severities fall back to ``0.10`` (negligible but non-zero).
"""

from __future__ import annotations

from typing import Any

from .confidence import ConfidenceSignal

# ---------------------------------------------------------------------------
# Severity weight table
# ---------------------------------------------------------------------------

#: Maps issue severity → numeric weight for ``weighted_catch_rate``.
#: Unknown severities fall back to ``0.10``.
SEVERITY_WEIGHTS: dict[str, float] = {
    "BLOCKER": 1.00,
    "MAJOR": 0.75,
    "MINOR": 0.25,
    "NITPICK": 0.10,
}

# Internal sub-score weights (must sum to 1.0).
_W_FIX_VERIFICATION: float = 0.40
_W_CATCH_RATE: float = 0.40
_W_FP_PENALTY: float = 0.20

# Neutral score returned when there are no outcomes to evaluate.
_NEUTRAL_SCORE: float = 0.5


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class ReviewCatchValueCalculator:
    """Compute a ``"review_catch_value"`` :class:`~confidence.ConfidenceSignal`.

    Accepts a list of ReviewOutcome dicts (as returned by
    ``db.get_review_outcomes_for_run()``) and produces a single normalised
    signal reflecting how much real value the review phase delivered.

    Args:
        weight: The signal weight to embed in the returned
                :class:`~confidence.ConfidenceSignal`.  Must be >= 0.
                Should match the ``"review_catch_value"`` entry in
                ``DEFAULT_WEIGHTS``.  Defaults to ``0.15``.
        severity_weights: Optional override for :data:`SEVERITY_WEIGHTS`.
                          Merged on top of the module defaults; unknown keys
                          extend the table, known keys override.

    Raises:
        ValueError: If *weight* is negative.

    Example::

        outcomes = db.get_review_outcomes_for_run(run_id)
        calc = ReviewCatchValueCalculator(weight=0.15)
        signal = calc.compute(outcomes)
    """

    def __init__(
        self,
        weight: float = 0.15,
        severity_weights: dict[str, float] | None = None,
    ) -> None:
        if weight < 0:
            raise ValueError(f"ReviewCatchValueCalculator weight must be >= 0, got {weight}")
        #: Signal weight embedded in the returned :class:`~confidence.ConfidenceSignal`.
        self.weight: float = weight
        self._severity_weights: dict[str, float] = {**SEVERITY_WEIGHTS}
        if severity_weights:
            self._severity_weights.update(severity_weights)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        outcomes: list[dict[str, Any]],
    ) -> ConfidenceSignal:
        """Compute and return a ``"review_catch_value"`` signal.

        When *outcomes* is empty a **neutral** signal (``value = 0.5``) is
        returned — "no review data yet" is neither evidence of quality nor of
        problems.

        Args:
            outcomes: List of review outcome dicts.  Each dict should contain:

                      * ``"fix_verified"`` (bool) — whether the fix was
                        confirmed.
                      * ``"issues_found"`` (list) — each element is a dict
                        with at least a ``"severity"`` key (str).
                      * ``"verdict"`` (str) — ``"APPROVE"`` or
                        ``"REQUEST_CHANGES"``.

        Returns:
            A :class:`~confidence.ConfidenceSignal` named
            ``"review_catch_value"`` with ``value`` in ``[0, 1]``.
        """
        if not outcomes:
            return ConfidenceSignal(
                name="review_catch_value",
                value=_NEUTRAL_SCORE,
                weight=self.weight,
                raw_value={
                    "fix_verification_rate": _NEUTRAL_SCORE,
                    "weighted_catch_rate": _NEUTRAL_SCORE,
                    "false_positive_rate": 0.0,
                    "false_positive_penalty": 1.0,
                    "outcomes_count": 0,
                },
                source="0 review outcome(s) — neutral score",
            )

        n = len(outcomes)

        # ------------------------------------------------------------------
        # Sub-score 1: fix_verification_rate
        # Ratio of outcomes where fix_verified is True.
        # Range: [0, 1]
        # ------------------------------------------------------------------
        fix_verification_rate: float = sum(1 for o in outcomes if o.get("fix_verified")) / n

        # ------------------------------------------------------------------
        # Sub-score 2: weighted_catch_rate
        # Severity-weighted fraction of issue mass confirmed real (i.e. the
        # issue originated in an outcome with fix_verified=True).
        # Neutral (0.5) when no issues were found at all — "nothing to catch"
        # is neither a good nor a bad signal.
        # Range: [0, 1]
        # ------------------------------------------------------------------
        total_weight: float = sum(
            self._severity_weights.get(issue.get("severity", ""), 0.1)
            for o in outcomes
            for issue in o.get("issues_found", [])
        )
        verified_weight: float = sum(
            self._severity_weights.get(issue.get("severity", ""), 0.1)
            for o in outcomes
            if o.get("fix_verified")
            for issue in o.get("issues_found", [])
        )
        weighted_catch_rate: float = verified_weight / total_weight if total_weight > 0 else 0.5

        # ------------------------------------------------------------------
        # Sub-score 3: false_positive_rate / false_positive_penalty
        # Outcomes with issues_found AND verdict "APPROVE" are contradictory
        # (a reviewer saying "APPROVE" while also listing issues is a FP).
        # false_positive_penalty = 1 - false_positive_rate
        #   → 1.0 = no FPs (good)
        #   → 0.0 = all FPs (bad)
        # Range: [0, 1]
        # ------------------------------------------------------------------
        fp_count: int = sum(
            1 for o in outcomes if o.get("issues_found") and o.get("verdict") == "APPROVE"
        )
        false_positive_rate: float = fp_count / n
        false_positive_penalty: float = 1.0 - false_positive_rate

        # ------------------------------------------------------------------
        # Composite score
        # ------------------------------------------------------------------
        raw_score: float = (
            _W_FIX_VERIFICATION * fix_verification_rate
            + _W_CATCH_RATE * weighted_catch_rate
            + _W_FP_PENALTY * false_positive_penalty
        )
        # Clamp to [0, 1] — formula is analytically bounded, but guard
        # for floating-point edge-cases.
        score: float = max(0.0, min(1.0, raw_score))

        raw_value: dict[str, Any] = {
            "fix_verification_rate": fix_verification_rate,
            "weighted_catch_rate": weighted_catch_rate,
            "false_positive_rate": false_positive_rate,
            "false_positive_penalty": false_positive_penalty,
            "outcomes_count": n,
        }

        return ConfidenceSignal(
            name="review_catch_value",
            value=score,
            weight=self.weight,
            raw_value=raw_value,
            source=f"{n} review outcome(s)",
        )
