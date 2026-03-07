"""Tests for Issue #331.1 — Composite Confidence Scoring Model.

Covers:
- ConfidenceLevel enum (3-tier, run-scoped)
- ConfidenceSignal dataclass (clamping, validation)
- ConfidenceResult dataclass
- ConfidenceCalculator defaults and custom weights
- Signal extraction: llm_judge, test_pass_rate, review_quality, change_complexity
- compute_confidence with real fixture directories
- Edge cases: empty dir, missing dir, missing signals, partial data
- Module exports via __init__.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration_engine.confidence import (
    DEFAULT_WEIGHTS,
    ConfidenceCalculator,
    ConfidenceLevel,
    ConfidenceResult,
    ConfidenceSignal,
    _score_to_level,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_json(
    task_type: str = "content",
    state: str = "success",
    confidence: float = 0.8,
    filename: str | None = None,
) -> dict:
    """Build a minimal task result dict."""
    d: dict = {
        "task_type": task_type,
        "state": state,
        "confidence": confidence,
        "confidence_level": "high",
    }
    if filename is not None:
        d["_source_file"] = filename
    return d


def _write_task(tmp_path: Path, filename: str, **kwargs) -> Path:
    """Write a task result JSON file to tmp_path and return its path."""
    data = _task_json(filename=filename, **kwargs)
    # Remove the injected _source_file so it looks like real output
    data.pop("_source_file", None)
    p = tmp_path / filename
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# ConfidenceLevel
# ---------------------------------------------------------------------------

class TestConfidenceLevel:
    def test_values(self):
        assert ConfidenceLevel.HIGH == "high"
        assert ConfidenceLevel.MEDIUM == "medium"
        assert ConfidenceLevel.LOW == "low"

    def test_is_str_enum(self):
        assert isinstance(ConfidenceLevel.HIGH, str)

    def test_score_to_level_high(self):
        assert _score_to_level(0.90) == ConfidenceLevel.HIGH
        assert _score_to_level(1.00) == ConfidenceLevel.HIGH
        assert _score_to_level(0.95) == ConfidenceLevel.HIGH

    def test_score_to_level_medium(self):
        assert _score_to_level(0.75) == ConfidenceLevel.MEDIUM
        assert _score_to_level(0.80) == ConfidenceLevel.MEDIUM
        assert _score_to_level(0.89) == ConfidenceLevel.MEDIUM

    def test_score_to_level_low(self):
        assert _score_to_level(0.0) == ConfidenceLevel.LOW
        assert _score_to_level(0.50) == ConfidenceLevel.LOW
        assert _score_to_level(0.74) == ConfidenceLevel.LOW

    def test_boundary_exactly_090(self):
        assert _score_to_level(0.90) == ConfidenceLevel.HIGH

    def test_boundary_just_below_090(self):
        assert _score_to_level(0.8999) == ConfidenceLevel.MEDIUM

    def test_boundary_exactly_075(self):
        assert _score_to_level(0.75) == ConfidenceLevel.MEDIUM

    def test_boundary_just_below_075(self):
        assert _score_to_level(0.7499) == ConfidenceLevel.LOW


# ---------------------------------------------------------------------------
# ConfidenceSignal
# ---------------------------------------------------------------------------

class TestConfidenceSignal:
    def test_basic_creation(self):
        sig = ConfidenceSignal(
            name="test", value=0.5, weight=0.3, raw_value=0.5, source="test.json"
        )
        assert sig.name == "test"
        assert sig.value == 0.5
        assert sig.weight == 0.3

    def test_value_clamped_above_one(self):
        sig = ConfidenceSignal(
            name="test", value=1.5, weight=0.1, raw_value=1.5, source="x"
        )
        assert sig.value == 1.0

    def test_value_clamped_below_zero(self):
        sig = ConfidenceSignal(
            name="test", value=-0.5, weight=0.1, raw_value=-0.5, source="x"
        )
        assert sig.value == 0.0

    def test_value_exactly_zero_allowed(self):
        sig = ConfidenceSignal(
            name="test", value=0.0, weight=0.1, raw_value=0.0, source="x"
        )
        assert sig.value == 0.0

    def test_value_exactly_one_allowed(self):
        sig = ConfidenceSignal(
            name="test", value=1.0, weight=0.1, raw_value=1.0, source="x"
        )
        assert sig.value == 1.0

    def test_negative_weight_raises(self):
        with pytest.raises(ValueError, match="weight must be >= 0"):
            ConfidenceSignal(
                name="bad", value=0.5, weight=-0.1, raw_value=0.5, source="x"
            )

    def test_zero_weight_allowed(self):
        sig = ConfidenceSignal(
            name="zero_weight", value=0.5, weight=0.0, raw_value=0.5, source="x"
        )
        assert sig.weight == 0.0

    def test_raw_value_preserved(self):
        raw = {"passes": 9, "total": 10}
        sig = ConfidenceSignal(
            name="test", value=0.9, weight=0.3, raw_value=raw, source="x"
        )
        assert sig.raw_value is raw

    def test_source_preserved(self):
        sig = ConfidenceSignal(
            name="test", value=0.5, weight=0.3, raw_value=None, source="review.json"
        )
        assert sig.source == "review.json"


# ---------------------------------------------------------------------------
# ConfidenceResult
# ---------------------------------------------------------------------------

class TestConfidenceResult:
    def test_basic_creation(self):
        result = ConfidenceResult(
            signals=[],
            composite_score=0.85,
            confidence_level=ConfidenceLevel.MEDIUM,
            explanation="Test",
        )
        assert result.composite_score == 0.85
        assert result.confidence_level == ConfidenceLevel.MEDIUM

    def test_signals_list(self):
        sig = ConfidenceSignal("a", 0.5, 0.5, 0.5, "x")
        result = ConfidenceResult(
            signals=[sig],
            composite_score=0.5,
            confidence_level=ConfidenceLevel.LOW,
            explanation="ok",
        )
        assert len(result.signals) == 1
        assert result.signals[0] is sig


# ---------------------------------------------------------------------------
# DEFAULT_WEIGHTS
# ---------------------------------------------------------------------------

class TestDefaultWeights:
    def test_keys_present(self):
        for key in (
            "llm_judge", "test_pass_rate", "review_quality",
            "change_complexity", "review_catch_value",  # Issue #4.1.3
            "audit_catch_rate",                         # Issue #4.1.4
        ):
            assert key in DEFAULT_WEIGHTS

    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9

    def test_llm_judge_weight(self):
        # Updated in Issue #4.1.4: 0.35 → 0.30 to accommodate audit_catch_rate
        assert DEFAULT_WEIGHTS["llm_judge"] == 0.30

    def test_test_pass_rate_weight(self):
        # Updated in Issue #4.1.4: 0.25 → 0.20 to accommodate audit_catch_rate
        assert DEFAULT_WEIGHTS["test_pass_rate"] == 0.20

    def test_review_quality_weight(self):
        # Updated in Issue #4.1.3: 0.2 → 0.15
        assert DEFAULT_WEIGHTS["review_quality"] == 0.15

    def test_change_complexity_weight(self):
        assert DEFAULT_WEIGHTS["change_complexity"] == 0.10

    def test_review_catch_value_weight(self):
        # New signal added in Issue #4.1.3
        assert DEFAULT_WEIGHTS["review_catch_value"] == 0.15

    def test_audit_catch_rate_weight(self):
        # New signal added in Issue #4.1.4
        assert DEFAULT_WEIGHTS["audit_catch_rate"] == 0.10


# ---------------------------------------------------------------------------
# ConfidenceCalculator — instantiation
# ---------------------------------------------------------------------------

class TestConfidenceCalculatorInit:
    def test_default_weights(self):
        calc = ConfidenceCalculator()
        for key, val in DEFAULT_WEIGHTS.items():
            assert calc.weights[key] == val

    def test_custom_weights_merged(self):
        calc = ConfidenceCalculator(weights={"llm_judge": 0.6})
        assert calc.weights["llm_judge"] == 0.6
        # Other defaults survive
        assert calc.weights["test_pass_rate"] == DEFAULT_WEIGHTS["test_pass_rate"]

    def test_none_weights_uses_defaults(self):
        calc = ConfidenceCalculator(weights=None)
        assert calc.weights == DEFAULT_WEIGHTS

    def test_extra_weights_accepted(self):
        calc = ConfidenceCalculator(weights={"custom_signal": 0.05})
        assert calc.weights["custom_signal"] == 0.05


# ---------------------------------------------------------------------------
# ConfidenceCalculator — missing / empty directory
# ---------------------------------------------------------------------------

class TestComputeConfidenceMissingDir:
    def test_nonexistent_dir_raises(self, tmp_path):
        calc = ConfidenceCalculator()
        missing = tmp_path / "does_not_exist"
        with pytest.raises(ValueError, match="does not exist"):
            calc.compute_confidence(missing)

    def test_empty_dir_returns_low(self, tmp_path):
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert result.confidence_level == ConfidenceLevel.LOW
        assert result.composite_score == 0.0
        assert result.signals == []

    def test_only_meta_files_returns_low(self, tmp_path):
        """Files with _ prefix (e.g. _final_output.json) are skipped."""
        meta = {
            "task_id": "abc",
            "state": "success",
            "confidence": 0.95,
            "task_type": "content",
        }
        (tmp_path / "_final_output.json").write_text(json.dumps(meta))
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert result.confidence_level == ConfidenceLevel.LOW
        assert result.composite_score == 0.0


# ---------------------------------------------------------------------------
# ConfidenceCalculator — single task
# ---------------------------------------------------------------------------

class TestComputeConfidenceSingleTask:
    def test_single_content_task(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert isinstance(result, ConfidenceResult)
        # Should have signals (at least test_pass_rate, review_quality, change_complexity)
        signal_names = {s.name for s in result.signals}
        assert "test_pass_rate" in signal_names
        assert "review_quality" in signal_names
        assert "change_complexity" in signal_names
        # llm_judge should NOT be present (no review task)
        assert "llm_judge" not in signal_names

    def test_single_success_pass_rate_is_one(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        pass_signal = next(s for s in result.signals if s.name == "test_pass_rate")
        assert pass_signal.value == 1.0

    def test_single_failure_pass_rate_is_zero(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="error", confidence=0.2)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        pass_signal = next(s for s in result.signals if s.name == "test_pass_rate")
        assert pass_signal.value == 0.0

    def test_single_task_complexity_score(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        complexity = next(s for s in result.signals if s.name == "change_complexity")
        # 1 task file: 1 / (1 + 1) = 0.5
        assert complexity.value == pytest.approx(0.5)
        assert complexity.raw_value == 1


# ---------------------------------------------------------------------------
# ConfidenceCalculator — review / judge tasks
# ---------------------------------------------------------------------------

class TestComputeConfidenceReviewTasks:
    def test_review_task_produces_llm_judge_signal(self, tmp_path):
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.85)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        signal_names = {s.name for s in result.signals}
        assert "llm_judge" in signal_names

    def test_judge_task_type_produces_llm_judge_signal(self, tmp_path):
        _write_task(tmp_path, "eval.json", task_type="judge", state="success", confidence=0.9)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        signal_names = {s.name for s in result.signals}
        assert "llm_judge" in signal_names

    def test_filename_with_review_produces_llm_judge(self, tmp_path):
        """Files with 'review' in their filename are treated as judge tasks."""
        _write_task(tmp_path, "code-review.json", task_type="content", state="success", confidence=0.75)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        signal_names = {s.name for s in result.signals}
        assert "llm_judge" in signal_names

    def test_llm_judge_value_matches_confidence(self, tmp_path):
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.85)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        judge = next(s for s in result.signals if s.name == "llm_judge")
        assert judge.value == pytest.approx(0.85)

    def test_multiple_review_tasks_averaged(self, tmp_path):
        _write_task(tmp_path, "review-a.json", task_type="review", state="success", confidence=0.80)
        _write_task(tmp_path, "review-b.json", task_type="review", state="success", confidence=0.60)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        judge = next(s for s in result.signals if s.name == "llm_judge")
        assert judge.value == pytest.approx(0.70)

    def test_review_tasks_excluded_from_pass_rate(self, tmp_path):
        """Review tasks should not count toward test_pass_rate."""
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.85)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        # No non-review tasks → test_pass_rate signal should be absent
        signal_names = {s.name for s in result.signals}
        assert "test_pass_rate" not in signal_names


# ---------------------------------------------------------------------------
# ConfidenceCalculator — mixed tasks
# ---------------------------------------------------------------------------

class TestComputeConfidenceMixedTasks:
    def test_mixed_success_failure(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.9)
        _write_task(tmp_path, "phase2.json", task_type="content", state="error", confidence=0.1)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        pass_signal = next(s for s in result.signals if s.name == "test_pass_rate")
        assert pass_signal.value == pytest.approx(0.5)

    def test_all_success_pass_rate_one(self, tmp_path):
        for i in range(3):
            _write_task(tmp_path, f"phase{i}.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        pass_signal = next(s for s in result.signals if s.name == "test_pass_rate")
        assert pass_signal.value == 1.0

    def test_all_failure_pass_rate_zero(self, tmp_path):
        for i in range(3):
            _write_task(tmp_path, f"phase{i}.json", task_type="content", state="error", confidence=0.1)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        pass_signal = next(s for s in result.signals if s.name == "test_pass_rate")
        assert pass_signal.value == 0.0

    def test_review_quality_averages_all_tasks(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.6)
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        rq = next(s for s in result.signals if s.name == "review_quality")
        assert rq.value == pytest.approx(0.7)

    def test_complexity_scales_with_task_count(self, tmp_path):
        for i in range(9):
            _write_task(tmp_path, f"phase{i}.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        comp = next(s for s in result.signals if s.name == "change_complexity")
        # 9 tasks: 1 / (1 + 9) = 0.1
        assert comp.value == pytest.approx(0.1)
        assert comp.raw_value == 9


# ---------------------------------------------------------------------------
# ConfidenceCalculator — composite score and level
# ---------------------------------------------------------------------------

class TestComputeConfidenceScoreAndLevel:
    def test_high_confidence_result(self, tmp_path):
        """All-success tasks with high confidence → should reach HIGH or MEDIUM."""
        for i in range(2):
            _write_task(tmp_path, f"phase{i}.json", task_type="content", state="success", confidence=0.95)
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.95)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert result.composite_score > 0.75
        assert result.confidence_level in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)

    def test_low_confidence_result(self, tmp_path):
        """All-failure tasks with low confidence → should reach LOW."""
        for i in range(3):
            _write_task(tmp_path, f"phase{i}.json", task_type="content", state="error", confidence=0.1)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert result.composite_score < 0.75
        assert result.confidence_level == ConfidenceLevel.LOW

    def test_composite_score_in_range(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.7)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert 0.0 <= result.composite_score <= 1.0

    def test_explanation_is_string(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.7)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0

    def test_explanation_contains_score(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.7)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        # Explanation should mention the score
        assert str(round(result.composite_score, 2))[:3] in result.explanation or \
               "score" in result.explanation.lower()

    def test_custom_weights_affect_score(self, tmp_path):
        """Changing weights should change the composite score."""
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.5)
        _write_task(tmp_path, "review.json", task_type="review", state="success", confidence=0.9)

        calc_default = ConfidenceCalculator()
        calc_heavy_judge = ConfidenceCalculator(weights={"llm_judge": 0.9, "test_pass_rate": 0.05, "review_quality": 0.04, "change_complexity": 0.01})

        result_default = calc_default.compute_confidence(tmp_path)
        result_heavy = calc_heavy_judge.compute_confidence(tmp_path)

        # With heavy judge weight, score should be higher (review confidence is 0.9 > 0.5)
        assert result_heavy.composite_score != result_default.composite_score

    def test_result_signals_not_empty_for_real_dir(self, tmp_path):
        _write_task(tmp_path, "phase1.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert len(result.signals) > 0


# ---------------------------------------------------------------------------
# ConfidenceCalculator — malformed JSON tolerance
# ---------------------------------------------------------------------------

class TestComputeConfidenceMalformedFiles:
    def test_malformed_json_skipped(self, tmp_path):
        """Malformed JSON files should be silently skipped."""
        (tmp_path / "bad.json").write_text("this is not json {{{")
        _write_task(tmp_path, "good.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        # Should still produce a result based on the good file
        assert isinstance(result, ConfidenceResult)

    def test_non_dict_json_skipped(self, tmp_path):
        """JSON arrays or scalars at the top level are not task results."""
        (tmp_path / "list.json").write_text("[1, 2, 3]")
        _write_task(tmp_path, "task.json", task_type="content", state="success", confidence=0.8)
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert isinstance(result, ConfidenceResult)

    def test_task_without_confidence_field(self, tmp_path):
        """Tasks missing 'confidence' field should not break signal extraction."""
        data = {"task_type": "content", "state": "success"}
        (tmp_path / "no-conf.json").write_text(json.dumps(data))
        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        assert isinstance(result, ConfidenceResult)
        # test_pass_rate should still work (it doesn't need confidence)
        signal_names = {s.name for s in result.signals}
        assert "test_pass_rate" in signal_names


# ---------------------------------------------------------------------------
# Module exports (__init__.py)
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_calculator_importable_from_package(self):
        from orchestration_engine import ConfidenceCalculator as CC
        assert CC is ConfidenceCalculator

    def test_result_importable_from_package(self):
        from orchestration_engine import ConfidenceResult as CR
        assert CR is ConfidenceResult

    def test_signal_importable_from_package(self):
        from orchestration_engine import ConfidenceSignal as CS
        assert CS is ConfidenceSignal

    def test_run_confidence_level_importable_from_package(self):
        from orchestration_engine import RunConfidenceLevel
        assert RunConfidenceLevel is ConfidenceLevel

    def test_default_weights_importable_from_package(self):
        from orchestration_engine import DEFAULT_WEIGHTS as DW
        assert DW is DEFAULT_WEIGHTS

    def test_schemas_confidence_level_unchanged(self):
        """The original 5-tier schemas.ConfidenceLevel must not be broken."""
        from orchestration_engine.schemas import ConfidenceLevel as SchemaCL
        assert hasattr(SchemaCL, "VERY_LOW")
        assert hasattr(SchemaCL, "VERY_HIGH")
        assert "very_low" in [m.value for m in SchemaCL]

    def test_run_confidence_level_distinct_from_schemas(self):
        """RunConfidenceLevel (3-tier) must be a different enum from schemas.ConfidenceLevel."""
        from orchestration_engine import RunConfidenceLevel
        from orchestration_engine.schemas import ConfidenceLevel as SchemaCL
        assert RunConfidenceLevel is not SchemaCL
        # 3-tier vs 5-tier
        assert len(list(RunConfidenceLevel)) == 3
        assert len(list(SchemaCL)) == 5
