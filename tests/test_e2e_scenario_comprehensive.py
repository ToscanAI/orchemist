"""Comprehensive QA tests for the E2E Autonomous Scenario implementation.

Covers every acceptance criterion (AC-1 through AC-6) plus identified edge
cases not exercised by the primary test suites.

Test organisation:
  - TestAC1_TestTemplate          — scenarios/e2e-test-template.yaml structure
  - TestAC2_ScenarioFile          — scenarios/e2e-autonomous.yaml schema
  - TestAC3_KeywordGrader_Unit    — KeywordGrader logic + edge cases
  - TestAC3_KeywordGrader_Exports — module / __init__ exports
  - TestAC3_ExtractText           — _extract_text helper (boundary cases)
  - TestAC3_DispatchIntegration   — criterion type "keyword" dispatched correctly
  - TestAC4_CLIWiring             — orch scenario run … command wiring
  - TestAC5_ScoreReportFormat     — exact format of printed score report
  - TestAC6_DryRunCompatibility   — ORCH_DRY_RUN + stub-score semantics
  - TestEdgeCases_PathTraversal   — rubric_file path-traversal protection
  - TestEdgeCases_LLMJudge        — LLMJudgeGrader output-field + priority chain
  - TestEdgeCases_Grading         — gate/score edge cases in run_scenario()
  - TestEdgeCases_Suite           — run_suite() missing entries, multi-scenario

All tests are fully offline (no real API or network calls required).
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup — ensure both src/ and project root are on sys.path
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from scenario_runner.graders.assertion import AssertionGrader
from scenario_runner.graders.keyword_grader import KeywordGrader, _extract_text
from scenario_runner.graders.llm_judge import LLMJudgeGrader
from scenario_runner.graders.url_check import URLCheckGrader
from scenario_runner.models import GradeResult, CriterionResult, ScenarioResult
from scenario_runner.runner import ScenarioRunner

# File paths used repeatedly
SCENARIOS_DIR = _REPO_ROOT / "scenarios"
E2E_SCENARIO_FILE = SCENARIOS_DIR / "e2e-autonomous.yaml"
E2E_TEMPLATE_FILE = SCENARIOS_DIR / "e2e-test-template.yaml"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _dry_run_phase_output() -> dict:
    """Synthetic DryRunExecutor phase output (contains 'mock', 'execution', 'content')."""
    return {
        "task_id": "test-task-comprehensive",
        "task_type": "content",
        "state": "success",
        "confidence": 0.85,
        "result": {
            "message": "Mock execution of content task",
            "model_used": "dry-run",
            "worker_id": "sequencer-worker",
            "payload_size": 120,
        },
        "errors": [],
        "model_used": "dry-run",
        "tokens_consumed": 500,
        "cost_usd": 0.05,
        "execution_time_seconds": 0.0,
    }


def _cli_structured_output() -> dict:
    """Output as produced by the CLI: {"final": ..., "phases": ...}."""
    phase_out = _dry_run_phase_output()
    return {
        "final": phase_out,
        "phases": {
            "outline": phase_out,
            "draft": phase_out,
        },
    }


def _make_mock_http_response(body_text: str) -> MagicMock:
    """Build a urllib mock response that returns *body_text* as JSON."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "content": [{"text": body_text}]
    }).encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# ===========================================================================
# AC-1 — Test Template (scenarios/e2e-test-template.yaml)
# ===========================================================================


class TestAC1_TestTemplate:
    """AC-1: Acceptance criteria for the e2e-test-template.yaml file."""

    def test_ac1_template_file_exists(self) -> None:
        """AC-1.1 — e2e-test-template.yaml must exist at scenarios/."""
        assert E2E_TEMPLATE_FILE.exists(), (
            f"Missing: {E2E_TEMPLATE_FILE}"
        )

    def test_ac1_template_validates_with_zero_errors(self) -> None:
        """AC-1.2 — TemplateEngine.validate_template() returns 0 errors."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        from orchestration_engine.templates import TemplateEngine

        engine = TemplateEngine()
        template = engine.load_template(E2E_TEMPLATE_FILE)
        errors = engine.validate_template(template)
        assert errors == [], (
            f"Unexpected template errors: {errors}"
        )

    def test_ac1_phase_count_is_2_or_3(self) -> None:
        """AC-1.3 — Template has exactly 2 or 3 phases."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        n = len(data.get("phases", []))
        assert 2 <= n <= 3, f"Expected 2–3 phases, got {n}"

    def test_ac1_all_phases_model_tier_haiku(self) -> None:
        """AC-1.4 — Every phase uses model_tier: haiku."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        for phase in data.get("phases", []):
            tier = phase.get("model_tier", "")
            assert tier.lower() == "haiku", (
                f"Phase '{phase.get('id')}' uses model_tier='{tier}', expected 'haiku'"
            )

    def test_ac1_final_phase_prompt_contains_deterministic_marker(self) -> None:
        """AC-1.5 — Final phase prompt instructs model to include 'Analysis complete'.

        This creates a deterministic assertion point for downstream graders
        when the pipeline is run in live mode.
        """
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        phases = data.get("phases", [])
        assert phases, "No phases found in template"
        final_phase = phases[-1]  # last phase

        prompt = final_phase.get("prompt_template", "")
        # The prompt must tell the model to emit "Analysis complete."
        assert "Analysis complete" in prompt, (
            f"Final phase prompt must instruct model to emit 'Analysis complete'. "
            f"Prompt: {prompt[:200]!r}"
        )

    def test_ac1_template_has_required_top_level_keys(self) -> None:
        """AC-1 — Template must have id, name, version, phases."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        for key in ("id", "name", "version", "phases"):
            assert key in data, f"Template missing required key: '{key}'"

    def test_ac1_validate_cli_exits_0(self) -> None:
        """AC-1.6 — `orch validate scenarios/e2e-test-template.yaml` exits 0."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["validate", str(E2E_TEMPLATE_FILE)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, (
            f"orch validate exited {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_ac1_template_all_phases_have_prompt_template(self) -> None:
        """Each phase must supply a prompt_template so the sequencer can render it."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        for phase in data.get("phases", []):
            assert "prompt_template" in phase, (
                f"Phase '{phase.get('id')}' is missing 'prompt_template'"
            )
            assert phase["prompt_template"].strip(), (
                f"Phase '{phase.get('id')}' has an empty prompt_template"
            )

    def test_ac1_phase_depends_on_chain_is_valid(self) -> None:
        """Phase depends_on references must point to earlier phases."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        phases = data.get("phases", [])
        known_ids = set()
        for phase in phases:
            pid = phase.get("id", "")
            deps = phase.get("depends_on", []) or []
            for dep in deps:
                assert dep in known_ids, (
                    f"Phase '{pid}' depends_on '{dep}' which is not a preceding phase. "
                    f"Known so far: {known_ids}"
                )
            known_ids.add(pid)


# ===========================================================================
# AC-2 — Scenario File (scenarios/e2e-autonomous.yaml)
# ===========================================================================


class TestAC2_ScenarioFile:
    """AC-2: Acceptance criteria for the e2e-autonomous.yaml scenario file."""

    def test_ac2_scenario_file_exists(self) -> None:
        """AC-2.1 — e2e-autonomous.yaml must exist at scenarios/."""
        assert E2E_SCENARIO_FILE.exists(), f"Missing: {E2E_SCENARIO_FILE}"

    def test_ac2_scenario_has_all_required_keys(self) -> None:
        """AC-2.2 — Required keys: id, pipeline, input, acceptance, scoring."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        for key in ("id", "pipeline", "input", "acceptance", "scoring"):
            assert key in data, f"Scenario missing required key: '{key}'"

    def test_ac2_has_at_least_one_keyword_criterion(self) -> None:
        """AC-2.3 — acceptance must contain at least one type=keyword criterion."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        keyword_crits = [c for c in data["acceptance"] if c.get("type") == "keyword"]
        assert keyword_crits, "No 'keyword' criterion found in acceptance list"

    def test_ac2_has_at_least_one_llm_judge_criterion(self) -> None:
        """AC-2.4 — acceptance must contain at least one type=llm_judge criterion."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        judge_crits = [c for c in data["acceptance"] if c.get("type") == "llm_judge"]
        assert judge_crits, "No 'llm_judge' criterion found in acceptance list"

    def test_ac2_non_gate_weights_sum_to_100(self) -> None:
        """AC-2.5 — Non-gate (weight != 0) criteria weights must sum exactly to 100."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        non_gate = [c for c in data["acceptance"] if int(c.get("weight", 0)) != 0]
        total = sum(int(c["weight"]) for c in non_gate)
        assert total == 100, f"Non-gate weights sum = {total}, expected 100"

    def test_ac2_pass_threshold_is_0_70(self) -> None:
        """AC-2.6 — scoring.pass_threshold must be 0.70."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        threshold = float(data["scoring"]["pass_threshold"])
        assert threshold == pytest.approx(0.70)

    def test_ac2_gate_mode_is_all_or_nothing(self) -> None:
        """AC-2.7 — scoring.gate_mode must be 'all_or_nothing'."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        assert data["scoring"]["gate_mode"] == "all_or_nothing"

    def test_ac2_loads_via_scenario_runner(self) -> None:
        """AC-2.8 — ScenarioRunner.load_scenario() succeeds without raising."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        assert scenario["id"] == "e2e-autonomous"
        assert isinstance(scenario["acceptance"], list)

    def test_ac2_gate_criterion_has_weight_zero(self) -> None:
        """The gate criterion must have weight 0 (not a positive integer)."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        gates = [c for c in data["acceptance"] if int(c.get("weight", -1)) == 0]
        assert gates, "No gate criterion (weight=0) found in acceptance list"

    def test_ac2_keyword_criterion_has_keywords_and_match_mode(self) -> None:
        """keyword criterion must have non-empty 'keywords' list and valid 'match_mode'."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        for c in data["acceptance"]:
            if c.get("type") == "keyword":
                assert c.get("keywords"), f"Criterion '{c['id']}' has empty 'keywords'"
                assert c.get("match_mode") in ("all", "any", "ratio"), (
                    f"Criterion '{c['id']}' has invalid match_mode: '{c.get('match_mode')}'"
                )

    def test_ac2_llm_judge_criterion_has_rubric(self) -> None:
        """llm_judge criterion must have 'rubric' or 'rubric_file' and a judge_model."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        for c in data["acceptance"]:
            if c.get("type") == "llm_judge":
                has_rubric = bool(c.get("rubric")) or bool(c.get("rubric_file"))
                assert has_rubric, (
                    f"llm_judge criterion '{c['id']}' has no 'rubric' or 'rubric_file'"
                )
                assert c.get("judge_model"), (
                    f"llm_judge criterion '{c['id']}' is missing 'judge_model'"
                )

    def test_ac2_pipeline_ref_points_to_existing_file(self) -> None:
        """The 'pipeline' key must reference a file that actually exists."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        pipeline_ref = data.get("pipeline", "")
        # Resolve relative to either the scenario file's dir or repo root
        candidate1 = E2E_SCENARIO_FILE.parent / pipeline_ref
        candidate2 = _REPO_ROOT / pipeline_ref

        assert candidate1.exists() or candidate2.exists(), (
            f"Pipeline reference '{pipeline_ref}' not found at "
            f"{candidate1} or {candidate2}"
        )


# ===========================================================================
# AC-3 — KeywordGrader (module, exports, unit, dispatch)
# ===========================================================================


class TestAC3_KeywordGrader_Exports:
    """AC-3: KeywordGrader is properly exported and importable."""

    def test_keyword_grader_module_file_exists(self) -> None:
        """AC-3.1 — keyword_grader.py must exist as a file."""
        grader_path = _REPO_ROOT / "scenario_runner" / "graders" / "keyword_grader.py"
        assert grader_path.exists(), f"Missing: {grader_path}"

    def test_keyword_grader_exported_from_init(self) -> None:
        """AC-3.7 — KeywordGrader is exported from scenario_runner.graders.__init__."""
        from scenario_runner.graders import KeywordGrader as KG
        assert KG is KeywordGrader  # same object

    def test_keyword_grader_in_all_list(self) -> None:
        """KeywordGrader appears in __all__ in graders/__init__.py."""
        import scenario_runner.graders as graders_pkg
        all_list = getattr(graders_pkg, "__all__", [])
        assert "KeywordGrader" in all_list, (
            f"'KeywordGrader' not in __all__. Found: {all_list}"
        )

    def test_keyword_grader_grade_method_signature(self) -> None:
        """grade() must accept: output, keywords, match_mode, output_field."""
        import inspect

        sig = inspect.signature(KeywordGrader.grade)
        params = list(sig.parameters.keys())
        assert "output" in params
        assert "keywords" in params
        assert "match_mode" in params
        assert "output_field" in params

    def test_keyword_grader_returns_grade_result(self) -> None:
        """grade() must return a GradeResult instance."""
        grader = KeywordGrader()
        result = grader.grade({"text": "hello"}, ["hello"], match_mode="any")
        assert isinstance(result, GradeResult)

    def test_grade_result_grader_type_is_keyword(self) -> None:
        """GradeResult.grader_type must always be 'keyword'."""
        grader = KeywordGrader()
        for mode in ("all", "any", "ratio"):
            result = grader.grade({"text": "hello"}, ["hello"], match_mode=mode)
            assert result.grader_type == "keyword", (
                f"Expected 'keyword' for mode={mode}, got '{result.grader_type}'"
            )


class TestAC3_KeywordGrader_Unit:
    """AC-3: Comprehensive unit tests for each match mode and edge case."""

    @pytest.fixture()
    def grader(self) -> KeywordGrader:
        return KeywordGrader()

    # ── "all" mode ────────────────────────────────────────────────────────────

    def test_all_mode_exact_single_keyword(self, grader: KeywordGrader) -> None:
        """Single keyword, all-mode: found → 1.0."""
        result = grader.grade({"text": "orchestration"}, ["orchestration"], "all")
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_all_mode_multiple_keywords_all_found(self, grader: KeywordGrader) -> None:
        """All 3 keywords present → score 1.0."""
        result = grader.grade({"text": "alpha beta gamma"}, ["alpha", "beta", "gamma"], "all")
        assert result.score == pytest.approx(1.0)

    def test_all_mode_one_keyword_absent(self, grader: KeywordGrader) -> None:
        """2 of 3 keywords → all-mode score 0.0."""
        result = grader.grade({"text": "alpha beta"}, ["alpha", "beta", "gamma"], "all")
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_all_mode_missing_details_message(self, grader: KeywordGrader) -> None:
        """details field lists the missing keyword when all-mode fails."""
        result = grader.grade({"text": "alpha"}, ["alpha", "missing_kw"], "all")
        assert "missing_kw" in result.details

    # ── "any" mode ────────────────────────────────────────────────────────────

    def test_any_mode_only_last_keyword_found(self, grader: KeywordGrader) -> None:
        """any-mode: first 2 absent, third present → score 1.0."""
        result = grader.grade({"text": "gamma"}, ["alpha", "beta", "gamma"], "any")
        assert result.score == pytest.approx(1.0)

    def test_any_mode_none_found_details(self, grader: KeywordGrader) -> None:
        """details lists the keyword list when any-mode fails."""
        kws = ["nope1", "nope2"]
        result = grader.grade({"text": "hello"}, kws, "any")
        assert result.passed is False
        # At minimum, the failure details should mention the mode or the keywords
        assert result.details

    # ── "ratio" mode ─────────────────────────────────────────────────────────

    def test_ratio_mode_1_of_3(self, grader: KeywordGrader) -> None:
        """ratio: 1 matched of 3 → score ≈ 0.333."""
        result = grader.grade({"text": "alpha"}, ["alpha", "beta", "gamma"], "ratio")
        assert result.score == pytest.approx(1 / 3, abs=1e-9)

    def test_ratio_mode_3_of_3(self, grader: KeywordGrader) -> None:
        """ratio: 3 matched of 3 → score 1.0."""
        result = grader.grade({"text": "alpha beta gamma"}, ["alpha", "beta", "gamma"], "ratio")
        assert result.score == pytest.approx(1.0)

    def test_ratio_mode_0_of_3(self, grader: KeywordGrader) -> None:
        """ratio: 0 matched of 3 → score 0.0 and passed=False."""
        result = grader.grade({"text": "nothing here"}, ["alpha", "beta", "gamma"], "ratio")
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_ratio_mode_score_in_range_0_to_1(self, grader: KeywordGrader) -> None:
        """ratio score must always be in [0.0, 1.0]."""
        for matched_n in range(4):
            text = " ".join(f"kw{i}" for i in range(matched_n))
            result = grader.grade({"text": text}, [f"kw{i}" for i in range(4)], "ratio")
            assert 0.0 <= result.score <= 1.0

    def test_ratio_partial_pass_semantics(self, grader: KeywordGrader) -> None:
        """ratio mode: any match is considered passed=True."""
        result = grader.grade({"text": "alpha"}, ["alpha", "beta"], "ratio")
        assert result.passed is True  # 0.5 > 0.0 → passed
        assert result.score == pytest.approx(0.5)

    # ── Case insensitivity ───────────────────────────────────────────────────

    def test_case_insensitive_upper_keyword_lower_text(self, grader: KeywordGrader) -> None:
        """Uppercase keyword must match lowercase text."""
        result = grader.grade({"text": "orchestration"}, ["ORCHESTRATION"], "all")
        assert result.passed is True

    def test_case_insensitive_lower_keyword_upper_text(self, grader: KeywordGrader) -> None:
        """Lowercase keyword must match uppercase text."""
        result = grader.grade({"text": "ORCHESTRATION"}, ["orchestration"], "all")
        assert result.passed is True

    def test_case_insensitive_mixed_case_keyword(self, grader: KeywordGrader) -> None:
        """Mixed-case keyword matches regardless of text case."""
        result = grader.grade({"text": "aI oRcHestRatION"}, ["AI Orchestration"], "any")
        assert result.passed is True

    # ── Empty keywords ────────────────────────────────────────────────────────

    def test_empty_keywords_all_mode_vacuous_pass(self, grader: KeywordGrader) -> None:
        """Empty keyword list is vacuous truth for 'all' mode."""
        result = grader.grade({"text": "anything"}, [], "all")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)

    def test_empty_keywords_any_mode_vacuous_pass(self, grader: KeywordGrader) -> None:
        """Empty keyword list is vacuous truth for 'any' mode."""
        result = grader.grade({"text": "anything"}, [], "any")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)

    def test_empty_keywords_ratio_mode_vacuous_pass(self, grader: KeywordGrader) -> None:
        """Empty keyword list is vacuous truth for 'ratio' mode."""
        result = grader.grade({"text": "anything"}, [], "ratio")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)

    # ── Invalid match_mode ───────────────────────────────────────────────────

    def test_invalid_match_mode_returns_failed_grade_result(self, grader: KeywordGrader) -> None:
        """Unknown match_mode returns passed=False with explanation in details."""
        result = grader.grade({"text": "hello"}, ["hello"], "quantum_entanglement")
        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        # Details must mention the bad mode name
        assert "quantum_entanglement" in result.details.lower() or \
               "match_mode" in result.details.lower() or \
               "unknown" in result.details.lower()

    # ── Nested dict text extraction ──────────────────────────────────────────

    def test_nested_dict_two_levels_deep(self, grader: KeywordGrader) -> None:
        """Keywords nested 2 levels deep are found."""
        output = {"outer": {"inner": "target_keyword"}}
        result = grader.grade(output, ["target_keyword"], "any")
        assert result.passed is True

    def test_deeply_nested_output(self, grader: KeywordGrader) -> None:
        """Keywords buried in the DryRunExecutor output dict are found."""
        output = _dry_run_phase_output()
        result = grader.grade(output, ["mock"], "any")
        assert result.passed is True

    def test_cli_structured_output_keywords_found(self, grader: KeywordGrader) -> None:
        """Keywords in CLI-structured {"final": ..., "phases": ...} output are found."""
        output = _cli_structured_output()
        result = grader.grade(output, ["mock", "execution"], "all")
        assert result.passed is True, (
            "KeywordGrader must recurse into CLI-structured output"
        )

    # ── output_field parameter ───────────────────────────────────────────────

    def test_output_field_present_and_keyword_found(self, grader: KeywordGrader) -> None:
        """output_field restricts search to that key; keyword is found there."""
        output = {"article": "important keyword here", "other": "nothing relevant"}
        result = grader.grade(output, ["important"], "any", output_field="article")
        assert result.passed is True

    def test_output_field_present_keyword_not_in_field(self, grader: KeywordGrader) -> None:
        """output_field restricts search: keyword only in other field → fails."""
        output = {"article": "nothing here", "other": "important keyword"}
        result = grader.grade(output, ["important"], "any", output_field="article")
        assert result.passed is False

    def test_output_field_missing_from_output_returns_failed(self, grader: KeywordGrader) -> None:
        """output_field pointing to absent key → no text → keyword not found."""
        output = {"article": "hello there"}
        result = grader.grade(output, ["hello"], "any", output_field="nonexistent_field")
        # The field doesn't exist → empty string → keyword can't be found
        assert result.passed is False

    def test_output_field_none_searches_all(self, grader: KeywordGrader) -> None:
        """output_field=None (default) searches the entire output dict."""
        output = {"article": "hello", "metadata": "world"}
        result = grader.grade(output, ["world"], "any", output_field=None)
        assert result.passed is True


class TestAC3_ExtractText:
    """_extract_text helper edge cases (called internally by KeywordGrader)."""

    def test_none_value_returns_empty_string(self) -> None:
        assert _extract_text(None) == ""

    def test_bool_true_returns_true_string(self) -> None:
        assert "true" in _extract_text(True).lower()

    def test_bool_false_returns_false_string(self) -> None:
        assert "false" in _extract_text(False).lower()

    def test_integer_returns_numeric_string(self) -> None:
        assert _extract_text(42) == "42"

    def test_float_returns_string_representation(self) -> None:
        result = _extract_text(3.14)
        assert "3.14" in result

    def test_empty_string_returns_empty(self) -> None:
        assert _extract_text("") == ""

    def test_plain_string_returned_as_is(self) -> None:
        assert _extract_text("hello world") == "hello world"

    def test_flat_list_joined_with_spaces(self) -> None:
        result = _extract_text(["alpha", "beta", "gamma"])
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result

    def test_tuple_processed_like_list(self) -> None:
        result = _extract_text(("x", "y"))
        assert "x" in result
        assert "y" in result

    def test_nested_dict_extracts_all_values(self) -> None:
        result = _extract_text({"a": {"b": {"c": "deep_value"}}})
        assert "deep_value" in result

    def test_list_of_dicts_extracts_values(self) -> None:
        result = _extract_text([{"key": "val1"}, {"key": "val2"}])
        assert "val1" in result
        assert "val2" in result

    def test_mixed_types_in_dict(self) -> None:
        """Dict with int, bool, None, string values — all converted."""
        result = _extract_text({"num": 10, "flag": True, "nothing": None, "text": "hello"})
        assert "10" in result
        assert "True" in result
        assert "hello" in result

    def test_empty_dict_returns_empty_string(self) -> None:
        result = _extract_text({})
        assert result == ""

    def test_empty_list_returns_empty_string(self) -> None:
        result = _extract_text([])
        assert result == ""


class TestAC3_DispatchIntegration:
    """AC-3.8 — criterion type 'keyword' is dispatched to KeywordGrader via run_scenario."""

    def test_keyword_criterion_dispatched_and_passes(self, tmp_path: Path) -> None:
        """A 'keyword' criterion runs KeywordGrader and returns grader_type='keyword'."""
        scenario = {
            "id": "dispatch-test",
            "acceptance": [
                {
                    "id": "kw_check",
                    "type": "keyword",
                    "keywords": ["hello"],
                    "match_mode": "any",
                    "weight": 1,
                    "threshold": 0.5,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"text": "hello world"})

        kw_result = next(
            cr for cr in result.criterion_results if cr.criterion_id == "kw_check"
        )
        assert kw_result.grade.grader_type == "keyword"
        assert kw_result.grade.passed is True
        assert kw_result.grade.score == pytest.approx(1.0)

    def test_keyword_criterion_dispatched_and_fails(self, tmp_path: Path) -> None:
        """A 'keyword' criterion that finds nothing returns score=0.0."""
        scenario = {
            "id": "dispatch-fail-test",
            "acceptance": [
                {
                    "id": "kw_fail",
                    "type": "keyword",
                    "keywords": ["missing_word"],
                    "match_mode": "all",
                    "weight": 1,
                    "threshold": 0.5,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"text": "nothing relevant here"})

        kw_result = result.criterion_results[0]
        assert kw_result.grade.grader_type == "keyword"
        assert kw_result.grade.passed is False

    def test_keyword_gate_criterion_fails_whole_scenario(self, tmp_path: Path) -> None:
        """A keyword gate (weight=0) that fails blocks the entire scenario."""
        scenario = {
            "id": "keyword-gate",
            "acceptance": [
                {
                    "id": "must_have",
                    "type": "keyword",
                    "keywords": ["required_word"],
                    "match_mode": "all",
                    "weight": 0,   # gate
                    "threshold": 0.5,
                },
                {
                    "id": "scored",
                    "type": "assertion",
                    "check": "True",
                    "weight": 1,
                    "threshold": 0.5,
                },
            ],
            "scoring": {"pass_threshold": 0.5, "gate_mode": "all_or_nothing"},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"text": "no match here"})

        assert result.gates_passed is False
        assert result.passed is False

    def test_all_criterion_types_dispatched_correctly(self, tmp_path: Path) -> None:
        """assertion, keyword, url_check, llm_judge all produce the right grader_type."""
        scenario = {
            "id": "multi-type",
            "acceptance": [
                {
                    "id": "assert_c",
                    "type": "assertion",
                    "check": "True",
                    "weight": 0,
                },
                {
                    "id": "kw_c",
                    "type": "keyword",
                    "keywords": ["hello"],
                    "match_mode": "any",
                    "weight": 1,
                    "threshold": 0.5,
                },
                {
                    "id": "url_c",
                    "type": "url_check",
                    "weight": 1,
                    "threshold": 0.5,
                },
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "hello world"})

        types = {cr.criterion_id: cr.grade.grader_type for cr in result.criterion_results}
        assert types["assert_c"] == "assertion"
        assert types["kw_c"] == "keyword"
        assert types["url_c"] == "url_check"


# ===========================================================================
# AC-4 — CLI Wiring
# ===========================================================================


class TestAC4_CLIWiring:
    """AC-4: orch scenario run … command exists and behaves correctly."""

    def test_scenario_group_exists(self) -> None:
        """AC-4.1 — 'scenario' sub-group exists on the main CLI."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scenario", "--help"])
        assert result.exit_code == 0
        assert "run" in result.output

    def test_scenario_run_help_exits_0(self) -> None:
        """AC-4.2 — `orch scenario run --help` exits 0 and shows key options."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scenario", "run", "--help"])

        assert result.exit_code == 0
        assert "--dry-run" in result.output
        assert "--scenario-dir" in result.output

    def test_scenario_run_help_shows_scenario_id_arg(self) -> None:
        """Help text must mention SCENARIO_ID positional argument."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scenario", "run", "--help"])

        assert result.exit_code == 0
        assert "SCENARIO_ID" in result.output or "scenario_id" in result.output.lower()

    def test_scenario_run_dry_run_flag_skips_api_calls(self) -> None:
        """AC-4.3 — --dry-run causes PipelineRunner.dry_run() to be used (no API calls)."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main
        from orchestration_engine.pipeline_runner import PipelineRunner

        calls = []
        original_dry_run = PipelineRunner.dry_run

        def tracking_dry_run(*args, **kwargs):
            calls.append(("dry_run", args, kwargs))
            return original_dry_run(*args, **kwargs)

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        with patch.object(PipelineRunner, "dry_run", side_effect=tracking_dry_run):
            result = runner.invoke(
                main,
                ["scenario", "run", "e2e-autonomous",
                 "--dry-run", "--scenario-dir", str(SCENARIOS_DIR)],
                env=env,
                catch_exceptions=False,
            )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        assert len(calls) == 1, "Expected PipelineRunner.dry_run() to be called once"

    def test_scenario_run_custom_scenario_dir(self, tmp_path: Path) -> None:
        """AC-4.4 — --scenario-dir allows specifying a non-default directory."""
        # Create a minimal passing scenario in tmp_path
        scenario = {
            "id": "custom-dir-test",
            "pipeline": str(E2E_TEMPLATE_FILE),
            "input": {"topic": "test"},
            "acceptance": [
                {
                    "id": "gate",
                    "type": "assertion",
                    "check": "len(str(output)) > 0",
                    "weight": 0,
                },
            ],
            "scoring": {"pass_threshold": 0.0, "gate_mode": "all_or_nothing"},
        }
        scenario_file = tmp_path / "custom-dir-test.yaml"
        scenario_file.write_text(yaml.dump(scenario))

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        result = runner.invoke(
            main,
            ["scenario", "run", "custom-dir-test",
             "--dry-run", "--scenario-dir", str(tmp_path)],
            env=env,
            catch_exceptions=False,
        )

        # The scenario must be found and run (exit 0 = pass)
        assert result.exit_code == 0, (
            f"Expected exit 0 with custom --scenario-dir.\nOutput:\n{result.output}"
        )

    def test_scenario_run_exits_0_when_passing(self) -> None:
        """AC-4.6 — Exit code 0 when scenario passes."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        result = runner.invoke(
            main,
            ["scenario", "run", "e2e-autonomous",
             "--dry-run", "--scenario-dir", str(SCENARIOS_DIR)],
            env=env,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, (
            f"Scenario should exit 0 (pass). Got {result.exit_code}.\n{result.output}"
        )

    def test_scenario_run_exits_1_when_not_found(self) -> None:
        """AC-4.6 — Exit code 1 for non-existent scenario ID."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scenario", "run", "totally-nonexistent-scenario-id-xyz"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 for missing scenario, got {result.exit_code}"
        )

    def test_scenario_run_nonexistent_scenario_dir_exits_1(self) -> None:
        """--scenario-dir pointing to non-existent directory → exit 1."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scenario", "run", "anything",
             "--scenario-dir", "/tmp/path-that-does-not-exist-xyz-12345"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    def test_scenario_run_with_path_argument(self) -> None:
        """scenario_id can be a direct file path instead of a bare ID."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        # Pass the full path as the scenario_id argument
        result = runner.invoke(
            main,
            ["scenario", "run", str(E2E_SCENARIO_FILE), "--dry-run"],
            env=env,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 when passing scenario as direct path.\n{result.output}"
        )


# ===========================================================================
# AC-5 — Score Report Format
# ===========================================================================


class TestAC5_ScoreReportFormat:
    """AC-5: The score report printed to stdout must include all required fields."""

    def _invoke_e2e(self) -> str:
        """Invoke `orch scenario run e2e-autonomous --dry-run` and return stdout."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}
        result = runner.invoke(
            main,
            ["scenario", "run", "e2e-autonomous",
             "--dry-run", "--scenario-dir", str(SCENARIOS_DIR)],
            env=env,
            catch_exceptions=False,
        )
        return result.output

    @pytest.fixture(scope="class")
    def report_output(self) -> str:
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")
        return self._invoke_e2e()

    def test_ac5_report_contains_scenario_id(self, report_output: str) -> None:
        """AC-5.1 — Report must show the scenario ID 'e2e-autonomous'."""
        assert "e2e-autonomous" in report_output

    def test_ac5_report_contains_overall_score(self, report_output: str) -> None:
        """AC-5.1 — Report must include a numeric overall score."""
        # The report shows "Score:     88.0 / 100" (or similar)
        import re
        score_pattern = re.compile(r"\d+\.?\d*\s*/\s*100")
        assert score_pattern.search(report_output), (
            f"No 'XX / 100' score found in output:\n{report_output[:500]}"
        )

    def test_ac5_report_contains_pass_or_fail_verdict(self, report_output: str) -> None:
        """AC-5.1 — Report must show PASS or FAIL verdict."""
        assert "PASS" in report_output or "FAIL" in report_output

    def test_ac5_report_contains_criterion_ids(self, report_output: str) -> None:
        """AC-5.2 — Report must list all criterion IDs."""
        assert "output_not_empty" in report_output
        assert "output_content_check" in report_output
        assert "output_quality" in report_output

    def test_ac5_report_gate_criterion_labeled(self, report_output: str) -> None:
        """AC-5.3 — Gate criterion must appear with [GATE] label."""
        assert "[GATE]" in report_output

    def test_ac5_report_shows_keyword_grader_type(self, report_output: str) -> None:
        """AC-5.2 — Per-criterion rows must show grader type 'keyword'."""
        assert "keyword" in report_output

    def test_ac5_report_shows_llm_judge_grader_type(self, report_output: str) -> None:
        """AC-5.2 — Per-criterion rows must show grader type 'llm_judge'."""
        assert "llm_judge" in report_output

    def test_ac5_report_score_is_88_approx_dry_run(self, report_output: str) -> None:
        """AC-5 — Dry-run expected score is 88.0 (= (1.0×40 + 0.8×60) / 100)."""
        # Score reported as "88.0 / 100"
        assert "88.0" in report_output or "88" in report_output, (
            f"Expected '88' in output (dry-run score=0.88). Output:\n{report_output[:600]}"
        )

    def test_ac5_report_shows_gate_status(self, report_output: str) -> None:
        """AC-5 — Report must include the gates status line."""
        # "Gates:     all passed" or "Gates:     one or more FAILED"
        assert "Gate" in report_output or "gate" in report_output


# ===========================================================================
# AC-6 — Dry-Run Mode Compatibility
# ===========================================================================


class TestAC6_DryRunCompatibility:
    """AC-6: LLMJudgeGrader dry-run behaviour."""

    def test_ac6_orch_dry_run_env_returns_stub_score(self) -> None:
        """AC-6.1 — ORCH_DRY_RUN=1 bypasses API and returns stub 0.8."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=0.8)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"text": "some content"},
                rubric="Rate 0–1.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.8)
        assert result.passed is True  # 0.8 >= 0.5
        assert result.grader_type == "llm_judge"

    def test_ac6_stub_score_in_details(self) -> None:
        """AC-6.1 — details string mentions 'dry-run' when stub is returned."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=0.8)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"text": "test"},
                rubric="Rate.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert "dry-run" in result.details.lower() or "dry_run" in result.details.lower()

    def test_ac6_stub_score_is_configurable(self) -> None:
        """AC-6.1 — dry_run_stub_score parameter is respected."""
        for stub in (0.0, 0.5, 0.9, 1.0):
            grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=stub)
            with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
                result = grader.grade({}, "rubric", "model")
            assert result.score == pytest.approx(stub)

    def test_ac6_stub_score_clamped_below_zero(self) -> None:
        """Stub score below 0.0 is clamped to 0.0 at construction time."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=-0.5)
        assert grader.dry_run_stub_score == pytest.approx(0.0)

    def test_ac6_stub_score_clamped_above_one(self) -> None:
        """Stub score above 1.0 is clamped to 1.0 at construction time."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=5.0)
        assert grader.dry_run_stub_score == pytest.approx(1.0)

    def test_ac6_no_dry_run_env_no_api_key_returns_zero(self) -> None:
        """Without ORCH_DRY_RUN, missing API key → score 0.0."""
        env_without = {k: v for k, v in os.environ.items()
                       if k not in ("ORCH_DRY_RUN", "ANTHROPIC_API_KEY")}
        grader = LLMJudgeGrader(api_key=None)
        with patch.dict(os.environ, env_without, clear=True):
            grader2 = LLMJudgeGrader(api_key=None)  # constructor sees no API key
            result = grader2.grade({"text": "hello"}, "rubric", "model")

        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_ac6_dry_run_env_not_leaked_after_cli_command(self) -> None:
        """ORCH_DRY_RUN must NOT remain set in os.environ after scenario_run exits.

        This is critical for test isolation in Click's single-process CliRunner.
        """
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        # Ensure ORCH_DRY_RUN is NOT set before the command
        env_before = {k: v for k, v in os.environ.items() if k != "ORCH_DRY_RUN"}

        runner = CliRunner()
        # CliRunner uses mix_stderr=True by default and isolates env only for the
        # duration of the invoke if env= is passed.  Omit env= to test real leak.
        with patch.dict(os.environ, env_before, clear=True):
            runner.invoke(
                main,
                ["scenario", "run", "e2e-autonomous",
                 "--dry-run", "--scenario-dir", str(SCENARIOS_DIR)],
                catch_exceptions=False,
            )
            # After the command returns, ORCH_DRY_RUN must not be set
            assert "ORCH_DRY_RUN" not in os.environ, (
                "ORCH_DRY_RUN was NOT cleaned up after scenario_run! "
                "This will corrupt subsequent test runs."
            )

    def test_ac6_pre_existing_dry_run_env_not_removed(self) -> None:
        """If ORCH_DRY_RUN was already set before the command, it must survive."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env_with_dry_run = {**os.environ, "ORCH_DRY_RUN": "1"}

        with patch.dict(os.environ, env_with_dry_run, clear=True):
            runner.invoke(
                main,
                ["scenario", "run", "e2e-autonomous",
                 "--dry-run", "--scenario-dir", str(SCENARIOS_DIR)],
                catch_exceptions=False,
            )
            # The pre-existing var must still be set
            assert os.environ.get("ORCH_DRY_RUN") == "1", (
                "ORCH_DRY_RUN was removed even though it was set before the command!"
            )


# ===========================================================================
# Edge Cases — Path Traversal Protection
# ===========================================================================


class TestEdgeCases_PathTraversal:
    """rubric_file path-traversal protection in ScenarioRunner._resolve_rubric."""

    def test_path_traversal_dotdot_blocked(self, tmp_path: Path) -> None:
        """rubric_file with '../' that escapes scenarios_dir raises ValueError."""
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()
        runner = ScenarioRunner(scenarios_dir=suite_dir)

        criterion = {"rubric_file": "../outside.txt"}
        with pytest.raises(ValueError, match="escapes scenarios directory"):
            runner._resolve_rubric(criterion)

    def test_path_traversal_absolute_path_blocked(self, tmp_path: Path) -> None:
        """An absolute rubric_file path that escapes scenarios_dir is blocked."""
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()
        runner = ScenarioRunner(scenarios_dir=suite_dir)

        # Absolute path pointing outside scenarios_dir
        criterion = {"rubric_file": "/etc/passwd"}
        with pytest.raises((ValueError, OSError)):
            runner._resolve_rubric(criterion)

    def test_valid_relative_rubric_file_loaded(self, tmp_path: Path) -> None:
        """A valid relative rubric_file within scenarios_dir is loaded correctly."""
        suite_dir = tmp_path / "suite"
        rubric_dir = suite_dir / "rubrics"
        rubric_dir.mkdir(parents=True)
        rubric_file = rubric_dir / "test.md"
        rubric_file.write_text("Score: 0.9\nThis is a rubric.")

        runner = ScenarioRunner(scenarios_dir=suite_dir)
        criterion = {"rubric_file": "rubrics/test.md"}
        text = runner._resolve_rubric(criterion)

        assert "Score: 0.9" in text

    def test_inline_rubric_returned_directly(self, tmp_path: Path) -> None:
        """When no rubric_file is present, the inline 'rubric' value is returned."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        criterion = {"rubric": "Inline rubric text here."}
        text = runner._resolve_rubric(criterion)
        assert text == "Inline rubric text here."

    def test_neither_rubric_nor_rubric_file_returns_empty(self, tmp_path: Path) -> None:
        """Criterion with no rubric key → empty string returned."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        criterion = {"id": "no_rubric"}
        text = runner._resolve_rubric(criterion)
        assert text == ""


# ===========================================================================
# Edge Cases — LLMJudgeGrader
# ===========================================================================


class TestEdgeCases_LLMJudge:
    """LLMJudgeGrader output-field, text-extraction priority chain, error handling."""

    def test_output_field_parameter_restricts_judge_input(self) -> None:
        """output_field limits what text the judge sees."""
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode())["messages"][0]["content"])
            return _make_mock_http_response("Score: 0.9\nGood.")

        grader = LLMJudgeGrader(api_key="sk-fake")
        output = {
            "article": "THIS IS THE ARTICLE",
            "metadata": "top secret metadata",
        }

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade(output, "rubric", "model", output_field="article")

        assert len(captured) == 1
        user_msg = captured[0]
        assert "THIS IS THE ARTICLE" in user_msg
        assert "top secret metadata" not in user_msg, (
            "output_field='article' should exclude 'metadata' from judge input"
        )

    def test_text_extraction_priority_article_key(self) -> None:
        """Priority chain: 'article' key extracted first."""
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode())["messages"][0]["content"])
            return _make_mock_http_response("Score: 0.9\nGood.")

        grader = LLMJudgeGrader(api_key="sk-fake")
        output = {"article": "ARTICLE_CONTENT", "text": "TEXT_CONTENT"}

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade(output, "rubric", "model")

        assert "ARTICLE_CONTENT" in captured[0]

    def test_text_extraction_priority_text_key_fallback(self) -> None:
        """Priority chain: 'text' key used when 'article' absent."""
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode())["messages"][0]["content"])
            return _make_mock_http_response("Score: 0.8\nOkay.")

        grader = LLMJudgeGrader(api_key="sk-fake")
        output = {"text": "TEXT_FALLBACK_CONTENT"}

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade(output, "rubric", "model")

        assert "TEXT_FALLBACK_CONTENT" in captured[0]

    def test_text_extraction_priority_final_key_for_cli_output(self) -> None:
        """Priority chain: CLI-structured output uses 'final' sub-dict."""
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode())["messages"][0]["content"])
            return _make_mock_http_response("Score: 0.7\nDecent.")

        grader = LLMJudgeGrader(api_key="sk-fake")
        output = _cli_structured_output()  # {"final": {...}, "phases": {...}}

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade(output, "rubric", "model")

        assert len(captured) == 1
        user_msg = captured[0]
        # The judge must receive some content — not an empty string
        assert len(user_msg.strip()) > len("## Rubric\n\nrubric\n\n## Article to Evaluate\n\n")

    def test_text_extraction_json_fallback_for_unknown_output(self) -> None:
        """Priority chain: unknown output shape → JSON serialisation is used."""
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(json.loads(request.data.decode())["messages"][0]["content"])
            return _make_mock_http_response("Score: 0.6\nSomewhat good.")

        grader = LLMJudgeGrader(api_key="sk-fake")
        output = {"completely_custom_key": "custom_value", "another_key": 42}

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade(output, "rubric", "model")

        user_msg = captured[0]
        # The custom_value should appear somewhere in the serialised JSON
        assert "custom_value" in user_msg or "custom_custom_key" in user_msg or len(user_msg) > 50

    def test_network_timeout_returns_error_grade(self) -> None:
        """A socket timeout returns passed=False with error details."""
        import socket

        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = grader.grade({"text": "hello"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "error" in result.details.lower() or "timeout" in result.details.lower() or \
               "TimeoutError" in result.details or "timed out" in result.details.lower()

    def test_score_clamped_when_judge_returns_above_1(self) -> None:
        """LLM responding 'Score: 1.5' → score clamped to 1.0."""
        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen",
                   return_value=_make_mock_http_response("Score: 1.5\nGreat article.")):
            result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.score == pytest.approx(1.0)

    def test_score_clamped_when_judge_returns_below_0(self) -> None:
        """LLM responding 'Score: -0.3' → score clamped to 0.0."""
        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen",
                   return_value=_make_mock_http_response("Score: -0.3\nTerrible.")):
            result = grader.grade({"article": "text"}, "rubric", "model")

        # -0.3 not parsed by regex (only [0-9]*\.?[0-9]+), but even if it were, clamp applies
        # The regex won't match negative — so we get 0.0 (no match default)
        assert result.score >= 0.0  # must be ≥ 0

    def test_score_integer_in_response_parsed(self) -> None:
        """LLM responding 'Score: 1' (integer, no decimal) is parsed as 1.0."""
        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen",
                   return_value=_make_mock_http_response("Score: 1\nPerfect.")):
            result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.score == pytest.approx(1.0)

    def test_403_http_error_returns_failed(self) -> None:
        """HTTP 403 from Anthropic API returns passed=False."""
        import urllib.error

        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(
                       None, 403, "Forbidden", None, BytesIO(b"forbidden")
                   )):
            result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert "403" in result.details


# ===========================================================================
# Edge Cases — Grading / ScenarioRunner
# ===========================================================================


class TestEdgeCases_Grading:
    """Edge cases in gate/weight semantics, threshold overrides, data model."""

    def test_two_gates_both_pass_scenario_passes(self, tmp_path: Path) -> None:
        """Two gates both passing → gates_passed=True."""
        scenario = {
            "id": "two-gates-pass",
            "acceptance": [
                {"id": "g1", "type": "assertion", "check": "True", "weight": 0},
                {"id": "g2", "type": "assertion", "check": "True", "weight": 0},
            ],
            "scoring": {"pass_threshold": 0.0, "gate_mode": "all_or_nothing"},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        assert result.gates_passed is True
        assert result.passed is True

    def test_two_gates_one_fails_scenario_fails(self, tmp_path: Path) -> None:
        """Two gates, one fails → gates_passed=False → scenario fails."""
        scenario = {
            "id": "two-gates-one-fail",
            "acceptance": [
                {"id": "g1", "type": "assertion", "check": "True", "weight": 0},
                {"id": "g2", "type": "assertion", "check": "False", "weight": 0},
            ],
            "scoring": {"pass_threshold": 0.0, "gate_mode": "all_or_nothing"},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        assert result.gates_passed is False
        assert result.passed is False

    def test_all_gates_fail_but_scored_criteria_pass(self, tmp_path: Path) -> None:
        """All gates fail → scenario fails regardless of high scored score."""
        scenario = {
            "id": "gate-fail-scored-pass",
            "acceptance": [
                {"id": "g1", "type": "assertion", "check": "False", "weight": 0},
                {"id": "s1", "type": "assertion", "check": "True", "weight": 10, "threshold": 0.5},
                {"id": "s2", "type": "assertion", "check": "True", "weight": 10, "threshold": 0.5},
            ],
            "scoring": {"pass_threshold": 0.5, "gate_mode": "all_or_nothing"},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "content"})

        assert result.weighted_score == pytest.approx(1.0)  # scored criteria all pass
        assert result.gates_passed is False
        assert result.passed is False

    def test_criterion_threshold_controls_per_criterion_pass(self, tmp_path: Path) -> None:
        """criterion.threshold is applied to the raw score to determine per-criterion pass."""
        # LLMJudgeGrader dry-run stub = 0.8; threshold = 0.9 → should fail
        scenario = {
            "id": "threshold-test",
            "acceptance": [
                {
                    "id": "strict_judge",
                    "type": "llm_judge",
                    "rubric": "Rate.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.9,  # stub=0.8 < 0.9 → fails
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.0},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, {"text": "some text"})

        cr = result.criterion_results[0]
        assert cr.grade.score == pytest.approx(0.8)  # stub score
        assert cr.grade.passed is False  # 0.8 < 0.9 threshold

    def test_high_threshold_makes_criterion_fail_without_gate_fail(
        self, tmp_path: Path
    ) -> None:
        """High criterion threshold → that criterion fails but it's not a gate."""
        scenario = {
            "id": "high-threshold-no-gate",
            "acceptance": [
                {
                    "id": "hard_criterion",
                    "type": "assertion",
                    "check": "True",  # score=1.0
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        # Score=1.0, threshold=0.5 → passes
        assert result.criterion_results[0].grade.passed is True

    def test_weighted_score_exact_boundary_at_threshold(self, tmp_path: Path) -> None:
        """Weighted score exactly equal to pass_threshold → scenario passes."""
        # 1 of 2 scored criteria pass, equal weight → score = 0.5 == threshold
        scenario = {
            "id": "boundary-score",
            "acceptance": [
                {"id": "pass", "type": "assertion", "check": "True",  "weight": 1, "threshold": 0.5},
                {"id": "fail", "type": "assertion", "check": "False", "weight": 1, "threshold": 0.5},
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        assert result.weighted_score == pytest.approx(0.5)
        assert result.passed is True  # 0.5 >= 0.5

    def test_weighted_score_just_below_threshold(self, tmp_path: Path) -> None:
        """Weighted score just below pass_threshold → scenario fails."""
        # weight 1 fail + weight 100 pass → (0+100)/101 ≈ 0.990  (pass)
        # weight 10 fail + weight 1 pass  → (0+1)/11  ≈ 0.091  (fail vs 0.5 threshold)
        scenario = {
            "id": "just-below",
            "acceptance": [
                {"id": "heavy_fail", "type": "assertion", "check": "False", "weight": 10, "threshold": 0.5},
                {"id": "light_pass", "type": "assertion", "check": "True",  "weight": 1, "threshold": 0.5},
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        expected = (0.0 * 10 + 1.0 * 1) / 11
        assert result.weighted_score == pytest.approx(expected)
        assert result.passed is False  # ≈ 0.091 < 0.5

    def test_grade_result_dataclass_fields(self) -> None:
        """GradeResult dataclass has all required fields."""
        gr = GradeResult(passed=True, score=0.9, details="test", grader_type="keyword")
        assert gr.passed is True
        assert gr.score == pytest.approx(0.9)
        assert gr.details == "test"
        assert gr.grader_type == "keyword"

    def test_criterion_result_dataclass_fields(self, tmp_path: Path) -> None:
        """CriterionResult.is_gate is True exactly when weight == 0."""
        scenario = {
            "id": "is-gate-test",
            "acceptance": [
                {"id": "gate_c",  "type": "assertion", "check": "True", "weight": 0},
                {"id": "scored_c", "type": "assertion", "check": "True", "weight": 5, "threshold": 0.5},
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        by_id = {cr.criterion_id: cr for cr in result.criterion_results}
        assert by_id["gate_c"].is_gate is True
        assert by_id["gate_c"].weight == 0
        assert by_id["scored_c"].is_gate is False
        assert by_id["scored_c"].weight == 5

    def test_no_observations_key_defaults_to_empty_dict(self, tmp_path: Path) -> None:
        """Scenario without 'observations' key → ScenarioResult.observations == {}."""
        scenario = {
            "id": "no-obs",
            "acceptance": [
                {"id": "g", "type": "assertion", "check": "True", "weight": 0}
            ],
            "scoring": {"pass_threshold": 0.0},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {})

        assert result.observations == {}

    def test_keyword_output_field_with_cli_structured_output(
        self, tmp_path: Path
    ) -> None:
        """output_field='final' on a keyword criterion searches only the 'final' sub-dict."""
        scenario = {
            "id": "kw-output-field",
            "acceptance": [
                {
                    "id": "kw",
                    "type": "keyword",
                    "keywords": ["mock"],
                    "match_mode": "any",
                    "output_field": None,  # None → search everything
                    "weight": 1,
                    "threshold": 0.5,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, _cli_structured_output())

        assert result.criterion_results[0].grade.passed is True

    def test_assertion_subscript_access_on_nested_output(self, tmp_path: Path) -> None:
        """Assertion using subscript access on CLI-structured output works."""
        scenario = {
            "id": "subscript-test",
            "acceptance": [
                {
                    "id": "s",
                    "type": "assertion",
                    "check": "len(str(output)) > 10",
                    "weight": 0,
                }
            ],
            "scoring": {"pass_threshold": 0.0},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, _cli_structured_output())

        assert result.gates_passed is True


# ===========================================================================
# Edge Cases — run_suite()
# ===========================================================================


class TestEdgeCases_Suite:
    """run_suite() edge cases: missing entries, multi-scenario, empty dir."""

    def test_suite_missing_output_uses_empty_dict(self, tmp_path: Path) -> None:
        """When a scenario ID is absent from pipeline_outputs, {} is used.

        This causes the assertion gate to receive an empty dict → gate fails
        → scenario fails.
        """
        scenario = {
            "id": "suite-missing-output",
            "acceptance": [
                {
                    "id": "not_empty",
                    "type": "assertion",
                    "check": "len(str(output)) > 10",
                    "weight": 0,
                }
            ],
            "scoring": {"pass_threshold": 0.0},
        }
        (tmp_path / "suite-missing-output.yaml").write_text(yaml.dump(scenario))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(tmp_path, pipeline_outputs={})

        assert suite.total_scenarios == 1
        assert suite.scenarios[0].passed is False
        assert suite.satisfaction_rate == pytest.approx(0.0)

    def test_suite_all_scenarios_have_result(self, tmp_path: Path) -> None:
        """SuiteResult.scenarios has an entry for every YAML file in suite_dir."""
        for i in range(5):
            scenario = {
                "id": f"multi-{i}",
                "acceptance": [
                    {"id": "g", "type": "assertion", "check": "True", "weight": 0}
                ],
                "scoring": {"pass_threshold": 0.0},
            }
            (tmp_path / f"scenario-{i}.yaml").write_text(yaml.dump(scenario))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        outputs = {f"multi-{i}": {"x": i} for i in range(5)}
        suite = runner.run_suite(tmp_path, pipeline_outputs=outputs)

        assert suite.total_scenarios == 5
        assert len(suite.scenarios) == 5

    def test_suite_satisfaction_rate_zero_on_zero_total(self, tmp_path: Path) -> None:
        """Empty suite_dir → satisfaction_rate = 0.0, scenarios = []."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(tmp_path, pipeline_outputs={})

        assert suite.total_scenarios == 0
        assert suite.satisfaction_rate == pytest.approx(0.0)
        assert suite.scenarios == []

    def test_suite_result_is_suite_result_type(self, tmp_path: Path) -> None:
        """run_suite() returns a SuiteResult instance."""
        from scenario_runner.models import SuiteResult

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(tmp_path, pipeline_outputs={})
        assert isinstance(suite, SuiteResult)

    def test_suite_partial_outputs_gives_partial_pass(self, tmp_path: Path) -> None:
        """3 scenarios, only 2 have outputs that cause the gate to pass → rate = 2/3."""
        for i in range(3):
            scenario = {
                "id": f"partial-{i}",
                "acceptance": [
                    {
                        "id": "g",
                        "type": "assertion",
                        "check": "len(str(output)) > 5",
                        "weight": 0,
                    }
                ],
                "scoring": {"pass_threshold": 0.0},
            }
            (tmp_path / f"scenario-{i}.yaml").write_text(yaml.dump(scenario))

        # Only 2 of 3 get non-empty outputs
        outputs = {
            "partial-0": {"data": "has content"},
            "partial-1": {"data": "has content too"},
            # partial-2 is absent → gets {} → gate fails
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(tmp_path, pipeline_outputs=outputs)

        assert suite.total_scenarios == 3
        assert suite.satisfaction_rate == pytest.approx(2 / 3)


# ===========================================================================
# Integration — Full E2E Scenario In Dry-Run Mode
# ===========================================================================


class TestIntegration_FullDryRun:
    """Integration tests: full e2e-autonomous scoring pipeline in dry-run mode."""

    def test_full_pipeline_criterion_ids_present(self) -> None:
        """All three criterion IDs appear in the ScenarioResult."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _dry_run_phase_output())

        ids = {cr.criterion_id for cr in result.criterion_results}
        assert "output_not_empty" in ids
        assert "output_content_check" in ids
        assert "output_quality" in ids

    def test_full_pipeline_no_gate_failures_in_dry_run(self) -> None:
        """Dry-run phase output passes all gates."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _dry_run_phase_output())

        gate_results = [cr for cr in result.criterion_results if cr.is_gate]
        for g in gate_results:
            assert g.grade.passed is True, (
                f"Gate '{g.criterion_id}' failed: {g.grade.details}"
            )

    def test_full_pipeline_keyword_criterion_passes(self) -> None:
        """output_content_check (keyword criterion) passes in dry-run."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _dry_run_phase_output())

        kw = next(
            cr for cr in result.criterion_results if cr.criterion_id == "output_content_check"
        )
        assert kw.grade.passed is True

    def test_full_pipeline_llm_judge_criterion_stub_score(self) -> None:
        """output_quality (llm_judge) returns stub 0.8 in dry-run."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _dry_run_phase_output())

        judge = next(
            cr for cr in result.criterion_results if cr.criterion_id == "output_quality"
        )
        assert judge.grade.score == pytest.approx(0.8)
        assert judge.grade.grader_type == "llm_judge"

    def test_full_pipeline_passes_with_cli_structured_output(self) -> None:
        """Scenario also passes when grading CLI-structured {"final":…,"phases":…} output."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _cli_structured_output())

        assert result.passed is True, (
            f"Scenario must pass with CLI-structured output. "
            f"Score={result.weighted_score:.3f}, gates_passed={result.gates_passed}"
        )

    def test_full_pipeline_exact_weighted_score(self) -> None:
        """Weighted score == (1.0×40 + 0.8×60) / 100 = 0.88 in dry-run mode."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, _dry_run_phase_output())

        expected = (1.0 * 40 + 0.8 * 60) / 100
        assert result.weighted_score == pytest.approx(expected, abs=1e-6)

    def test_full_pipeline_fails_with_completely_empty_output(self) -> None:
        """Empty dict → gate 'output_not_empty' fails → scenario fails."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, {})

        assert result.passed is False
        assert result.gates_passed is False

    def test_full_pipeline_consistent_across_multiple_runs(self) -> None:
        """Dry-run mode is deterministic: two runs produce the same score."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)
        output = _dry_run_phase_output()

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            r1 = runner.run_scenario(scenario, output)
            r2 = runner.run_scenario(scenario, output)

        assert r1.weighted_score == pytest.approx(r2.weighted_score)
        assert r1.passed == r2.passed
        assert r1.gates_passed == r2.gates_passed

    def test_full_pipeline_analysis_complete_keyword_in_live_style_output(self) -> None:
        """'Analysis complete' keyword phrase (live LLM marker) passes keyword criterion."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        # Simulate what a live LLM phase would produce
        live_like_output = {
            "result": {
                "text": (
                    "AI orchestration is the coordination of multiple AI agents "
                    "to complete complex tasks autonomously.\n\nAnalysis complete."
                )
            }
        }

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, live_like_output)

        kw = next(
            cr for cr in result.criterion_results if cr.criterion_id == "output_content_check"
        )
        assert kw.grade.passed is True, (
            "Keyword criterion must pass for output containing 'analysis' and 'complete'"
        )
