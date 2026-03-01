"""Tests for Issue #172 — Post-Pipeline Auto-Scoring.

Covers:
  - PipelineTemplate.scenario field (dataclass + load_template)
  - scoring.run_scoring() helper
  - scoring._load_pipeline_output() helper
  - CLI --skip-scoring flag
  - CLI --score-only flag
  - Auto-scoring wired into orch run
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.templates import PipelineTemplate, TemplateEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_yaml(tmp_path) -> Path:
    """Minimal valid pipeline YAML with no scenario field."""
    content = """\
id: test-pipeline
name: Test Pipeline
version: "1.0.0"
description: Test pipeline for scoring tests.
author: Test Author
use_cases: ["testing"]
example_input:
  input: value
phases:
  - id: phase1
    name: Phase One
    prompt_template: "Do something with {input}"
    model_tier: haiku
    thinking_level: off
"""
    p = tmp_path / "test-pipeline.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def pipeline_with_scenario(tmp_path) -> tuple:
    """Pipeline YAML with scenario field + matching scenario YAML.

    Returns (template_path, scenario_path).
    """
    scenario_content = """\
id: test-scenario
name: Test Scenario
acceptance:
  - id: not_empty
    type: assertion
    check: "len(str(output)) > 0"
    weight: 0
scoring:
  pass_threshold: 0.5
"""
    scenario_path = tmp_path / "test-scenario.yaml"
    scenario_path.write_text(scenario_content)

    template_content = f"""\
id: test-pipeline-scored
name: Test Pipeline Scored
version: "1.0.0"
description: Test pipeline with scenario for auto-scoring.
author: Test Author
use_cases: ["testing"]
example_input:
  input: value
scenario: test-scenario.yaml
phases:
  - id: phase1
    name: Phase One
    prompt_template: "Do something with {{input}}"
    model_tier: haiku
    thinking_level: off
"""
    template_path = tmp_path / "test-pipeline-scored.yaml"
    template_path.write_text(template_content)
    return template_path, scenario_path


@pytest.fixture
def completed_output_dir(tmp_path) -> Path:
    """A fake completed pipeline output directory."""
    out_dir = tmp_path / "output" / "test-run"
    out_dir.mkdir(parents=True)

    # Write _final_output.json
    (out_dir / "_final_output.json").write_text(
        json.dumps({"result": "test output text"})
    )
    (out_dir / "_final_output.md").write_text("# Final Output\n\ntest output text\n")

    # Write per-phase output
    (out_dir / "phase1.json").write_text(
        json.dumps({"state": "success", "result": {"text": "phase1 output"}})
    )
    (out_dir / "phase1.md").write_text("# Phase: phase1\n\nphase1 output\n")
    (out_dir / "_summary.md").write_text("# Run Summary\n\n")

    return out_dir


def _invoke(args, env=None):
    """Invoke the orch CLI via CliRunner."""
    runner = CliRunner()
    return runner.invoke(main, args, env=env or {}, catch_exceptions=False)


# ---------------------------------------------------------------------------
# PipelineTemplate.scenario field
# ---------------------------------------------------------------------------

class TestPipelineTemplateScenarioField:
    """The PipelineTemplate dataclass should have an optional scenario field."""

    def test_scenario_defaults_to_none(self):
        """PipelineTemplate.scenario should default to None."""
        template = PipelineTemplate(id="x", name="X")
        assert template.scenario is None

    def test_scenario_can_be_set(self):
        """PipelineTemplate.scenario can be set to a string path."""
        template = PipelineTemplate(id="x", name="X", scenario="my-scenario.yaml")
        assert template.scenario == "my-scenario.yaml"

    def test_empty_string_normalised_to_none(self):
        """An empty string value for scenario is normalised to None in __post_init__."""
        template = PipelineTemplate(id="x", name="X", scenario="")
        assert template.scenario is None

    def test_none_stays_none(self):
        """Explicitly passing None leaves scenario as None."""
        template = PipelineTemplate(id="x", name="X", scenario=None)
        assert template.scenario is None


# ---------------------------------------------------------------------------
# TemplateEngine.load_template — parses scenario: YAML key
# ---------------------------------------------------------------------------

class TestLoadTemplateScenarioField:
    """TemplateEngine.load_template() should parse the scenario: key."""

    def test_load_template_without_scenario(self, minimal_yaml):
        """Templates without scenario: should load with scenario=None."""
        engine = TemplateEngine()
        template = engine.load_template(minimal_yaml)
        assert template.scenario is None

    def test_load_template_with_scenario_path(self, tmp_path):
        """Templates with scenario: should populate template.scenario."""
        content = """\
id: t
name: T
version: "1.0.0"
description: desc
author: au
use_cases: []
example_input: {}
scenario: "my-scenario.yaml"
phases:
  - id: p1
    name: P1
    prompt_template: "hello {input}"
    model_tier: haiku
    thinking_level: off
"""
        p = tmp_path / "t.yaml"
        p.write_text(content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        assert template.scenario == "my-scenario.yaml"

    def test_load_template_with_empty_scenario(self, tmp_path):
        """Templates with empty scenario: '' should result in scenario=None."""
        content = """\
id: t
name: T
version: "1.0.0"
description: desc
author: au
use_cases: []
example_input: {}
scenario: ""
phases:
  - id: p1
    name: P1
    prompt_template: "hello {input}"
    model_tier: haiku
    thinking_level: off
"""
        p = tmp_path / "t.yaml"
        p.write_text(content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        assert template.scenario is None

    def test_load_template_scenario_path_preserved(self, tmp_path):
        """The scenario path string should be preserved exactly as written."""
        content = """\
id: t
name: T
version: "1.0.0"
description: desc
author: au
use_cases: []
example_input: {}
scenario: "scenarios/subdir/my-scenario.yaml"
phases:
  - id: p1
    name: P1
    prompt_template: "hello {input}"
    model_tier: haiku
    thinking_level: off
"""
        p = tmp_path / "t.yaml"
        p.write_text(content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        assert template.scenario == "scenarios/subdir/my-scenario.yaml"


# ---------------------------------------------------------------------------
# scoring._load_pipeline_output
# ---------------------------------------------------------------------------

class TestLoadPipelineOutput:
    """scoring._load_pipeline_output() should load output files correctly."""

    def test_loads_final_output(self, completed_output_dir):
        from orchestration_engine.scoring import _load_pipeline_output
        output = _load_pipeline_output(completed_output_dir)
        assert "final" in output
        assert output["final"] == {"result": "test output text"}

    def test_loads_phase_outputs(self, completed_output_dir):
        from orchestration_engine.scoring import _load_pipeline_output
        output = _load_pipeline_output(completed_output_dir)
        assert "phases" in output
        assert "phase1" in output["phases"]

    def test_excludes_underscore_prefixed_files(self, completed_output_dir):
        from orchestration_engine.scoring import _load_pipeline_output
        output = _load_pipeline_output(completed_output_dir)
        # _summary, _final_output should NOT appear in phases
        assert "_summary" not in output["phases"]
        assert "_final_output" not in output["phases"]

    def test_missing_final_output_returns_empty_dict(self, tmp_path):
        from orchestration_engine.scoring import _load_pipeline_output
        out_dir = tmp_path / "empty-run"
        out_dir.mkdir()
        output = _load_pipeline_output(out_dir)
        assert output["final"] == {}
        assert output["phases"] == {}

    def test_malformed_json_is_skipped(self, tmp_path):
        from orchestration_engine.scoring import _load_pipeline_output
        out_dir = tmp_path / "bad-run"
        out_dir.mkdir()
        (out_dir / "_final_output.json").write_text("{bad json}")
        (out_dir / "phase1.json").write_text("not json at all")
        output = _load_pipeline_output(out_dir)
        assert output["final"] == {}
        assert "phase1" not in output["phases"]


# ---------------------------------------------------------------------------
# scoring.run_scoring() — unit tests with mocks
# ---------------------------------------------------------------------------

class TestRunScoringUnit:
    """Unit tests for scoring.run_scoring() using mocked ScenarioRunner."""

    def _make_template(self, scenario: str = "test-scenario.yaml") -> PipelineTemplate:
        """Return a minimal PipelineTemplate with scenario set."""
        from pathlib import Path as _Path
        t = PipelineTemplate(id="t", name="T", scenario=scenario)
        # Simulate template_path being set (as done by load_template)
        t.template_path = _Path("/fake/templates/t.yaml")
        return t

    def _make_score_result(self, passed: bool = True) -> MagicMock:
        """Build a minimal ScenarioResult mock."""
        result = MagicMock()
        result.passed = passed
        result.weighted_score = 0.9 if passed else 0.4
        result.gates_passed = passed
        result.scenario_id = "test-scenario"
        result.criterion_results = []
        return result

    def test_raises_valueerror_when_scenario_is_none(self, tmp_path):
        from orchestration_engine.scoring import run_scoring
        template = PipelineTemplate(id="t", name="T")  # scenario=None
        with pytest.raises(ValueError, match="template.scenario is not set"):
            run_scoring(template, output_dir=tmp_path, exit_on_failure=False)

    def test_raises_filenotfounderror_when_scenario_file_missing(self, tmp_path):
        from orchestration_engine.scoring import run_scoring
        template = PipelineTemplate(id="t", name="T", scenario="nonexistent.yaml")
        template.template_path = tmp_path / "t.yaml"
        with pytest.raises(FileNotFoundError, match="Scenario file not found"):
            run_scoring(template, output_dir=tmp_path, exit_on_failure=False)

    def test_returns_true_when_scenario_passes(self, tmp_path, completed_output_dir):
        """run_scoring returns True when the scenario passes."""
        from orchestration_engine.scoring import run_scoring

        # Write a minimal scenario file
        scenario_path = tmp_path / "test-scenario.yaml"
        scenario_path.write_text(
            "id: test-scenario\nacceptance:\n  - id: c1\n    type: assertion\n"
            "    check: 'True'\n    weight: 1\nscoring:\n  pass_threshold: 0.5\n"
        )

        template = PipelineTemplate(id="t", name="T", scenario="test-scenario.yaml")
        template.template_path = tmp_path / "t.yaml"

        score_result = self._make_score_result(passed=True)

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "test-scenario",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.5},
            }
            mock_instance.run_scenario.return_value = score_result

            from rich.console import Console
            console = Console(file=open(os.devnull, "w"))
            result = run_scoring(
                template,
                output_dir=completed_output_dir,
                console=console,
                exit_on_failure=False,
            )

        assert result is True

    def test_returns_false_when_scenario_fails(self, tmp_path, completed_output_dir):
        """run_scoring returns False when the scenario does not pass."""
        from orchestration_engine.scoring import run_scoring

        scenario_path = tmp_path / "test-scenario.yaml"
        scenario_path.write_text(
            "id: test-scenario\nacceptance: []\nscoring:\n  pass_threshold: 0.99\n"
        )

        template = PipelineTemplate(id="t", name="T", scenario="test-scenario.yaml")
        template.template_path = tmp_path / "t.yaml"

        score_result = self._make_score_result(passed=False)

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "test-scenario",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.99},
            }
            mock_instance.run_scenario.return_value = score_result

            from rich.console import Console
            console = Console(file=open(os.devnull, "w"))
            result = run_scoring(
                template,
                output_dir=completed_output_dir,
                console=console,
                exit_on_failure=False,  # Don't sys.exit in tests
            )

        assert result is False


# ---------------------------------------------------------------------------
# CLI — --skip-scoring flag
# ---------------------------------------------------------------------------

class TestSkipScoringFlag:
    """The --skip-scoring flag should prevent auto-scoring from running."""

    def test_skip_scoring_suppresses_scoring(self, pipeline_with_scenario, tmp_path, monkeypatch):
        """When --skip-scoring is passed, run_scoring should never be called."""
        template_path, _ = pipeline_with_scenario
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "out"

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            result = _invoke([
                "run", str(template_path),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
                "--skip-scoring",
            ])

        # Pipeline should succeed
        assert result.exit_code == 0, result.output
        # ScenarioRunner should NOT be called when --skip-scoring is passed
        mock_cls.assert_not_called()

    def test_skip_scoring_flag_in_help(self):
        """The --skip-scoring flag should appear in help text."""
        result = _invoke(["run", "--help"])
        assert "skip-scoring" in result.output.lower()

    def test_score_only_flag_in_help(self):
        """The --score-only flag should appear in help text."""
        result = _invoke(["run", "--help"])
        assert "score-only" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI — --score-only flag
# ---------------------------------------------------------------------------

class TestScoreOnlyFlag:
    """The --score-only flag should run scoring against an existing output dir."""

    def test_score_only_requires_output_dir(self, minimal_yaml, tmp_path, monkeypatch):
        """--score-only without --output-dir should exit with code 1."""
        monkeypatch.chdir(tmp_path)
        # mix_stderr=True so error messages appear in result.output
        result = CliRunner(mix_stderr=True).invoke(
            main,
            ["run", str(minimal_yaml), "--mode", "dry-run", "--score-only"],
            env={},
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "output-dir" in result.output.lower() or "output_dir" in result.output.lower()

    def test_score_only_requires_scenario_in_template(
        self, minimal_yaml, completed_output_dir, tmp_path, monkeypatch
    ):
        """--score-only on a template without 'scenario:' should exit with code 1."""
        monkeypatch.chdir(tmp_path)
        # mix_stderr=True so error messages appear in result.output
        result = CliRunner(mix_stderr=True).invoke(
            main,
            [
                "run", str(minimal_yaml),
                "--mode", "dry-run",
                "--score-only",
                "--output-dir", str(completed_output_dir),
            ],
            env={},
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "scenario" in result.output.lower()

    def test_score_only_calls_run_scoring(
        self, pipeline_with_scenario, completed_output_dir, tmp_path, monkeypatch
    ):
        """--score-only should call run_scoring with the output dir."""
        template_path, _ = pipeline_with_scenario
        monkeypatch.chdir(tmp_path)

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.weighted_score = 0.9
        mock_result.gates_passed = True
        mock_result.scenario_id = "test-scenario"
        mock_result.criterion_results = []

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "test-scenario",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.5},
            }
            mock_instance.run_scenario.return_value = mock_result

            result = CliRunner().invoke(
                main,
                [
                    "run", str(template_path),
                    "--mode", "dry-run",
                    "--score-only",
                    "--output-dir", str(completed_output_dir),
                ],
                env={},
                catch_exceptions=False,
            )

        # Should call run_scenario (scoring was invoked)
        mock_instance.run_scenario.assert_called_once()
        # Exit code 0 = scenario passed
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI — auto-scoring wired into run_template
# ---------------------------------------------------------------------------

class TestAutoScoringInRunTemplate:
    """When template.scenario is set and --skip-scoring is NOT passed,
    scoring should run automatically after pipeline completion."""

    def test_auto_scoring_runs_after_pipeline(
        self, pipeline_with_scenario, tmp_path, monkeypatch
    ):
        """Scoring is invoked after a successful dry-run when scenario is set."""
        template_path, _ = pipeline_with_scenario
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "scored-run"

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.weighted_score = 0.88
        mock_result.gates_passed = True
        mock_result.scenario_id = "test-scenario"
        mock_result.criterion_results = []

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "test-scenario",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.5},
            }
            mock_instance.run_scenario.return_value = mock_result

            result = _invoke([
                "run", str(template_path),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
            ])

        assert result.exit_code == 0, result.output
        # ScenarioRunner should have been instantiated (scoring ran)
        mock_cls.assert_called()

    def test_no_auto_scoring_without_scenario_field(
        self, minimal_yaml, tmp_path, monkeypatch
    ):
        """Templates without scenario: should NOT invoke scoring."""
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "no-score-run"

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            result = _invoke([
                "run", str(minimal_yaml),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
            ])

        assert result.exit_code == 0, result.output
        mock_cls.assert_not_called()

    def test_skip_scoring_prevents_auto_scoring(
        self, pipeline_with_scenario, tmp_path, monkeypatch
    ):
        """--skip-scoring suppresses auto-scoring even when scenario is set."""
        template_path, _ = pipeline_with_scenario
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "skipped-scoring-run"

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            result = _invoke([
                "run", str(template_path),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
                "--skip-scoring",
            ])

        assert result.exit_code == 0, result.output
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# update_pipeline_run — scoring_status / scoring_score fields (Issue #287)
# ---------------------------------------------------------------------------


class TestUpdatePipelineRunScoringFields:
    """Verify that update_pipeline_run() accepts and persists scoring_status
    and scoring_score, which are used by the daemon to record scoring outcome.
    """

    @pytest.fixture
    def db(self, tmp_path):
        from orchestration_engine.db import Database
        return Database(tmp_path / "scoring-fields.db")

    @pytest.fixture
    def run_id(self, db):
        rid = "scoring-fields-run"
        db.insert_pipeline_run({
            "run_id": rid,
            "template_path": "/fake/t.yaml",
            "template_id": "test",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": "/tmp/out",
        })
        return rid

    def test_scoring_status_accepted_by_update(self, db, run_id):
        """update_pipeline_run() should accept 'scoring_status' without error."""
        result = db.update_pipeline_run(run_id, scoring_status="passed")
        assert result is True

    def test_scoring_score_accepted_by_update(self, db, run_id):
        """update_pipeline_run() should accept 'scoring_score' without error."""
        result = db.update_pipeline_run(run_id, scoring_score=0.9)
        assert result is True

    def test_scoring_status_persisted(self, db, run_id):
        """scoring_status='passed' should be readable back from the DB."""
        db.update_pipeline_run(run_id, scoring_status="passed")
        run = db.get_pipeline_run(run_id)
        assert run["scoring_status"] == "passed"

    def test_scoring_status_failed_persisted(self, db, run_id):
        """scoring_status='failed' should be readable back from the DB."""
        db.update_pipeline_run(run_id, scoring_status="failed")
        run = db.get_pipeline_run(run_id)
        assert run["scoring_status"] == "failed"

    def test_scoring_status_error_persisted(self, db, run_id):
        """scoring_status='error' should be readable back from the DB."""
        db.update_pipeline_run(run_id, scoring_status="error")
        run = db.get_pipeline_run(run_id)
        assert run["scoring_status"] == "error"

    def test_scoring_score_persisted(self, db, run_id):
        """scoring_score should round-trip through the DB accurately."""
        db.update_pipeline_run(run_id, scoring_status="passed", scoring_score=0.75)
        run = db.get_pipeline_run(run_id)
        assert abs(run["scoring_score"] - 0.75) < 1e-6

    def test_default_scoring_status_is_null(self, db, run_id):
        """A freshly inserted run should have scoring_status=None."""
        run = db.get_pipeline_run(run_id)
        assert run["scoring_status"] is None

    def test_default_scoring_score_is_null(self, db, run_id):
        """A freshly inserted run should have scoring_score=None."""
        run = db.get_pipeline_run(run_id)
        assert run["scoring_score"] is None

    def test_unknown_field_is_silently_ignored(self, db, run_id):
        """Unknown kwargs to update_pipeline_run() should be silently ignored."""
        # Should not raise — 'bogus_field' is not in the allowed set
        result = db.update_pipeline_run(run_id, bogus_field="ignored")
        # Returns False because no valid field was updated
        assert result is False
