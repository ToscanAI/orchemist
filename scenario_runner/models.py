"""Data models for scenario runner results."""

from dataclasses import dataclass, field
from typing import List


@dataclass
class GradeResult:
    """Result from a single grader evaluation."""
    passed: bool
    score: float        # 0.0 to 1.0
    details: str
    grader_type: str    # "assertion", "llm_judge", "url_check"


@dataclass
class CriterionResult:
    """Result for one acceptance criterion in a scenario."""
    criterion_id: str
    grade: GradeResult
    weight: int
    is_gate: bool       # weight == 0 means this is a hard gate


@dataclass
class ScenarioResult:
    """Overall result for a complete scenario run."""
    scenario_id: str
    passed: bool
    weighted_score: float
    gates_passed: bool
    criterion_results: List[CriterionResult]
    observations: dict


@dataclass
class SuiteResult:
    """Result for a suite of scenarios run together."""
    scenarios: List[ScenarioResult]
    satisfaction_rate: float    # passed / total
    total_scenarios: int
