"""CI-safe pytest integration tests for dry-run scenario execution.

GitHub issue #173 — Dry-run scenario testing in CI.

Design
------
* **Auto-discovers** every ``scenarios/*.yaml`` at the repository root,
  excluding ``e2e-test-template.yaml`` (which is a skeleton, not a runnable
  scenario).
* **Parametrizes** so each scenario becomes its own pytest test case, named
  by the scenario ``id`` field for easy identification in CI output.
* **Dry-run mode only** — sets ``ORCH_DRY_RUN=1`` via ``monkeypatch`` so:
    - :class:`~scenario_runner.graders.llm_judge.LLMJudgeGrader` returns the
      built-in stub score (0.8) without making any API call.
    - No ``ANTHROPIC_API_KEY`` is needed.
* **Mock pipeline output** mirrors the structure produced by
  ``PhaseSequencer + DryRunExecutor`` as consumed by graders (see
  ``cli.py::scenario_run``):

      pipeline_output = {"final": final_output, "phases": phase_outputs}

  The mock text contains "mock", "execution", and "content" — the three
  keywords that every dry-run keyword criterion checks for (``match_mode:
  any``).  This lets all keyword gates pass without modifying the scenario
  YAML files.
* **Asserts** ``result.passed is True`` and
  ``result.weighted_score >= pass_threshold`` extracted from the scenario.
* **Failure reporting** prints a per-criterion breakdown table so failures
  are self-diagnosable in GitHub Actions logs without re-running locally.

Expected dry-run score for all bundled scenarios:

    keyword (weight 40) × 1.0  +  llm_judge (weight 60) × 0.8
    ──────────────────────────────────────────────────────────
                      (40 + 60)
    = (40 + 48) / 100 = 0.88  →  PASS (threshold 0.70)
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path bootstrap — make the project importable when pytest rootdir handling
# does not add the project root automatically (e.g. bare ``python -m pytest``
# from outside the repo).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from scenario_runner.runner import ScenarioRunner  # noqa: E402 — after path fix


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCENARIOS_DIR: Path = _REPO_ROOT / "scenarios"

#: Template file that is intentionally NOT a runnable scenario.
_EXCLUDED_FILES: frozenset[str] = frozenset({"e2e-test-template.yaml"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discover_scenarios() -> list[Path]:
    """Return all runnable scenario YAML files under ``scenarios/``.

    Only top-level ``scenarios/*.yaml`` files are collected (subdirectories
    such as ``scenarios/content-pipeline/`` contain scenario suites that are
    covered by separate test modules).  Template files listed in
    ``_EXCLUDED_FILES`` are skipped.
    """
    if not _SCENARIOS_DIR.exists():
        return []
    return sorted(
        p
        for p in _SCENARIOS_DIR.glob("*.yaml")
        if p.name not in _EXCLUDED_FILES
    )


def _scenario_id_from_path(path: Path) -> str:
    """Read the ``id`` field from a scenario YAML for use as the pytest ID.

    Falls back to the filename stem if the file cannot be parsed — this keeps
    parametrize working even for partially-broken scenario files.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
    except Exception:  # noqa: BLE001
        pass
    return path.stem


def _make_dry_run_mock_output() -> dict[str, Any]:
    """Build a mock pipeline output that passes all dry-run acceptance criteria.

    Structure mirrors what ``cli.py::scenario_run`` exposes to graders after a
    real dry-run pipeline execution::

        pipeline_output = {"final": final_output, "phases": phase_outputs}

    The ``DryRunExecutor`` always produces messages of the form::

        "Mock execution of {task_type} task"

    which guarantees the keywords **mock**, **execution**, and **content** are
    present in every smoke-test scenario's keyword criterion (``match_mode:
    any``).  The gate assertion ``len(str(output)) > 10`` is trivially
    satisfied by this structure (several hundred characters).
    """
    return {
        "final": {
            "message": "Mock execution of content task completed successfully.",
            "model_used": "dry-run",
            "worker_id": "dry-run-worker-0",
            "status": "success",
        },
        "phases": {
            "phase_1": {
                "message": "Mock execution of content task",
                "model_used": "dry-run",
                "worker_id": "dry-run-worker-0",
                "status": "success",
            },
        },
    }


def _format_criterion_breakdown(result) -> str:  # noqa: ANN001
    """Return a human-readable per-criterion table for failure messages."""
    lines = ["", "  Per-criterion breakdown:", "  " + "-" * 60]
    for cr in result.criterion_results:
        gate_tag = " [GATE]" if cr.is_gate else f" [w={cr.weight}]"
        status = "PASS" if cr.grade.passed else "FAIL"
        lines.append(
            f"  {status:4s}  {cr.criterion_id:<28s}{gate_tag:<10s}"
            f"  score={cr.grade.score:.3f}  {cr.grade.details[:80]}"
        )
    lines.append("  " + "-" * 60)
    lines.append(f"  Weighted score : {result.weighted_score:.4f}")
    lines.append(f"  Gates passed   : {result.gates_passed}")
    lines.append(f"  Overall passed : {result.passed}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parametrize — one test per discovered scenario
# ---------------------------------------------------------------------------

_SCENARIO_PATHS: list[Path] = _discover_scenarios()

# Build pytest.param list so each test has a meaningful ID (scenario id, not
# just the file path index).  If no scenarios are found the test collection
# step itself shows a clear warning rather than silently producing zero tests.
_PARAMS = [
    pytest.param(path, id=_scenario_id_from_path(path))
    for path in _SCENARIO_PATHS
]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SCENARIO_PATHS,
    reason=(
        f"No runnable scenario YAML files found under {_SCENARIOS_DIR}. "
        "Check that the scenarios/ directory exists and contains *.yaml files."
    ),
)
@pytest.mark.parametrize("scenario_path", _PARAMS)
def test_scenario_dry_run(scenario_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each scenario must pass when executed in dry-run mode.

    This test is CI-safe: it requires no API keys, no external services, and
    no network access.  ``ORCH_DRY_RUN=1`` triggers the built-in stub scoring
    path inside :class:`~scenario_runner.graders.llm_judge.LLMJudgeGrader`,
    and the mock pipeline output satisfies all keyword / assertion criteria.

    Failure output includes a full per-criterion breakdown table to aid
    debugging in GitHub Actions logs.
    """
    # ------------------------------------------------------------------
    # 1. Activate dry-run mode for graders (LLMJudgeGrader checks this).
    #    Use monkeypatch so the env var is always restored after the test —
    #    even on failure — preventing leakage to other test cases.
    # ------------------------------------------------------------------
    monkeypatch.setenv("ORCH_DRY_RUN", "1")

    # ------------------------------------------------------------------
    # 2. Load the scenario YAML.
    # ------------------------------------------------------------------
    runner = ScenarioRunner(scenarios_dir=_SCENARIOS_DIR)
    scenario = runner.load_scenario(scenario_path)

    scenario_id: str = scenario["id"]
    pass_threshold: float = float(
        scenario.get("scoring", {}).get("pass_threshold", 0.75)
    )

    # ------------------------------------------------------------------
    # 3. Build mock pipeline output.
    # ------------------------------------------------------------------
    pipeline_output = _make_dry_run_mock_output()

    # ------------------------------------------------------------------
    # 4. Run the scenario grader.
    # ------------------------------------------------------------------
    result = runner.run_scenario(scenario, pipeline_output)

    # ------------------------------------------------------------------
    # 5. Assertions with detailed failure messages.
    # ------------------------------------------------------------------
    breakdown = _format_criterion_breakdown(result)

    assert result.passed is True, (
        f"\n\nScenario '{scenario_id}' FAILED in dry-run mode.\n"
        f"  Expected: passed=True, weighted_score >= {pass_threshold}\n"
        f"  Got:      passed={result.passed}, weighted_score={result.weighted_score:.4f}"
        f"{breakdown}"
    )

    assert result.weighted_score >= pass_threshold, (
        f"\n\nScenario '{scenario_id}': weighted score {result.weighted_score:.4f} "
        f"is below pass_threshold {pass_threshold}."
        f"{breakdown}"
    )
