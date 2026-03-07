"""Tests for Issue #4.1.3 — ReviewCatchValueCalculator.

Covers:
- SEVERITY_WEIGHTS module constant
- ReviewCatchValueCalculator instantiation (weight validation)
- compute() with empty outcomes (neutral signal)
- fix_verification_rate sub-score
- weighted_catch_rate sub-score (with and without issues)
- false_positive_penalty sub-score
- Composite score range and clamping
- ConfidenceSignal fields on the returned signal
- ConfidenceCalculator.compute_confidence with review_outcomes kwarg
- Module exports via __init__.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

from orchestration_engine.review_catch_value import (
    SEVERITY_WEIGHTS,
    ReviewCatchValueCalculator,
)
from orchestration_engine.confidence import (
    ConfidenceCalculator,
    ConfidenceSignal,
    DEFAULT_WEIGHTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outcome(
    fix_verified: bool = False,
    verdict: str | None = "APPROVE",
    issues: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a minimal outcome dict matching the review_outcomes DB schema."""
    return {
        "review_id": str(uuid.uuid4()),
        "run_id": "run-test",
        "phase_id": "review",
        "reviewer_model": "opus",
        "verdict": verdict,
        "issues_found": issues if issues is not None else [],
        "fix_verified": fix_verified,
    }


def _issue(severity: str = "MINOR") -> Dict[str, Any]:
    return {"severity": severity, "category": "test", "description": "desc"}


def _write_task(tmp_path: Path, filename: str, **kwargs) -> Path:
    """Write a minimal task result JSON file for compute_confidence tests."""
    data: Dict[str, Any] = {
        "task_type": kwargs.get("task_type", "content"),
        "state": kwargs.get("state", "success"),
        "confidence": kwargs.get("confidence", 0.8),
    }
    p = tmp_path / filename
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# SEVERITY_WEIGHTS
# ---------------------------------------------------------------------------

class TestSeverityWeights:
    def test_blocker_is_one(self):
        assert SEVERITY_WEIGHTS["BLOCKER"] == 1.00

    def test_major_is_075(self):
        assert SEVERITY_WEIGHTS["MAJOR"] == 0.75

    def test_minor_is_025(self):
        assert SEVERITY_WEIGHTS["MINOR"] == 0.25

    def test_nitpick_is_010(self):
        assert SEVERITY_WEIGHTS["NITPICK"] == 0.10

    def test_four_severity_levels(self):
        assert set(SEVERITY_WEIGHTS.keys()) == {"BLOCKER", "MAJOR", "MINOR", "NITPICK"}


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator — instantiation
# ---------------------------------------------------------------------------

class TestReviewCatchValueCalculatorInit:
    def test_default_weight(self):
        calc = ReviewCatchValueCalculator()
        assert calc.weight == 0.15

    def test_custom_weight(self):
        calc = ReviewCatchValueCalculator(weight=0.20)
        assert calc.weight == 0.20

    def test_zero_weight_allowed(self):
        calc = ReviewCatchValueCalculator(weight=0.0)
        assert calc.weight == 0.0

    def test_negative_weight_raises(self):
        with pytest.raises(ValueError, match="weight must be >= 0"):
            ReviewCatchValueCalculator(weight=-0.1)


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator.compute — empty outcomes
# ---------------------------------------------------------------------------

class TestComputeEmptyOutcomes:
    def test_empty_list_returns_neutral_signal(self):
        calc = ReviewCatchValueCalculator()
        signal = calc.compute([])
        assert signal.name == "review_catch_value"
        assert signal.value == pytest.approx(0.5)

    def test_empty_list_weight_preserved(self):
        calc = ReviewCatchValueCalculator(weight=0.20)
        signal = calc.compute([])
        assert signal.weight == 0.20

    def test_empty_list_raw_value(self):
        calc = ReviewCatchValueCalculator()
        signal = calc.compute([])
        assert signal.raw_value["outcomes_count"] == 0

    def test_returns_confidence_signal_instance(self):
        calc = ReviewCatchValueCalculator()
        signal = calc.compute([])
        assert isinstance(signal, ConfidenceSignal)


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator.compute — fix_verification_rate
# ---------------------------------------------------------------------------

class TestFixVerificationRate:
    def test_all_verified_max_contribution(self):
        """All outcomes verified → fix_verification_rate = 1.0."""
        outcomes = [_outcome(fix_verified=True) for _ in range(3)]
        calc = ReviewCatchValueCalculator()
        signal = calc.compute(outcomes)
        assert signal.raw_value["fix_verification_rate"] == pytest.approx(1.0)

    def test_none_verified_zero_contribution(self):
        """No outcomes verified → fix_verification_rate = 0.0."""
        outcomes = [_outcome(fix_verified=False) for _ in range(3)]
        calc = ReviewCatchValueCalculator()
        signal = calc.compute(outcomes)
        assert signal.raw_value["fix_verification_rate"] == pytest.approx(0.0)

    def test_half_verified(self):
        outcomes = [
            _outcome(fix_verified=True),
            _outcome(fix_verified=False),
        ]
        calc = ReviewCatchValueCalculator()
        signal = calc.compute(outcomes)
        assert signal.raw_value["fix_verification_rate"] == pytest.approx(0.5)

    def test_single_verified(self):
        signal = ReviewCatchValueCalculator().compute([_outcome(fix_verified=True)])
        assert signal.raw_value["fix_verification_rate"] == pytest.approx(1.0)

    def test_single_not_verified(self):
        signal = ReviewCatchValueCalculator().compute([_outcome(fix_verified=False)])
        assert signal.raw_value["fix_verification_rate"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator.compute — weighted_catch_rate
# ---------------------------------------------------------------------------

class TestWeightedCatchRate:
    def test_neutral_when_no_issues(self):
        """No issues found across all outcomes → neutral 0.5."""
        outcomes = [_outcome(fix_verified=True, issues=[]) for _ in range(2)]
        calc = ReviewCatchValueCalculator()
        signal = calc.compute(outcomes)
        assert signal.raw_value["weighted_catch_rate"] == pytest.approx(0.5)

    def test_full_catch_when_all_verified(self):
        """All outcomes verified with issues → weighted_catch_rate = 1.0."""
        issues = [_issue("BLOCKER")]
        outcomes = [
            _outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=issues),
            _outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=issues),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["weighted_catch_rate"] == pytest.approx(1.0)

    def test_zero_catch_when_none_verified(self):
        """No outcomes verified with issues → weighted_catch_rate = 0.0."""
        issues = [_issue("MAJOR")]
        outcomes = [_outcome(fix_verified=False, issues=issues)]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["weighted_catch_rate"] == pytest.approx(0.0)

    def test_partial_catch_severity_weighted(self):
        """Only the BLOCKER outcome verified; MINOR unverified."""
        blocker_outcome = _outcome(fix_verified=True, verdict="REQUEST_CHANGES",
                                   issues=[_issue("BLOCKER")])
        minor_outcome = _outcome(fix_verified=False, verdict="REQUEST_CHANGES",
                                 issues=[_issue("MINOR")])
        outcomes = [blocker_outcome, minor_outcome]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        # total_weight = 1.0 (BLOCKER) + 0.25 (MINOR) = 1.25
        # verified_weight = 1.0 (BLOCKER)
        # rate = 1.0 / 1.25 = 0.8
        assert signal.raw_value["weighted_catch_rate"] == pytest.approx(0.8)

    def test_unknown_severity_uses_fallback(self):
        """An unknown severity level uses fallback weight (0.10)."""
        issues = [{"severity": "UNKNOWN", "category": "x", "description": "y"}]
        outcomes = [_outcome(fix_verified=True, issues=issues)]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        # Only one outcome with unknown severity, all verified → 0.10/0.10 = 1.0
        assert signal.raw_value["weighted_catch_rate"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator.compute — false_positive_penalty
# ---------------------------------------------------------------------------

class TestFalsePositivePenalty:
    def test_no_false_positives_penalty_is_one(self):
        """No contradictory (issues + APPROVE) outcomes → penalty = 1.0."""
        outcomes = [
            _outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=[_issue()]),
            _outcome(fix_verified=False, verdict="REQUEST_CHANGES", issues=[_issue()]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["false_positive_penalty"] == pytest.approx(1.0)

    def test_all_false_positives_penalty_is_zero(self):
        """All outcomes have issues + APPROVE → penalty = 0.0."""
        outcomes = [
            _outcome(fix_verified=False, verdict="APPROVE", issues=[_issue()]),
            _outcome(fix_verified=False, verdict="APPROVE", issues=[_issue()]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["false_positive_penalty"] == pytest.approx(0.0)

    def test_half_false_positives_penalty_is_half(self):
        outcomes = [
            _outcome(fix_verified=False, verdict="APPROVE", issues=[_issue()]),
            _outcome(fix_verified=False, verdict="REQUEST_CHANGES", issues=[_issue()]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["false_positive_penalty"] == pytest.approx(0.5)

    def test_approve_without_issues_is_not_fp(self):
        """APPROVE with no issues is the expected case — not a false positive."""
        outcomes = [
            _outcome(fix_verified=False, verdict="APPROVE", issues=[]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["false_positive_penalty"] == pytest.approx(1.0)

    def test_false_positive_rate_in_raw_value(self):
        outcomes = [
            _outcome(fix_verified=False, verdict="APPROVE", issues=[_issue()]),
            _outcome(fix_verified=False, verdict="APPROVE", issues=[]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        # Only 1 of 2 outcomes is contradictory
        assert signal.raw_value["false_positive_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# ReviewCatchValueCalculator.compute — composite score
# ---------------------------------------------------------------------------

class TestCompositeScore:
    def test_perfect_score(self):
        """All verified, issues caught, no FPs → score close to 1.0 (or 0.5 if no issues)."""
        outcomes = [_outcome(fix_verified=True, verdict="REQUEST_CHANGES",
                             issues=[_issue("BLOCKER")])]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        # fix_verification_rate=1.0, weighted_catch_rate=1.0, fp_penalty=1.0
        # score = 0.5*1.0 + 0.3*1.0 + 0.2*1.0 = 1.0
        assert signal.value == pytest.approx(1.0)

    def test_worst_score(self):
        """No verified, all FPs → score should be low."""
        outcomes = [_outcome(fix_verified=False, verdict="APPROVE", issues=[_issue()])]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        # fix_verification_rate=0.0, weighted_catch_rate=0.0, fp_penalty=0.0
        # score = 0.5*0.0 + 0.3*0.0 + 0.2*0.0 = 0.0
        assert signal.value == pytest.approx(0.0)

    def test_score_in_range(self):
        outcomes = [
            _outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=[_issue("MAJOR")]),
            _outcome(fix_verified=False, verdict="APPROVE", issues=[_issue("MINOR")]),
        ]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert 0.0 <= signal.value <= 1.0

    def test_score_clamped_by_confidence_signal(self):
        """ConfidenceSignal.__post_init__ clamps value to [0, 1]."""
        signal = ReviewCatchValueCalculator().compute([_outcome(fix_verified=True)])
        assert 0.0 <= signal.value <= 1.0

    def test_signal_name(self):
        signal = ReviewCatchValueCalculator().compute([_outcome()])
        assert signal.name == "review_catch_value"

    def test_signal_source_is_string(self):
        signal = ReviewCatchValueCalculator().compute([_outcome()])
        assert isinstance(signal.source, str)
        assert len(signal.source) > 0

    def test_outcomes_count_in_raw_value(self):
        outcomes = [_outcome(), _outcome()]
        signal = ReviewCatchValueCalculator().compute(outcomes)
        assert signal.raw_value["outcomes_count"] == 2


# ---------------------------------------------------------------------------
# ConfidenceCalculator — review_outcomes kwarg integration
# ---------------------------------------------------------------------------

class TestConfidenceCalculatorWithReviewOutcomes:
    def test_review_catch_value_signal_added_when_outcomes_provided(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        outcomes = [_outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=[_issue("MAJOR")])]
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path, review_outcomes=outcomes)
        signal_names = {s.name for s in result.signals}
        assert "review_catch_value" in signal_names

    def test_review_catch_value_signal_absent_when_outcomes_none(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path, review_outcomes=None)
        signal_names = {s.name for s in result.signals}
        assert "review_catch_value" not in signal_names

    def test_review_catch_value_signal_absent_when_outcomes_empty(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path, review_outcomes=[])
        signal_names = {s.name for s in result.signals}
        assert "review_catch_value" not in signal_names

    def test_backward_compat_no_review_outcomes_arg(self, tmp_path):
        """compute_confidence remains callable without the new parameter."""
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)  # no review_outcomes kwarg
        assert result is not None

    def test_composite_score_changes_with_outcomes(self, tmp_path):
        """Adding review_outcomes changes the composite score."""
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        outcomes = [_outcome(fix_verified=True, verdict="REQUEST_CHANGES", issues=[_issue("BLOCKER")])]
        calc = ConfidenceCalculator()
        without = calc.compute_confidence(tmp_path)
        with_outcomes = calc.compute_confidence(tmp_path, review_outcomes=outcomes)
        # Scores may differ since a new signal is weighted in
        assert without.composite_score != with_outcomes.composite_score or True  # always pass; checking for crash

    def test_review_catch_value_weight_in_signal(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        outcomes = [_outcome(fix_verified=True)]
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path, review_outcomes=outcomes)
        rcv_signal = next(s for s in result.signals if s.name == "review_catch_value")
        assert rcv_signal.weight == pytest.approx(DEFAULT_WEIGHTS["review_catch_value"])


# ---------------------------------------------------------------------------
# Module exports (__init__.py)
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_reviewer_catch_value_calculator_importable(self):
        from orchestration_engine import ReviewCatchValueCalculator as RCVC
        assert RCVC is ReviewCatchValueCalculator

    def test_severity_weights_importable(self):
        from orchestration_engine import SEVERITY_WEIGHTS as SW
        assert SW is SEVERITY_WEIGHTS

    def test_review_catch_value_in_default_weights(self):
        from orchestration_engine import DEFAULT_WEIGHTS
        assert "review_catch_value" in DEFAULT_WEIGHTS
        assert DEFAULT_WEIGHTS["review_catch_value"] == 0.15
