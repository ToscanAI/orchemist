"""Calibration tests for Issue #429.1 — Confidence Weight Recalibration.

Validates that the recalibrated DEFAULT_WEIGHTS_V2 produces composite scores
that reflect actual pipeline quality rather than being anchored by:
  1. Mock confidence ceiling (hardcoded 0.85 in task result files)
  2. change_complexity drag (1/(1+N) for N=5-10 tasks gives 0.09-0.17)

Sprint 1-4 empirical profiles are used as ground truth for the tests.

Key scenarios tested:
  - Typical 9-task coding pipeline: all non-review tasks succeed, review task
    scores 0.97 → composite should reach AUTO_MERGE_THRESHOLD (≥ 0.90)
  - Mid-quality run: mixed pass/fail, 0.82 review confidence → MEDIUM
  - Low-quality run: many failures, 0.65 review confidence → LOW
  - Weight sum invariant: DEFAULT_WEIGHTS_V2 sums to 1.00
  - Threshold constant values: AUTO_MERGE_THRESHOLD and HUMAN_REVIEW_THRESHOLD
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration_engine.confidence import (
    AUTO_MERGE_THRESHOLD,
    HUMAN_REVIEW_THRESHOLD,
    DEFAULT_WEIGHTS_V2,
    ConfidenceCalculator,
    ConfidenceLevel,
    ConfidenceSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_task(
    tmp_path: Path,
    filename: str,
    task_type: str = "content",
    state: str = "success",
    confidence: float = 0.85,
) -> Path:
    """Write a minimal task result JSON file and return its path."""
    data = {
        "task_type": task_type,
        "state": state,
        "confidence": confidence,
    }
    p = tmp_path / filename
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# Constant invariants
# ---------------------------------------------------------------------------

class TestThresholdConstants:
    """Issue #429.1: threshold constants must be in confidence.py as authority."""

    def test_auto_merge_threshold_value(self):
        assert AUTO_MERGE_THRESHOLD == 0.90

    def test_human_review_threshold_value(self):
        """HUMAN_REVIEW_THRESHOLD was lowered from 0.75 → 0.70 post-calibration."""
        assert HUMAN_REVIEW_THRESHOLD == 0.70

    def test_auto_merge_above_human_review(self):
        assert AUTO_MERGE_THRESHOLD > HUMAN_REVIEW_THRESHOLD

    def test_thresholds_importable_from_package(self):
        """Constants must be exported from the top-level package."""
        from orchestration_engine import AUTO_MERGE_THRESHOLD as amt
        from orchestration_engine import HUMAN_REVIEW_THRESHOLD as hrt
        assert amt == 0.90
        assert hrt == 0.70


class TestWeightsV2Invariants:
    """Structural invariants for the recalibrated weight table."""

    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_WEIGHTS_V2.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"

    def test_llm_judge_is_primary(self):
        """llm_judge must be the highest-weighted signal (Issue #429.1)."""
        assert DEFAULT_WEIGHTS_V2["llm_judge"] == max(DEFAULT_WEIGHTS_V2.values())

    def test_llm_judge_weight(self):
        assert DEFAULT_WEIGHTS_V2["llm_judge"] == 0.40

    def test_test_pass_rate_weight(self):
        assert DEFAULT_WEIGHTS_V2["test_pass_rate"] == 0.30

    def test_change_complexity_is_minimal(self):
        """change_complexity must be the smallest or near-smallest signal."""
        assert DEFAULT_WEIGHTS_V2["change_complexity"] <= 0.05

    def test_change_complexity_weight(self):
        assert DEFAULT_WEIGHTS_V2["change_complexity"] == 0.02

    def test_review_quality_restored(self):
        """review_quality must be present in v2 to avoid silent v1 fallback."""
        assert "review_quality" in DEFAULT_WEIGHTS_V2
        assert DEFAULT_WEIGHTS_V2["review_quality"] > 0

    def test_all_weights_positive(self):
        for key, val in DEFAULT_WEIGHTS_V2.items():
            assert val > 0, f"Weight for {key!r} is not positive: {val}"

    def test_historical_calibration_present(self):
        """historical_calibration must remain for extra_signals compatibility."""
        assert "historical_calibration" in DEFAULT_WEIGHTS_V2

    def test_default_weights_v2_importable_from_package(self):
        from orchestration_engine import DEFAULT_WEIGHTS_V2 as dw
        assert dw is DEFAULT_WEIGHTS_V2


# ---------------------------------------------------------------------------
# Sprint 1-4 calibration profiles
# ---------------------------------------------------------------------------

class TestSprintProfileHighQuality:
    """Typical high-quality 9-task coding pipeline from Sprint 1-4.

    Profile:
      - 8 non-review tasks (spec, implement, qa×4, build, deploy) — all success
      - 1 review task (code review LLM judge) — confidence 0.97
      - All non-review tasks have confidence 0.85 (executor mock ceiling)

    Expected result with DEFAULT_WEIGHTS_V2:
      - composite ≥ AUTO_MERGE_THRESHOLD (0.90) → AUTO-MERGE
    Expected result with DEFAULT_WEIGHTS (v1):
      - composite ≈ 0.79 (stuck due to change_complexity drag + mock ceiling)
    """

    def _build_dir(self, tmp_path: Path) -> Path:
        """Write 9-task Sprint 1-4 profile fixture."""
        run_dir = tmp_path / "sprint1-high-quality"
        run_dir.mkdir()
        # 8 non-review tasks (all success, mock confidence 0.85)
        for i in range(1, 9):
            _write_task(
                run_dir,
                f"task_{i:02d}_impl.json",
                task_type="content",
                state="success",
                confidence=0.85,
            )
        # 1 review task with high rubric score
        _write_task(
            run_dir,
            "review_01_llm_judge.json",
            task_type="review",
            state="success",
            confidence=0.97,
        )
        return run_dir

    def test_v2_weights_reach_auto_merge(self, tmp_path):
        """With v2 weights, high-quality run composite must hit AUTO_MERGE_THRESHOLD."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.composite_score >= AUTO_MERGE_THRESHOLD, (
            f"Expected composite ≥ {AUTO_MERGE_THRESHOLD}, "
            f"got {result.composite_score:.4f}\n{result.explanation}"
        )
        assert result.confidence_level == ConfidenceLevel.HIGH

    def test_v1_weights_below_auto_merge(self, tmp_path):
        """With v1 (uncalibrated) weights, the same run is stuck below AUTO_MERGE."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.composite_score < AUTO_MERGE_THRESHOLD, (
            f"Expected v1 composite < {AUTO_MERGE_THRESHOLD} "
            f"(demonstrating calibration need), got {result.composite_score:.4f}"
        )

    def test_v2_improvement_over_v1(self, tmp_path):
        """v2 composite must be strictly higher than v1 for this profile."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS
        run_dir = self._build_dir(tmp_path)
        v1_result = ConfidenceCalculator(weights=DEFAULT_WEIGHTS).compute_confidence(run_dir)
        v2_result = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2).compute_confidence(run_dir)
        assert v2_result.composite_score > v1_result.composite_score, (
            f"v2 ({v2_result.composite_score:.4f}) not better than "
            f"v1 ({v1_result.composite_score:.4f})"
        )

    def test_change_complexity_signal_weight_small(self, tmp_path):
        """change_complexity signal must have weight ≤ 0.05 to not drag composite."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        cc_signal = next(
            (s for s in result.signals if s.name == "change_complexity"), None
        )
        assert cc_signal is not None, "change_complexity signal missing"
        assert cc_signal.weight <= 0.05, (
            f"change_complexity weight {cc_signal.weight} too high; must be ≤ 0.05"
        )

    def test_explanation_references_signals(self, tmp_path):
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert "llm_judge" in result.explanation
        assert "test_pass_rate" in result.explanation


class TestSprintProfileMidQuality:
    """Mid-quality run: 50% pass rate, review confidence 0.82.

    Expected: HUMAN_REVIEW_THRESHOLD ≤ composite < AUTO_MERGE_THRESHOLD → MEDIUM
    """

    def _build_dir(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "sprint3-mid-quality"
        run_dir.mkdir()
        # 4 success, 4 failure (50% pass rate)
        for i in range(1, 5):
            _write_task(
                run_dir,
                f"task_{i:02d}_pass.json",
                task_type="content",
                state="success",
                confidence=0.85,
            )
        for i in range(5, 9):
            _write_task(
                run_dir,
                f"task_{i:02d}_fail.json",
                task_type="content",
                state="failed",
                confidence=0.70,
            )
        # Review task with middling confidence
        _write_task(
            run_dir,
            "review_01.json",
            task_type="review",
            state="success",
            confidence=0.82,
        )
        return run_dir

    def test_v2_mid_quality_is_medium_or_low(self, tmp_path):
        """Mid-quality run must not auto-merge."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.composite_score < AUTO_MERGE_THRESHOLD, (
            f"Mid-quality run should not auto-merge, "
            f"got composite={result.composite_score:.4f}"
        )

    def test_v2_mid_quality_above_floor(self, tmp_path):
        """Mid-quality run composite must be above 0.50."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.composite_score > 0.50


class TestSprintProfileLowQuality:
    """Low-quality run: 10% pass rate, review confidence 0.65.

    Expected: composite < HUMAN_REVIEW_THRESHOLD → LOW
    """

    def _build_dir(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "sprint2-low-quality"
        run_dir.mkdir()
        # 1 success, 9 failures
        _write_task(
            run_dir,
            "task_01_pass.json",
            task_type="content",
            state="success",
            confidence=0.85,
        )
        for i in range(2, 11):
            _write_task(
                run_dir,
                f"task_{i:02d}_fail.json",
                task_type="content",
                state="failed",
                confidence=0.60,
            )
        # Poor review score
        _write_task(
            run_dir,
            "review_01.json",
            task_type="review",
            state="success",
            confidence=0.65,
        )
        return run_dir

    def test_v2_low_quality_is_low(self, tmp_path):
        """Low-quality run must score below HUMAN_REVIEW_THRESHOLD."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.composite_score < HUMAN_REVIEW_THRESHOLD, (
            f"Low-quality run composite {result.composite_score:.4f} "
            f"should be < HUMAN_REVIEW_THRESHOLD ({HUMAN_REVIEW_THRESHOLD})"
        )
        assert result.confidence_level == ConfidenceLevel.LOW


class TestSprintProfileNoReviewTask:
    """Pipeline with no review task (review signal absent) — e.g. simple deploy run.

    With v2 weights and all tasks succeeding, composite should still be high
    because test_pass_rate dominates.
    """

    def _build_dir(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "no-review"
        run_dir.mkdir()
        for i in range(1, 6):
            _write_task(
                run_dir,
                f"task_{i:02d}.json",
                task_type="content",
                state="success",
                confidence=0.85,
            )
        return run_dir

    def test_all_success_no_review_is_medium_or_high(self, tmp_path):
        """All-success pipeline without review task should score MEDIUM or HIGH."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert result.confidence_level in (ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH), (
            f"All-success no-review pipeline scored {result.confidence_level}: "
            f"composite={result.composite_score:.4f}"
        )

    def test_no_llm_judge_signal(self, tmp_path):
        """Without review tasks, llm_judge signal must be absent."""
        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(self._build_dir(tmp_path))
        assert not any(s.name == "llm_judge" for s in result.signals)


# ---------------------------------------------------------------------------
# Extra signals compatibility
# ---------------------------------------------------------------------------

class TestExtraSignalsWithV2:
    """Verify extra_signals (e.g. historical_calibration) work with v2 weights."""

    def test_historical_calibration_extra_signal(self, tmp_path):
        run_dir = tmp_path / "extra-sig"
        run_dir.mkdir()
        _write_task(run_dir, "task_01.json", state="success", confidence=0.85)
        _write_task(run_dir, "review_01.json", task_type="review", confidence=0.97)

        historical = ConfidenceSignal(
            name="historical_calibration",
            value=0.92,
            weight=DEFAULT_WEIGHTS_V2.get("historical_calibration", 0.02),
            raw_value=0.92,
            source="ReviewerCalibrator longitudinal accuracy",
        )

        calc = ConfidenceCalculator(weights=DEFAULT_WEIGHTS_V2)
        result = calc.compute_confidence(run_dir, extra_signals=[historical])

        signal_names = [s.name for s in result.signals]
        assert "historical_calibration" in signal_names
        assert result.composite_score >= AUTO_MERGE_THRESHOLD
