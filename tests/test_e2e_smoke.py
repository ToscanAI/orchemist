"""E2E Smoke Tests — Issue #501.

Exercises the full daemon execution path in dry-run mode without any live LLM
calls or network I/O.  These tests are the CI gate for the orchestration
engine's core integration: template loading → daemon execution → DB state.

Test organisation:
  - TestSmokeTemplateParsing   — YAML fixture files load and validate correctly
  - TestSmokeDaemonExecution   — run_daemon() succeeds with a minimal template
  - TestSmokeDaemonDBState     — DB records are written correctly after a run
  - TestSmokeDaemonScenario    — scoring_status is set when a scenario is present
  - TestSmokeScenarioRunner    — ScenarioRunner evaluates the e2e-autonomous scenario

All tests are completely offline — no API calls, no network, no real LLM.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure src/ is on sys.path for direct imports
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _minimal_run_input() -> Dict[str, Any]:
    """Return a minimal but valid pipeline input dict."""
    return {"topic": "AI orchestration testing"}


def _insert_and_get_run(db, run_id: str, template_path: Path, output_dir: Path,
                         input_dict: Dict[str, Any] = None, **kwargs) -> str:
    """Helper: insert a pipeline run record and return run_id."""
    record = {
        "run_id": run_id,
        "template_path": str(template_path),
        "template_id": "smoke-test",
        "input_json": json.dumps(input_dict or _minimal_run_input()),
        "mode": "dry-run",
        "output_dir": str(output_dir),
    }
    record.update(kwargs)
    db.insert_pipeline_run(record)
    return run_id


# ===========================================================================
# Fixture: bypass preflight required-field check
# ===========================================================================

@pytest.fixture(autouse=True)
def bypass_preflight_required_fields():
    """Bypass the coding-pipeline-specific required-field preflight check.

    The smoke tests use minimal templates that only have a ``topic`` input
    field.  The ``REQUIRED_INPUT_FIELDS`` constant in
    ``orchestration_engine.preflight`` lists coding-pipeline fields
    (issue_title, branch_name, etc.) that are not relevant for generic
    smoke-test templates.

    This fixture patches that list to empty for every test in this module.
    Preflight behaviour for coding pipelines is tested separately in
    ``tests/test_preflight.py``.
    """
    with patch("orchestration_engine.preflight.REQUIRED_INPUT_FIELDS", []):
        yield


# ===========================================================================
# 1. Template parsing
# ===========================================================================

class TestSmokeTemplateParsing:
    """Verify that the fixture YAML files load and pass basic validation."""

    def test_minimal_template_loads(self):
        """The minimal-smoke.yaml fixture must load without error."""
        from orchestration_engine.templates import TemplateEngine

        engine = TemplateEngine()
        template = engine.load_template(_FIXTURES_DIR / "minimal-smoke.yaml")

        assert template.id == "minimal-smoke"
        assert len(template.phases) == 1
        assert template.phases[0].id == "smoke-phase"
        assert template.scenario is None  # no scenario configured

    def test_minimal_template_single_phase(self):
        """The minimal template must have exactly one phase."""
        from orchestration_engine.templates import TemplateEngine

        engine = TemplateEngine()
        template = engine.load_template(_FIXTURES_DIR / "minimal-smoke.yaml")

        assert len(template.phases) == 1, (
            f"Expected 1 phase, got {len(template.phases)}"
        )

    def test_scenario_template_loads(self):
        """The smoke-with-scenario.yaml fixture must load and reference a scenario."""
        from orchestration_engine.templates import TemplateEngine

        engine = TemplateEngine()
        template = engine.load_template(_FIXTURES_DIR / "smoke-with-scenario.yaml")

        assert template.id == "smoke-with-scenario"
        assert template.scenario is not None, "Expected scenario field to be set"

    def test_e2e_template_loads_and_validates(self):
        """The repository's e2e-test-template.yaml must load and pass validation."""
        from orchestration_engine.templates import TemplateEngine

        e2e_template_path = _REPO_ROOT / "scenarios" / "e2e-test-template.yaml"
        engine = TemplateEngine()
        template = engine.load_template(e2e_template_path)

        assert template.id == "e2e-test-template"
        assert len(template.phases) == 2
        phase_ids = [p.id for p in template.phases]
        assert "outline" in phase_ids
        assert "draft" in phase_ids


# ===========================================================================
# 2. Daemon execution — basic lifecycle
# ===========================================================================

class TestSmokeDaemonExecution:
    """run_daemon() must complete the full lifecycle for a minimal template."""

    def test_daemon_runs_successfully(self, tmp_path):
        """run_daemon() with a minimal dry-run template must not raise."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "smoke-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "smoke.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-run-001", template_path, out_dir)

        # Must return normally (no SystemExit, no exception)
        run_daemon(run_id, str(db_path))

    def test_daemon_creates_log_file(self, tmp_path):
        """run_daemon() must create .orch-daemon.log in the output directory."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "log-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "log.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-log-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        log_file = out_dir / ".orch-daemon.log"
        assert log_file.exists(), ".orch-daemon.log must be created"
        assert log_file.stat().st_size > 0, ".orch-daemon.log must not be empty"

    def test_daemon_removes_pid_file_on_success(self, tmp_path):
        """The .orch-daemon.pid file must be removed after a successful run."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "pid-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "pid.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-pid-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        pid_file = out_dir / ".orch-daemon.pid"
        assert not pid_file.exists(), ".orch-daemon.pid must be removed after success"

    def test_daemon_fails_gracefully_on_bad_template(self, tmp_path):
        """run_daemon() must mark run as 'failed' and sys.exit when template missing."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        out_dir = tmp_path / "bad-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "bad.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(
            db, "smoke-bad-001",
            tmp_path / "nonexistent.yaml",   # does not exist
            out_dir,
        )

        with pytest.raises(SystemExit):
            run_daemon(run_id, str(db_path))

        db2 = Database(db_path)
        run = db2.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "failed", (
            f"Expected 'failed', got {run['status']!r}"
        )

    def test_daemon_fails_gracefully_on_missing_run_id(self, tmp_path):
        """run_daemon() must sys.exit(1) when run_id is not in the DB."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        db_path = tmp_path / "empty.db"
        Database(db_path)  # create empty DB

        with pytest.raises(SystemExit) as exc_info:
            run_daemon("nonexistent-run-id", str(db_path))

        assert exc_info.value.code == 1


# ===========================================================================
# 3. Daemon execution — DB state after run
# ===========================================================================

class TestSmokeDaemonDBState:
    """Verify DB state is correct after a successful run_daemon() call."""

    _VALID_TERMINAL_STATUSES = {"success", "pending_review", "rejected"}

    def test_status_is_terminal_after_run(self, tmp_path):
        """Status must be a valid terminal state after a successful dry run."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "status-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "status.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-status-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] in self._VALID_TERMINAL_STATUSES, (
            f"Expected one of {self._VALID_TERMINAL_STATUSES}, got {run['status']!r}"
        )

    def test_completed_at_is_set(self, tmp_path):
        """completed_at must be set after a successful run."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "ts-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "ts.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-ts-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        assert run.get("completed_at") is not None, (
            "completed_at must be set after a successful run"
        )

    def test_completed_phases_written(self, tmp_path):
        """completed_phases must be written to the DB after execution."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "cp-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "cp.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-cp-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        raw_phases = run.get("completed_phases")
        assert raw_phases is not None, "completed_phases must be persisted to DB"
        completed_phases = json.loads(raw_phases)
        assert isinstance(completed_phases, list)
        assert "smoke-phase" in completed_phases, (
            f"Expected 'smoke-phase' in completed_phases, got {completed_phases}"
        )

    def test_phase_outputs_json_is_valid(self, tmp_path):
        """phase_outputs must be valid JSON written to the DB after execution."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "po-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "po.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-po-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        raw_outputs = run.get("phase_outputs")
        assert raw_outputs is not None, "phase_outputs must be persisted to DB"
        outputs = json.loads(raw_outputs)
        assert isinstance(outputs, dict)

    def test_no_scoring_status_without_scenario(self, tmp_path):
        """scoring_status must remain None when no scenario is configured."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_path = _FIXTURES_DIR / "minimal-smoke.yaml"
        out_dir = tmp_path / "noscoring-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "noscoring.db"
        db = Database(db_path)
        run_id = _insert_and_get_run(db, "smoke-nsc-001", template_path, out_dir)

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        assert run.get("scoring_status") is None, (
            f"Expected None (no scenario), got {run.get('scoring_status')!r}"
        )


# ===========================================================================
# 4. Daemon execution — scoring lifecycle
# ===========================================================================

class TestSmokeDaemonScenario:
    """Verify that run_daemon() correctly sets scoring_status when a scenario
    is present and auto-scoring is enabled.

    Uses mocked run_scoring to avoid real LLM judge calls.
    """

    _VALID_TERMINAL_STATUSES = {"success", "pending_review", "rejected"}

    def _setup(self, tmp_path, run_id: str, skip_scoring: int = 0):
        """Create DB + run record pointing to the smoke-with-scenario template."""
        from orchestration_engine.db import Database

        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)

        # Use the smoke-with-scenario template which has scenario: smoke-scenario.yaml
        _insert_and_get_run(
            db, run_id,
            _FIXTURES_DIR / "smoke-with-scenario.yaml",
            out_dir,
            skip_scoring=skip_scoring,
        )
        return db, db_path, out_dir

    def test_scoring_pass_sets_scoring_status_passed(self, tmp_path):
        """When run_scoring() returns (True, score), scoring_status='passed'."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        db, db_path, _ = self._setup(tmp_path, "smoke-sc-pass-001")

        with patch("orchestration_engine.scoring.run_scoring", return_value=(True, 0.88)):
            run_daemon("smoke-sc-pass-001", str(db_path))

        run = Database(db_path).get_pipeline_run("smoke-sc-pass-001")
        assert run is not None
        assert run["status"] in self._VALID_TERMINAL_STATUSES, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] == "passed", (
            f"Expected 'passed', got {run['scoring_status']!r}"
        )

    def test_scoring_fail_sets_status_scoring_failed(self, tmp_path):
        """When run_scoring() returns (False, score), status='scoring_failed'."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        db, db_path, _ = self._setup(tmp_path, "smoke-sc-fail-001")

        with patch("orchestration_engine.scoring.run_scoring", return_value=(False, 0.40)):
            run_daemon("smoke-sc-fail-001", str(db_path))

        run = Database(db_path).get_pipeline_run("smoke-sc-fail-001")
        assert run is not None
        assert run["status"] == "scoring_failed", (
            f"Expected 'scoring_failed', got {run['status']!r}"
        )
        assert run["scoring_status"] == "failed", (
            f"Expected 'failed', got {run['scoring_status']!r}"
        )

    def test_skip_scoring_leaves_scoring_status_none(self, tmp_path):
        """When skip_scoring=1, scoring block is skipped and scoring_status is None."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        db, db_path, _ = self._setup(tmp_path, "smoke-sc-skip-001", skip_scoring=1)

        run_daemon("smoke-sc-skip-001", str(db_path))

        run = Database(db_path).get_pipeline_run("smoke-sc-skip-001")
        assert run is not None
        assert run["status"] in self._VALID_TERMINAL_STATUSES, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] is None, (
            f"Expected None (skip_scoring=1), got {run['scoring_status']!r}"
        )

    def test_scoring_exception_sets_scoring_status_error(self, tmp_path):
        """When run_scoring() raises an exception, scoring_status='error'."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        db, db_path, _ = self._setup(tmp_path, "smoke-sc-err-001")

        with patch(
            "orchestration_engine.scoring.run_scoring",
            side_effect=RuntimeError("judge unavailable"),
        ):
            run_daemon("smoke-sc-err-001", str(db_path))

        run = Database(db_path).get_pipeline_run("smoke-sc-err-001")
        assert run is not None
        # Scoring infrastructure errors do NOT block the pipeline run
        assert run["status"] in self._VALID_TERMINAL_STATUSES, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] == "error", (
            f"Expected 'error', got {run['scoring_status']!r}"
        )


# ===========================================================================
# 5. ScenarioRunner — e2e-autonomous scenario (offline)
# ===========================================================================

class TestSmokeScenarioRunner:
    """Smoke-test the ScenarioRunner against the e2e-autonomous scenario.

    All tests run fully offline.  LLMJudgeGrader uses the ORCH_DRY_RUN stub.
    """

    def _dry_run_output(self) -> Dict[str, Any]:
        """Synthetic phase output from DryRunExecutor."""
        return {
            "task_id": "smoke-task-001",
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

    def test_e2e_scenario_loads(self):
        """e2e-autonomous.yaml must load as a valid scenario."""
        import yaml

        scenario_path = _REPO_ROOT / "scenarios" / "e2e-autonomous.yaml"
        assert scenario_path.exists(), f"Scenario file not found: {scenario_path}"

        with open(scenario_path) as f:
            data = yaml.safe_load(f)

        assert data["id"] == "e2e-autonomous"
        assert "acceptance" in data
        assert len(data["acceptance"]) >= 3

    def test_scenario_runner_passes_dry_run(self, monkeypatch):
        """ScenarioRunner with dry-run output must produce a passing result."""
        import os

        monkeypatch.setenv("ORCH_DRY_RUN", "1")

        from scenario_runner.runner import ScenarioRunner

        scenarios_dir = _REPO_ROOT / "scenarios"
        scenario_path = scenarios_dir / "e2e-autonomous.yaml"
        runner = ScenarioRunner(scenarios_dir=scenarios_dir)
        scenario = runner.load_scenario(scenario_path)
        output = self._dry_run_output()

        result = runner.run_scenario(scenario, output)

        assert result.passed, (
            f"Expected scenario to pass in dry-run mode, "
            f"score={result.weighted_score:.4f}, "
            f"failures={[c.criterion_id for c in result.criterion_results if not c.passed]}"
        )

    def test_keyword_grader_matches_dry_run_output(self):
        """KeywordGrader must match DRY-RUN keywords in synthetic executor output."""
        from scenario_runner.graders.keyword_grader import KeywordGrader

        grader = KeywordGrader()
        output = self._dry_run_output()
        result = grader.grade(output, keywords=["mock", "execution", "content"], match_mode="any")

        assert result.passed, (
            f"Expected keyword grader to pass with dry-run output, score={result.score}"
        )
        assert result.score == 1.0, (
            f"Expected score 1.0 (match_mode='any', at least one keyword found), "
            f"got {result.score}"
        )

    def test_assertion_grader_non_empty_output(self):
        """Assertion grader 'len(str(output)) > 10' must pass on non-trivial output."""
        from scenario_runner.graders.assertion import AssertionGrader

        grader = AssertionGrader()
        output = self._dry_run_output()
        result = grader.grade("len(str(output)) > 10", output)

        assert result.passed, (
            f"Expected assertion grader to pass on non-empty output, "
            f"details={result.details}"
        )

    def test_scenario_weighted_score_dry_run(self, monkeypatch):
        """Dry-run weighted score must be >= 0.70 (e2e-autonomous pass threshold)."""
        monkeypatch.setenv("ORCH_DRY_RUN", "1")

        from scenario_runner.runner import ScenarioRunner

        scenarios_dir = _REPO_ROOT / "scenarios"
        scenario_path = scenarios_dir / "e2e-autonomous.yaml"
        runner = ScenarioRunner(scenarios_dir=scenarios_dir)
        scenario = runner.load_scenario(scenario_path)
        output = self._dry_run_output()

        result = runner.run_scenario(scenario, output)

        assert result.weighted_score >= 0.70, (
            f"Expected weighted score >= 0.70 (pass_threshold), "
            f"got {result.weighted_score:.4f}"
        )
