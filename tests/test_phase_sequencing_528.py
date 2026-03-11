"""Tests for Issue #528: Spec-Driven Acceptance Tests.

Validates that the new acceptance_test phase is correctly inserted into
coding-pipeline-v1.yaml, transitions are wired correctly, guard rail text
is present in the implement phase, and confidence weight updates are correct.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "coding-pipeline-v1.yaml"
SCENARIO_PATH = REPO_ROOT / "scenarios" / "coding-pipeline-v1-smoke.yaml"


def load_template() -> dict:
    """Load and parse the coding-pipeline-v1.yaml template."""
    return yaml.safe_load(TEMPLATE_PATH.read_text())


def get_phase(template: dict, phase_id: str) -> dict | None:
    """Return the phase block with the given id, or None."""
    for phase in template.get("phases", []):
        if phase.get("id") == phase_id:
            return phase
    return None


def phase_index(template: dict, phase_id: str) -> int:
    """Return the 0-based index of a phase, or -1."""
    for i, phase in enumerate(template.get("phases", [])):
        if phase.get("id") == phase_id:
            return i
    return -1


# ---------------------------------------------------------------------------
# Phase existence
# ---------------------------------------------------------------------------

class TestAcceptanceTestPhaseExists:
    """The acceptance_test phase must be present in the template."""

    def test_acceptance_test_phase_present(self):
        """acceptance_test phase exists in coding-pipeline-v1.yaml."""
        template = load_template()
        phase = get_phase(template, "acceptance_test")
        assert phase is not None, "acceptance_test phase not found in template"

    def test_acceptance_test_phase_has_name(self):
        """acceptance_test phase has a non-empty name."""
        template = load_template()
        phase = get_phase(template, "acceptance_test")
        assert phase is not None
        assert phase.get("name"), "acceptance_test phase has no name"

    def test_acceptance_test_phase_has_prompt_template(self):
        """acceptance_test phase has a non-empty prompt_template."""
        template = load_template()
        phase = get_phase(template, "acceptance_test")
        assert phase is not None
        assert phase.get("prompt_template"), "acceptance_test phase has no prompt_template"

    def test_acceptance_test_phase_model_tier(self):
        """acceptance_test phase uses sonnet model tier."""
        template = load_template()
        phase = get_phase(template, "acceptance_test")
        assert phase is not None
        assert phase.get("model_tier") == "sonnet", (
            f"Expected sonnet, got {phase.get('model_tier')}"
        )


# ---------------------------------------------------------------------------
# Phase ordering
# ---------------------------------------------------------------------------

class TestPhaseOrdering:
    """acceptance_test must appear between spec and implement."""

    def test_acceptance_test_after_spec(self):
        """acceptance_test phase index > spec phase index."""
        template = load_template()
        spec_idx = phase_index(template, "spec")
        at_idx = phase_index(template, "acceptance_test")
        assert spec_idx >= 0, "spec phase not found"
        assert at_idx >= 0, "acceptance_test phase not found"
        assert at_idx > spec_idx, (
            f"acceptance_test (idx={at_idx}) should come after spec (idx={spec_idx})"
        )

    def test_acceptance_test_before_implement(self):
        """acceptance_test phase index < implement phase index."""
        template = load_template()
        at_idx = phase_index(template, "acceptance_test")
        impl_idx = phase_index(template, "implement")
        assert at_idx >= 0, "acceptance_test phase not found"
        assert impl_idx >= 0, "implement phase not found"
        assert at_idx < impl_idx, (
            f"acceptance_test (idx={at_idx}) should come before implement (idx={impl_idx})"
        )

    def test_phase_count_is_seven(self):
        """Template has exactly 7 phases (spec, acceptance_test, implement, acceptance_run, review, fix, test)."""
        template = load_template()
        phase_ids = [p.get("id") for p in template.get("phases", [])]
        assert len(phase_ids) == 7, (
            f"Expected 7 phases, got {len(phase_ids)}: {phase_ids}"
        )


# ---------------------------------------------------------------------------
# Transition wiring
# ---------------------------------------------------------------------------

class TestTransitions:
    """Phase transitions must form a correct chain."""

    def test_spec_transitions_to_acceptance_test(self):
        """spec phase success transition points to acceptance_test."""
        template = load_template()
        spec = get_phase(template, "spec")
        assert spec is not None
        transitions = spec.get("transitions", {})
        assert transitions.get("success") == "acceptance_test", (
            f"spec success transition should be 'acceptance_test', "
            f"got {transitions.get('success')!r}"
        )

    def test_acceptance_test_transitions_to_implement(self):
        """acceptance_test phase success transition points to implement."""
        template = load_template()
        at_phase = get_phase(template, "acceptance_test")
        assert at_phase is not None
        transitions = at_phase.get("transitions", {})
        assert transitions.get("success") == "implement", (
            f"acceptance_test success transition should be 'implement', "
            f"got {transitions.get('success')!r}"
        )

    def test_implement_transitions_to_acceptance_run(self):
        """implement phase success transition points to acceptance_run (updated in #532)."""
        template = load_template()
        impl = get_phase(template, "implement")
        assert impl is not None
        transitions = impl.get("transitions", {})
        assert transitions.get("success") == "acceptance_run", (
            f"implement success transition should be 'acceptance_run' (updated by #532), "
            f"got {transitions.get('success')!r}"
        )


# ---------------------------------------------------------------------------
# Guard rail text
# ---------------------------------------------------------------------------

class TestImplementGuardRail:
    """The implement phase prompt must contain acceptance test guard rail language."""

    def test_implement_prompt_references_immutable(self):
        """implement prompt mentions the IMMUTABLE constraint keyword."""
        template = load_template()
        impl = get_phase(template, "implement")
        assert impl is not None
        prompt = impl.get("prompt_template", "")
        assert "IMMUTABLE" in prompt, (
            "implement prompt_template must contain 'IMMUTABLE' guard rail text"
        )

    def test_implement_prompt_references_acceptance_tests(self):
        """implement prompt instructs agent to read acceptance_tests.py."""
        template = load_template()
        impl = get_phase(template, "implement")
        assert impl is not None
        prompt = impl.get("prompt_template", "")
        assert "acceptance_tests.py" in prompt, (
            "implement prompt_template must reference 'acceptance_tests.py'"
        )

    def test_implement_prompt_context_header_updated(self):
        """implement prompt uses (3/5) phase numbering, not old (2/4)."""
        template = load_template()
        impl = get_phase(template, "implement")
        assert impl is not None
        prompt = impl.get("prompt_template", "")
        assert "(3/5)" in prompt, (
            "implement prompt should reference phase (3/5), not (2/4)"
        )
        assert "(2/4)" not in prompt, (
            "implement prompt still contains old phase numbering (2/4)"
        )

    def test_implement_prompt_references_acceptance_results_json(self):
        """implement prompt instructs agent to write acceptance_results.json."""
        template = load_template()
        impl = get_phase(template, "implement")
        assert impl is not None
        prompt = impl.get("prompt_template", "")
        assert "acceptance_results.json" in prompt, (
            "implement prompt must instruct agent to write 'acceptance_results.json'"
        )


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

class TestConfigSchema:
    """acceptance_test_file must be present in config_schema."""

    def test_acceptance_test_file_in_config_schema(self):
        """config_schema includes acceptance_test_file property."""
        template = load_template()
        schema = template.get("config_schema", {})
        props = schema.get("properties", {})
        assert "acceptance_test_file" in props, (
            "config_schema.properties should include 'acceptance_test_file'"
        )

    def test_acceptance_test_file_has_default(self):
        """acceptance_test_file property has a default value."""
        template = load_template()
        schema = template.get("config_schema", {})
        props = schema.get("properties", {})
        at_prop = props.get("acceptance_test_file", {})
        assert "default" in at_prop, (
            "acceptance_test_file should have a default value in config_schema"
        )


# ---------------------------------------------------------------------------
# Template version
# ---------------------------------------------------------------------------

class TestTemplateVersion:
    """Template version must be bumped to 1.3.0."""

    def test_version_bumped(self):
        """Template version is 1.4.0 (bumped by #532 from 1.3.0)."""
        template = load_template()
        version = template.get("version")
        assert version == "1.4.0", (
            f"Expected version '1.4.0', got {version!r}"
        )

    def test_name_updated(self):
        """Template name reflects the current version."""
        template = load_template()
        name = template.get("name", "")
        assert "1.4" in name, (
            f"Template name should reference v1.4, got {name!r}"
        )


# ---------------------------------------------------------------------------
# Confidence weights (confidence.py)
# ---------------------------------------------------------------------------

class TestConfidenceWeights:
    """Issue #528 confidence weight changes must be reflected in confidence.py."""

    def test_acceptance_pass_rate_in_default_weights(self):
        """DEFAULT_WEIGHTS includes 'acceptance_pass_rate'."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS
        assert "acceptance_pass_rate" in DEFAULT_WEIGHTS, (
            "DEFAULT_WEIGHTS must include 'acceptance_pass_rate'"
        )

    def test_acceptance_pass_rate_weight_is_040(self):
        """DEFAULT_WEIGHTS['acceptance_pass_rate'] == 0.40."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS
        assert DEFAULT_WEIGHTS["acceptance_pass_rate"] == pytest.approx(0.40), (
            f"acceptance_pass_rate weight should be 0.40, "
            f"got {DEFAULT_WEIGHTS['acceptance_pass_rate']}"
        )

    def test_review_quality_reduced_in_default_weights(self):
        """DEFAULT_WEIGHTS['review_quality'] is 0.05 (reduced from 0.15)."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS
        assert DEFAULT_WEIGHTS["review_quality"] == pytest.approx(0.05), (
            f"review_quality weight should be 0.05 (reduced from 0.15), "
            f"got {DEFAULT_WEIGHTS['review_quality']}"
        )

    def test_acceptance_pass_rate_in_v2_weights(self):
        """DEFAULT_WEIGHTS_V2 includes 'acceptance_pass_rate'."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS_V2
        assert "acceptance_pass_rate" in DEFAULT_WEIGHTS_V2, (
            "DEFAULT_WEIGHTS_V2 must include 'acceptance_pass_rate'"
        )

    def test_review_quality_reduced_in_v2_weights(self):
        """DEFAULT_WEIGHTS_V2['review_quality'] is 0.02 (reduced from 0.06)."""
        from orchestration_engine.confidence import DEFAULT_WEIGHTS_V2
        assert DEFAULT_WEIGHTS_V2["review_quality"] == pytest.approx(0.02), (
            f"DEFAULT_WEIGHTS_V2 review_quality should be 0.02 (reduced from 0.06), "
            f"got {DEFAULT_WEIGHTS_V2['review_quality']}"
        )


# ---------------------------------------------------------------------------
# _extract_acceptance_pass_rate method
# ---------------------------------------------------------------------------

class TestExtractAcceptancePassRate:
    """Unit tests for ConfidenceCalculator._extract_acceptance_pass_rate."""

    def _make_calc(self):
        from orchestration_engine.confidence import ConfidenceCalculator, DEFAULT_WEIGHTS
        return ConfidenceCalculator(), DEFAULT_WEIGHTS

    def test_returns_none_when_file_missing(self, tmp_path):
        """Returns None when acceptance_results.json does not exist."""
        calc, weights = self._make_calc()
        result = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert result is None

    def test_returns_signal_with_pass_rate(self, tmp_path):
        """Returns a ConfidenceSignal with correct value from pass_rate field."""
        results_file = tmp_path / "acceptance_results.json"
        results_file.write_text(json.dumps({
            "phase": "implement",
            "status": "complete",
            "passed": 8,
            "failed": 2,
            "total": 10,
            "pass_rate": 0.8,
        }))
        calc, weights = self._make_calc()
        signal = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert signal is not None
        assert signal.name == "acceptance_pass_rate"
        assert signal.value == pytest.approx(0.8)
        assert signal.weight == pytest.approx(0.40)

    def test_derives_pass_rate_from_counts(self, tmp_path):
        """Derives pass_rate from passed/total when pass_rate field is absent."""
        results_file = tmp_path / "acceptance_results.json"
        results_file.write_text(json.dumps({
            "passed": 9,
            "failed": 1,
            "total": 10,
        }))
        calc, weights = self._make_calc()
        signal = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert signal is not None
        assert signal.value == pytest.approx(0.9)

    def test_skips_pre_implementation_placeholder(self, tmp_path):
        """Returns None for the pre-implementation placeholder (status='tests_written', total=0)."""
        results_file = tmp_path / "acceptance_results.json"
        results_file.write_text(json.dumps({
            "phase": "acceptance_test",
            "status": "tests_written",
            "passed": 0,
            "failed": 0,
            "total": 0,
            "pass_rate": 0.0,
            "note": "Tests written pre-implementation.",
        }))
        calc, weights = self._make_calc()
        result = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert result is None, (
            "Pre-implementation placeholder should be skipped (status=tests_written, total=0)"
        )

    def test_returns_none_for_invalid_json(self, tmp_path):
        """Returns None (with warning) when acceptance_results.json is invalid JSON."""
        results_file = tmp_path / "acceptance_results.json"
        results_file.write_text("{not valid json}")
        calc, weights = self._make_calc()
        result = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert result is None

    def test_returns_none_when_no_usable_rate_fields(self, tmp_path):
        """Returns None when JSON lacks both pass_rate and passed/total."""
        results_file = tmp_path / "acceptance_results.json"
        results_file.write_text(json.dumps({"status": "complete"}))
        calc, weights = self._make_calc()
        result = calc._extract_acceptance_pass_rate(tmp_path, weights)
        assert result is None


# ---------------------------------------------------------------------------
# compute_confidence integration
# ---------------------------------------------------------------------------

class TestComputeConfidenceIntegration:
    """acceptance_pass_rate is included in composite score when file is present."""

    def test_acceptance_pass_rate_included_in_signals(self, tmp_path):
        """compute_confidence emits acceptance_pass_rate signal when results file exists."""
        from orchestration_engine.confidence import ConfidenceCalculator

        # Write a minimal task file so compute_confidence doesn't return early
        task_file = tmp_path / "task_001.json"
        task_file.write_text(json.dumps({"state": "success", "confidence": 0.9}))

        # Write acceptance results
        (tmp_path / "acceptance_results.json").write_text(json.dumps({
            "phase": "implement",
            "status": "complete",
            "passed": 10,
            "failed": 0,
            "total": 10,
            "pass_rate": 1.0,
        }))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        signal_names = [s.name for s in result.signals]
        assert "acceptance_pass_rate" in signal_names, (
            f"acceptance_pass_rate not in signals: {signal_names}"
        )

    def test_acceptance_pass_rate_not_included_when_file_absent(self, tmp_path):
        """compute_confidence does NOT emit acceptance_pass_rate when results file is absent."""
        from orchestration_engine.confidence import ConfidenceCalculator

        task_file = tmp_path / "task_001.json"
        task_file.write_text(json.dumps({"state": "success", "confidence": 0.9}))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_path)
        signal_names = [s.name for s in result.signals]
        assert "acceptance_pass_rate" not in signal_names, (
            "acceptance_pass_rate should be absent when acceptance_results.json missing"
        )

    def test_full_pass_boosts_composite_score_vs_zero(self, tmp_path):
        """100% acceptance pass rate yields higher score than 0% pass rate.

        Note: both scenarios have the same file layout (task + acceptance_results.json)
        to ensure task-count-based signals (test_pass_rate, change_complexity) are
        identical, isolating the effect of the acceptance_pass_rate signal value.
        """
        from orchestration_engine.confidence import ConfidenceCalculator

        task_file = tmp_path / "task_001.json"
        task_file.write_text(json.dumps({"state": "success", "confidence": 0.8}))

        calc = ConfidenceCalculator()

        # Scenario A: 0% acceptance pass rate
        (tmp_path / "acceptance_results.json").write_text(json.dumps({
            "passed": 0, "failed": 10, "total": 10, "pass_rate": 0.0,
        }))
        result_zero = calc.compute_confidence(tmp_path)

        # Scenario B: 100% acceptance pass rate (same file layout, only value changes)
        (tmp_path / "acceptance_results.json").write_text(json.dumps({
            "passed": 10, "failed": 0, "total": 10, "pass_rate": 1.0,
        }))
        result_full = calc.compute_confidence(tmp_path)

        assert result_full.composite_score > result_zero.composite_score, (
            "100% acceptance pass rate should yield higher composite score than 0%"
        )

    def test_zero_pass_rate_lowers_composite_score(self, tmp_path):
        """0% acceptance pass rate lowers composite score vs no file."""
        from orchestration_engine.confidence import ConfidenceCalculator

        task_file = tmp_path / "task_001.json"
        task_file.write_text(json.dumps({"state": "success", "confidence": 0.9}))

        calc = ConfidenceCalculator()
        result_without = calc.compute_confidence(tmp_path)

        (tmp_path / "acceptance_results.json").write_text(json.dumps({
            "passed": 0, "failed": 10, "total": 10, "pass_rate": 0.0,
        }))
        result_with = calc.compute_confidence(tmp_path)

        assert result_with.composite_score < result_without.composite_score, (
            "Zero acceptance pass rate should lower composite score"
        )


# ---------------------------------------------------------------------------
# Scenario file
# ---------------------------------------------------------------------------

class TestScenarioFile:
    """coding-pipeline-v1-smoke.yaml should reference the 6-phase pipeline."""

    def test_scenario_version_bumped(self):
        """Scenario version is 1.1.0 (was 1.0.0)."""
        scenario = yaml.safe_load(SCENARIO_PATH.read_text())
        assert scenario.get("version") == "1.1.0", (
            f"Expected scenario version '1.1.0', got {scenario.get('version')!r}"
        )

    def test_scenario_description_mentions_6_phase(self):
        """Scenario description mentions 6-phase pipeline."""
        scenario = yaml.safe_load(SCENARIO_PATH.read_text())
        desc = scenario.get("description", "")
        assert "6-phase" in desc or "6 phase" in desc, (
            "Scenario description should mention '6-phase'"
        )

    def test_structural_keywords_include_acceptance_test(self):
        """Structural keywords include 'acceptance_test'."""
        scenario = yaml.safe_load(SCENARIO_PATH.read_text())
        checks = scenario.get("acceptance", [])
        keyword_check = next(
            (c for c in checks if c.get("id") == "structural_keywords"), None
        )
        assert keyword_check is not None, "structural_keywords check not found"
        keywords = keyword_check.get("keywords", [])
        assert "acceptance_test" in keywords, (
            f"'acceptance_test' not in structural_keywords: {keywords}"
        )

    def test_llm_judge_rubric_mentions_acceptance(self):
        """LLM judge rubric mentions acceptance tests or behavioral contracts."""
        scenario = yaml.safe_load(SCENARIO_PATH.read_text())
        checks = scenario.get("acceptance", [])
        judge_check = next(
            (c for c in checks if c.get("id") == "output_quality"), None
        )
        assert judge_check is not None, "output_quality check not found"
        rubric = judge_check.get("rubric", "")
        assert "acceptance" in rubric.lower() or "behavioral" in rubric.lower(), (
            "LLM judge rubric should mention acceptance tests or behavioral contracts"
        )
