"""Scenario Runner — loads YAML scenarios and grades pipeline outputs.

Usage::

    from pathlib import Path
    from scenario_runner import ScenarioRunner

    runner = ScenarioRunner(scenarios_dir=Path("scenarios/content-pipeline"))
    scenario = runner.load_scenario(Path("scenarios/content-pipeline/happy-path-001.yaml"))
    result = runner.run_scenario(scenario, pipeline_output={"article": "..."})
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .graders.assertion import AssertionGrader
from .graders.llm_judge import LLMJudgeGrader
from .graders.url_check import URLCheckGrader
from .models import (
    CriterionResult,
    GradeResult,
    ScenarioResult,
    SuiteResult,
)

# Required top-level keys for a valid scenario YAML
_REQUIRED_KEYS = ("id", "acceptance")


class ScenarioRunner:
    """Loads scenario YAML files and grades pipeline outputs against them.

    Parameters
    ----------
    scenarios_dir:
        Directory that contains the scenario YAML files (used as the base
        for resolving ``rubric_file`` paths in each scenario).
    engine_db:
        Optional reference to the orchestration engine Database (reserved
        for future integration; not used in the MVP).
    """

    def __init__(
        self,
        scenarios_dir: Path,
        engine_db=None,
    ) -> None:
        self.scenarios_dir = Path(scenarios_dir)
        self.engine_db = engine_db

        self._assertion_grader = AssertionGrader()
        self._llm_grader = LLMJudgeGrader()
        self._url_grader = URLCheckGrader()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_scenario(self, scenario_path: Path) -> dict:
        """Load and validate a scenario YAML file.

        Raises ValueError if required keys are missing.
        Raises FileNotFoundError / yaml.YAMLError for IO / parse problems.
        """
        scenario_path = Path(scenario_path)
        with open(scenario_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict):
            raise ValueError(
                f"Scenario file must be a YAML mapping, got {type(data).__name__}: "
                f"{scenario_path}"
            )

        for key in _REQUIRED_KEYS:
            if key not in data:
                raise ValueError(
                    f"Scenario missing required key '{key}': {scenario_path}"
                )

        if not isinstance(data.get("acceptance"), list):
            raise ValueError(
                f"Scenario 'acceptance' must be a list: {scenario_path}"
            )

        return data

    def run_scenario(
        self,
        scenario: dict,
        pipeline_output: dict,
    ) -> ScenarioResult:
        """Grade *pipeline_output* against all acceptance criteria in *scenario*.

        Steps:
        1. Run all assertion checks (gates and scored).
        2. Run all LLM judge checks.
        3. Run all URL checks.
        4. Compute weighted average over non-gate criteria.
        5. Apply gate_mode: if any gate fails → scenario fails (all_or_nothing).
        6. Compare weighted score against pass_threshold.

        Returns a ScenarioResult.
        """
        scenario_id: str = scenario["id"]
        criteria: list = scenario.get("acceptance", [])
        scoring: dict = scenario.get("scoring", {})
        pass_threshold: float = float(scoring.get("pass_threshold", 0.75))
        gate_mode: str = scoring.get("gate_mode", "all_or_nothing")

        criterion_results: list[CriterionResult] = []

        for criterion in criteria:
            crit_id: str = criterion["id"]
            crit_type: str = criterion["type"]
            weight: int = int(criterion.get("weight", 1))
            is_gate: bool = weight == 0
            threshold: float = float(criterion.get("threshold", 0.5))

            raw_grade = self._grade_criterion(
                criterion_type=crit_type,
                criterion=criterion,
                pipeline_output=pipeline_output,
            )

            # For assertion graders the score is already binary (0 or 1),
            # so the threshold is effectively irrelevant — but we still apply
            # it consistently so the model is uniform.
            final_passed = raw_grade.score >= threshold
            final_grade = GradeResult(
                passed=final_passed,
                score=raw_grade.score,
                details=raw_grade.details,
                grader_type=raw_grade.grader_type,
            )

            criterion_results.append(
                CriterionResult(
                    criterion_id=crit_id,
                    grade=final_grade,
                    weight=weight,
                    is_gate=is_gate,
                )
            )

        # --- Gate check ---
        gates = [cr for cr in criterion_results if cr.is_gate]
        gates_passed: bool = all(cr.grade.passed for cr in gates)

        # --- Weighted score (non-gate criteria only) ---
        scored = [cr for cr in criterion_results if not cr.is_gate]
        if scored:
            total_weight = sum(cr.weight for cr in scored)
            if total_weight > 0:
                weighted_score = (
                    sum(cr.grade.score * cr.weight for cr in scored) / total_weight
                )
            else:
                weighted_score = 1.0  # All scored criteria have weight 0 — treat as pass
        else:
            # Gates-only scenario: no scored criteria, so weighted score is N/A
            # Pass if all gates passed
            weighted_score = 1.0 if gates_passed else 0.0

        # --- Overall pass decision ---
        if gate_mode == "all_or_nothing" and not gates_passed:
            scenario_passed = False
        else:
            scenario_passed = weighted_score >= pass_threshold

        # --- Collect declared observations (values not yet populated by runner) ---
        observations: dict = {
            obs["id"]: obs.get("measure", "")
            for obs in scenario.get("observations", [])
        }

        return ScenarioResult(
            scenario_id=scenario_id,
            passed=scenario_passed,
            weighted_score=weighted_score,
            gates_passed=gates_passed,
            criterion_results=criterion_results,
            observations=observations,
        )

    def run_suite(
        self,
        suite_dir: Path,
        pipeline_outputs: dict,
    ) -> SuiteResult:
        """Run all scenarios in *suite_dir* against *pipeline_outputs*.

        Parameters
        ----------
        suite_dir:
            Directory containing ``*.yaml`` scenario files.
        pipeline_outputs:
            Mapping of scenario_id → pipeline output dict.  If a scenario ID
            is not present, an empty dict is used (all assertions will fail).

        Returns a SuiteResult.
        """
        suite_dir = Path(suite_dir)
        yaml_files = sorted(suite_dir.glob("*.yaml"))

        results: list[ScenarioResult] = []
        for yaml_file in yaml_files:
            scenario = self.load_scenario(yaml_file)
            output = pipeline_outputs.get(scenario["id"], {})
            result = self.run_scenario(scenario, output)
            results.append(result)

        total = len(results)
        passed_count = sum(1 for r in results if r.passed)
        satisfaction_rate = passed_count / total if total > 0 else 0.0

        return SuiteResult(
            scenarios=results,
            satisfaction_rate=satisfaction_rate,
            total_scenarios=total,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _grade_criterion(
        self,
        criterion_type: str,
        criterion: dict,
        pipeline_output: dict,
    ) -> GradeResult:
        """Dispatch to the appropriate grader and return a raw GradeResult."""

        if criterion_type == "assertion":
            check_expr: str = criterion.get("check", "False")
            return self._assertion_grader.grade(check_expr, pipeline_output)

        elif criterion_type == "llm_judge":
            rubric_text = self._resolve_rubric(criterion)
            judge_model: str = criterion.get(
                "judge_model", "claude-haiku-4-5-20241022"
            )
            return self._llm_grader.grade(pipeline_output, rubric_text, judge_model)

        elif criterion_type == "url_check":
            article_text: str = pipeline_output.get("article", "")
            return self._url_grader.grade(article_text)

        else:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Unknown criterion type: '{criterion_type}'",
                grader_type="unknown",
            )

    def _resolve_rubric(self, criterion: dict) -> str:
        """Return rubric text, loading from file if *rubric_file* is specified."""
        if "rubric_file" in criterion:
            # rubric_file is relative to the scenarios root, e.g.
            # "shared/rubrics/factual-accuracy.md" → scenarios/shared/rubrics/...
            scenarios_root = self.scenarios_dir.parent
            rubric_path = (scenarios_root / criterion["rubric_file"]).resolve()
            
            # Prevent path traversal — rubric must stay within scenarios directory
            if not rubric_path.is_relative_to(scenarios_root.resolve()):
                raise ValueError(
                    f"Rubric path escapes scenarios directory: {criterion['rubric_file']}"
                )
            
            with open(rubric_path, "r", encoding="utf-8") as fh:
                return fh.read()
        return criterion.get("rubric", "")
