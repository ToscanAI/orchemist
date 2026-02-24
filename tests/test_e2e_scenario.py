"""Integration tests: E2E autonomous scenario test pipeline.

Validates:
1. Scenario YAML loading for e2e-autonomous.yaml
2. Template YAML loading + validation for e2e-test-template.yaml
3. KeywordGrader — all match modes and edge cases
4. LLMJudgeGrader dry-run stub (ORCH_DRY_RUN=1)
5. Full scoring pipeline in dry-run mode (no API key required)
6. Score report calculation matches expected weighted average
7. CLI ``orch scenario run e2e-autonomous --dry-run`` via Click test runner

All tests are completely offline — no API calls are made.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

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

from scenario_runner.graders.keyword_grader import KeywordGrader
from scenario_runner.graders.llm_judge import LLMJudgeGrader
from scenario_runner.models import GradeResult
from scenario_runner.runner import ScenarioRunner


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

SCENARIOS_DIR = _REPO_ROOT / "scenarios"
E2E_SCENARIO_FILE = SCENARIOS_DIR / "e2e-autonomous.yaml"
E2E_TEMPLATE_FILE = SCENARIOS_DIR / "e2e-test-template.yaml"


def _dry_run_phase_output() -> dict:
    """Return a synthetic phase output that mimics DryRunExecutor output.

    This is what ``PhaseSequencer._execute_and_wait()`` returns after
    calling ``result.model_dump()`` on a successful DryRunExecutor result.
    """
    return {
        "task_id": "test-task-001",
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


# ===========================================================================
# 1 – Scenario YAML loading
# ===========================================================================


class TestScenarioLoading:
    """Validate that the e2e-autonomous.yaml scenario loads and parses correctly."""

    def test_scenario_file_exists(self) -> None:
        """e2e-autonomous.yaml must exist in scenarios/."""
        assert E2E_SCENARIO_FILE.exists(), (
            f"Expected scenario file at: {E2E_SCENARIO_FILE}"
        )

    def test_scenario_loads_without_error(self) -> None:
        """ScenarioRunner.load_scenario() must not raise for e2e-autonomous.yaml."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        assert scenario["id"] == "e2e-autonomous"
        assert isinstance(scenario["acceptance"], list)

    def test_scenario_has_required_keys(self) -> None:
        """Scenario must have id, pipeline, input, acceptance, scoring keys."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        for key in ("id", "pipeline", "input", "acceptance", "scoring"):
            assert key in data, f"Missing required key: '{key}'"

    def test_acceptance_has_keyword_criterion(self) -> None:
        """At least one acceptance criterion must use type: keyword."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        types = [c["type"] for c in data["acceptance"]]
        assert "keyword" in types, (
            f"No 'keyword' criterion found in acceptance. Types present: {types}"
        )

    def test_acceptance_has_llm_judge_criterion(self) -> None:
        """At least one acceptance criterion must use type: llm_judge."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        types = [c["type"] for c in data["acceptance"]]
        assert "llm_judge" in types, (
            f"No 'llm_judge' criterion found in acceptance. Types present: {types}"
        )

    def test_non_gate_weights_sum_to_100(self) -> None:
        """Non-gate criteria weights must sum to exactly 100."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        non_gate = [c for c in data["acceptance"] if int(c.get("weight", 0)) != 0]
        total_weight = sum(int(c["weight"]) for c in non_gate)
        assert total_weight == 100, (
            f"Non-gate weights sum to {total_weight}, expected 100"
        )

    def test_scoring_pass_threshold_is_0_70(self) -> None:
        """scoring.pass_threshold must be 0.70."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        threshold = data.get("scoring", {}).get("pass_threshold")
        assert float(threshold) == pytest.approx(0.70), (
            f"pass_threshold is {threshold}, expected 0.70"
        )

    def test_scoring_gate_mode_is_all_or_nothing(self) -> None:
        """scoring.gate_mode must be 'all_or_nothing'."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        gate_mode = data.get("scoring", {}).get("gate_mode")
        assert gate_mode == "all_or_nothing", (
            f"gate_mode is '{gate_mode}', expected 'all_or_nothing'"
        )


# ===========================================================================
# 2 – Template YAML loading + validation
# ===========================================================================


class TestTemplateLoading:
    """Validate e2e-test-template.yaml loads and passes TemplateEngine validation."""

    def test_template_file_exists(self) -> None:
        """e2e-test-template.yaml must exist in scenarios/."""
        assert E2E_TEMPLATE_FILE.exists(), (
            f"Expected template file at: {E2E_TEMPLATE_FILE}"
        )

    def test_template_loads_and_validates(self) -> None:
        """TemplateEngine.validate_template() must return zero errors."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        from orchestration_engine.templates import TemplateEngine

        engine = TemplateEngine()
        template = engine.load_template(E2E_TEMPLATE_FILE)
        errors = engine.validate_template(template)

        assert not errors, (
            f"Template validation failed with {len(errors)} error(s): {errors}"
        )

    def test_template_has_2_or_3_phases(self) -> None:
        """Template must have exactly 2 or 3 phases."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        n_phases = len(data.get("phases", []))
        assert 2 <= n_phases <= 3, (
            f"Expected 2–3 phases, got {n_phases}"
        )

    def test_all_phases_use_haiku_model(self) -> None:
        """All phases must use model_tier: haiku."""
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        with open(E2E_TEMPLATE_FILE) as fh:
            data = yaml.safe_load(fh)

        for phase in data.get("phases", []):
            assert phase.get("model_tier") == "haiku", (
                f"Phase '{phase.get('id')}' uses model_tier "
                f"'{phase.get('model_tier')}', expected 'haiku'"
            )


# ===========================================================================
# 3 – KeywordGrader
# ===========================================================================


class TestKeywordGrader:
    """Full coverage of KeywordGrader match modes and edge cases."""

    @pytest.fixture()
    def grader(self) -> KeywordGrader:
        return KeywordGrader()

    # --- match_mode: "all" ---

    def test_all_mode_all_keywords_found(self, grader: KeywordGrader) -> None:
        """match_mode='all' with all keywords present → score 1.0, passed=True."""
        output = {"article": "Mock execution of content task"}
        result = grader.grade(output, ["mock", "execution", "content"], match_mode="all")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)
        assert result.grader_type == "keyword"

    def test_all_mode_one_keyword_missing(self, grader: KeywordGrader) -> None:
        """match_mode='all' with one keyword missing → score 0.0, passed=False."""
        output = {"article": "Mock execution"}
        result = grader.grade(output, ["mock", "execution", "content"], match_mode="all")
        assert result.passed is False
        assert result.score == pytest.approx(0.0)

    def test_all_mode_no_keywords_found(self, grader: KeywordGrader) -> None:
        """match_mode='all' with no keywords present → score 0.0."""
        output = {"article": "Hello world"}
        result = grader.grade(output, ["mock", "execution"], match_mode="all")
        assert result.passed is False
        assert result.score == pytest.approx(0.0)

    # --- match_mode: "any" ---

    def test_any_mode_at_least_one_found(self, grader: KeywordGrader) -> None:
        """match_mode='any' with at least one keyword → score 1.0."""
        output = {"result": {"message": "Mock execution of content task"}}
        result = grader.grade(output, ["mock", "unicorn"], match_mode="any")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)

    def test_any_mode_none_found(self, grader: KeywordGrader) -> None:
        """match_mode='any' with no keywords matched → score 0.0."""
        output = {"article": "Hello world"}
        result = grader.grade(output, ["unicorn", "dragon"], match_mode="any")
        assert result.passed is False
        assert result.score == pytest.approx(0.0)

    # --- match_mode: "ratio" ---

    def test_ratio_mode_partial_match(self, grader: KeywordGrader) -> None:
        """match_mode='ratio' with 2 of 4 keywords → score 0.5."""
        output = {"text": "alpha beta"}
        result = grader.grade(
            output, ["alpha", "beta", "gamma", "delta"], match_mode="ratio"
        )
        assert result.score == pytest.approx(0.5)
        assert result.grader_type == "keyword"

    def test_ratio_mode_all_match(self, grader: KeywordGrader) -> None:
        """match_mode='ratio' with all keywords matched → score 1.0."""
        output = {"text": "alpha beta gamma"}
        result = grader.grade(output, ["alpha", "beta", "gamma"], match_mode="ratio")
        assert result.score == pytest.approx(1.0)

    def test_ratio_mode_no_match(self, grader: KeywordGrader) -> None:
        """match_mode='ratio' with nothing matched → score 0.0."""
        output = {"text": "hello world"}
        result = grader.grade(output, ["alpha", "beta"], match_mode="ratio")
        assert result.score == pytest.approx(0.0)

    # --- Case insensitivity ---

    def test_case_insensitive_matching(self, grader: KeywordGrader) -> None:
        """Keywords must match regardless of case."""
        output = {"article": "MOCK EXECUTION Complete"}
        result = grader.grade(output, ["mock", "EXECUTION", "complete"], match_mode="all")
        assert result.passed is True
        assert result.score == pytest.approx(1.0)

    # --- Nested output ---

    def test_nested_dict_text_extraction(self, grader: KeywordGrader) -> None:
        """KeywordGrader must recurse into nested dicts to find keywords."""
        output = _dry_run_phase_output()  # deeply nested
        result = grader.grade(output, ["mock"], match_mode="any")
        assert result.passed is True, "Expected 'mock' to be found in nested dict"

    def test_dry_run_output_contains_expected_keywords(self, grader: KeywordGrader) -> None:
        """dry-run phase output must match 'mock', 'execution', and 'content'."""
        output = _dry_run_phase_output()
        for kw in ["mock", "execution", "content"]:
            result = grader.grade(output, [kw], match_mode="any")
            assert result.passed is True, (
                f"Expected keyword '{kw}' to be found in dry-run output"
            )

    # --- output_field ---

    def test_output_field_restricts_search(self, grader: KeywordGrader) -> None:
        """When output_field is set, only that field is searched."""
        # "unicorn" only appears in the article field, not in the metadata field
        output = {"article": "unicorn grazing", "metadata": "irrelevant text here"}
        result_article = grader.grade(output, ["unicorn"], match_mode="any",
                                      output_field="article")
        assert result_article.passed is True

        result_meta = grader.grade(output, ["unicorn"], match_mode="any",
                                   output_field="metadata")
        assert result_meta.passed is False

    # --- Empty keywords ---

    def test_empty_keyword_list_is_vacuous_pass(self, grader: KeywordGrader) -> None:
        """Empty keyword list → vacuous pass (score 1.0) in all modes."""
        output = {"text": "hello"}
        for mode in ("all", "any", "ratio"):
            result = grader.grade(output, [], match_mode=mode)
            assert result.passed is True, f"Empty list should be vacuous pass (mode={mode})"
            assert result.score == pytest.approx(1.0)

    # --- Invalid match_mode ---

    def test_invalid_match_mode_returns_failed(self, grader: KeywordGrader) -> None:
        """Unknown match_mode returns a failed GradeResult, not an exception."""
        output = {"text": "hello"}
        result = grader.grade(output, ["hello"], match_mode="cosmic")
        assert result.passed is False
        assert "cosmic" in result.details.lower() or "match_mode" in result.details.lower()


# ===========================================================================
# 4 – LLMJudgeGrader dry-run stub
# ===========================================================================


class TestLLMJudgeGraderDryRun:
    """Verify ORCH_DRY_RUN=1 behaviour of LLMJudgeGrader."""

    def test_dry_run_env_returns_stub_score(self) -> None:
        """With ORCH_DRY_RUN=1, grade() returns stub score without API call."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=0.8)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"article": "some text"},
                rubric="Score this text 0–1.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.8)
        assert result.passed is True
        assert result.grader_type == "llm_judge"
        assert "dry-run" in result.details.lower()

    def test_dry_run_custom_stub_score(self) -> None:
        """dry_run_stub_score is honoured (e.g. 0.5)."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=0.5)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"article": "text"},
                rubric="Rate this.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.5)
        assert result.passed is True  # 0.5 >= 0.5 threshold in GradeResult

    def test_without_dry_run_env_no_api_key_returns_zero(self) -> None:
        """Without ORCH_DRY_RUN=1, missing API key still returns score 0.0."""
        grader = LLMJudgeGrader(api_key=None)

        with patch.dict(os.environ, {}, clear=True):
            # clear=True removes all env vars including ANTHROPIC_API_KEY
            grader2 = LLMJudgeGrader(api_key=None)

        # Grade outside the clear context — ORCH_DRY_RUN should not be set
        # in a normal test run
        env_without_dry_run = {k: v for k, v in os.environ.items()
                               if k != "ORCH_DRY_RUN"}
        with patch.dict(os.environ, env_without_dry_run, clear=True):
            result = grader2.grade(
                output={"article": "text"},
                rubric="Rate this.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_dry_run_stub_score_clamped_to_0_1(self) -> None:
        """stub_score values outside [0.0, 1.0] are clamped at construction."""
        grader_high = LLMJudgeGrader(api_key=None, dry_run_stub_score=2.5)
        grader_low = LLMJudgeGrader(api_key=None, dry_run_stub_score=-1.0)

        assert grader_high.dry_run_stub_score == pytest.approx(1.0)
        assert grader_low.dry_run_stub_score == pytest.approx(0.0)


# ===========================================================================
# 5 – Rubric parsing
# ===========================================================================


class TestRubricParsing:
    """Verify ScenarioRunner correctly parses the rubric in e2e-autonomous.yaml."""

    def test_inline_rubric_loaded_correctly(self) -> None:
        """llm_judge criterion with inline 'rubric' key loads and is non-empty."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        with open(E2E_SCENARIO_FILE) as fh:
            data = yaml.safe_load(fh)

        llm_criteria = [c for c in data["acceptance"] if c["type"] == "llm_judge"]
        assert llm_criteria, "Expected at least one llm_judge criterion"

        for c in llm_criteria:
            has_rubric = "rubric" in c or "rubric_file" in c
            assert has_rubric, (
                f"llm_judge criterion '{c['id']}' has no 'rubric' or 'rubric_file'"
            )
            rubric_text = c.get("rubric", "")
            if rubric_text:
                assert len(rubric_text.strip()) > 10, (
                    f"Rubric text for '{c['id']}' is too short: {rubric_text!r}"
                )


# ===========================================================================
# 6 – Full scoring pipeline (dry-run, no API)
# ===========================================================================


class TestScoringPipeline:
    """Integration test: full scenario grading in dry-run mode."""

    def test_scenario_passes_in_dry_run_mode(self) -> None:
        """e2e-autonomous scenario must pass when graded against dry-run output.

        Uses:
        - ORCH_DRY_RUN=1 → LLMJudgeGrader returns stub score 0.8
        - synthetic dry-run phase output containing 'mock'/'execution'/'content'
          → KeywordGrader criterion passes
        - assertion gate passes (output is non-empty)

        Expected weighted score: (1.0×40 + 0.8×60) / 100 = 0.88 ≥ 0.70 → PASS
        """
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)
        pipeline_output = _dry_run_phase_output()

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, pipeline_output)

        assert result.passed is True, (
            f"Scenario should pass in dry-run mode. "
            f"Score={result.weighted_score:.3f}, passed={result.passed}, "
            f"gates_passed={result.gates_passed}"
        )

    def test_weighted_score_matches_expected(self) -> None:
        """Weighted score must equal (1.0×40 + 0.8×60) / 100 = 0.88 in dry-run."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)
        pipeline_output = _dry_run_phase_output()

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, pipeline_output)

        # Gate passes but doesn't contribute to weighted score
        assert result.gates_passed is True

        # keyword (weight 40): score 1.0 (any of mock/execution/content found)
        # llm_judge (weight 60): score 0.8 (dry-run stub)
        expected = (1.0 * 40 + 0.8 * 60) / 100
        assert result.weighted_score == pytest.approx(expected, abs=1e-3), (
            f"Expected weighted_score ≈ {expected:.3f}, got {result.weighted_score:.3f}"
        )

    def test_gates_passed_in_dry_run(self) -> None:
        """Gate criterion (output_not_empty) must pass for non-empty dry-run output."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)
        pipeline_output = _dry_run_phase_output()

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, pipeline_output)

        assert result.gates_passed is True

    def test_scenario_fails_with_empty_output(self) -> None:
        """Empty pipeline output → assertion gate fails → scenario fails."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, {})

        # Gate 'output_not_empty' checks: len(str(output)) > 10
        # str({}) = '{}' which has length 2, so gate FAILS
        assert result.passed is False
        assert result.gates_passed is False

    def test_criterion_results_have_correct_types(self) -> None:
        """All CriterionResult objects must have the expected grader_type values."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")

        runner = ScenarioRunner(scenarios_dir=SCENARIOS_DIR)
        scenario = runner.load_scenario(E2E_SCENARIO_FILE)
        pipeline_output = _dry_run_phase_output()

        with patch.dict(os.environ, {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, pipeline_output)

        grader_types = {cr.criterion_id: cr.grade.grader_type
                        for cr in result.criterion_results}

        # Gate criterion must use assertion grader
        assert grader_types.get("output_not_empty") == "assertion"
        # Keyword criterion
        assert grader_types.get("output_content_check") == "keyword"
        # LLM judge criterion
        assert grader_types.get("output_quality") == "llm_judge"


# ===========================================================================
# 7 – CLI integration via Click test runner
# ===========================================================================


class TestCLIScenarioRun:
    """Verify ``orch scenario run`` command via Click's CliRunner."""

    def test_scenario_run_help(self) -> None:
        """``orch scenario run --help`` must exit 0 and show expected options."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scenario", "run", "--help"])

        assert result.exit_code == 0, (
            f"Expected exit code 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        assert "--dry-run" in result.output
        assert "--scenario-dir" in result.output

    def test_scenario_run_dry_run_passes(self) -> None:
        """``orch scenario run e2e-autonomous --dry-run`` must exit 0."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        result = runner.invoke(
            main,
            [
                "scenario", "run",
                "e2e-autonomous",
                "--dry-run",
                "--scenario-dir", str(SCENARIOS_DIR),
            ],
            env=env,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit code 0 (PASS), got {result.exit_code}.\n"
            f"--- Output ---\n{result.output}\n"
            f"--- Exception ---\n{result.exception}"
        )

    def test_scenario_run_nonexistent_id_exits_1(self) -> None:
        """``orch scenario run nonexistent`` must exit 1 (not found)."""
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scenario", "run", "nonexistent-scenario-xyz"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1, (
            f"Expected exit code 1 (not found), got {result.exit_code}"
        )

    def test_score_report_contains_expected_fields(self) -> None:
        """Score report output must include criterion IDs and PASS/FAIL labels."""
        if not E2E_SCENARIO_FILE.exists():
            pytest.skip("e2e-autonomous.yaml not found")
        if not E2E_TEMPLATE_FILE.exists():
            pytest.skip("e2e-test-template.yaml not found")

        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        env = {**os.environ, "ORCH_DRY_RUN": "1"}

        result = runner.invoke(
            main,
            [
                "scenario", "run",
                "e2e-autonomous",
                "--dry-run",
                "--scenario-dir", str(SCENARIOS_DIR),
            ],
            env=env,
            catch_exceptions=False,
        )

        output = result.output
        # Score report must mention criterion IDs
        assert "output_not_empty" in output, "Gate criterion ID missing from report"
        assert "output_content_check" in output, "Keyword criterion ID missing from report"
        assert "output_quality" in output, "LLM judge criterion ID missing from report"
        # Must show GATE label for the gate criterion
        assert "[GATE]" in output, "GATE label missing from report"
        # Must show PASS or FAIL verdict
        assert "PASS" in output or "FAIL" in output, "No verdict in report"
