"""Tests for PipelineRunner and the wired `orch run` CLI command.

All tests use DryRunExecutor or mock the AnthropicExecutor._call_api — no
real API calls are made.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from orchestration_engine.pipeline_runner import PipelineRunner
from orchestration_engine.runner import DryRunExecutor
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.schemas import TaskType, TaskState
from orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hello_yaml(tmp_path) -> Path:
    """Write the examples/hello-pipeline.yaml into a temp dir."""
    src = Path(__file__).parent.parent / "examples" / "hello-pipeline.yaml"
    dest = tmp_path / "hello-pipeline.yaml"
    dest.write_text(src.read_text())
    return dest


@pytest.fixture
def two_phase_template() -> PipelineTemplate:
    """Simple A → B pipeline template for unit tests."""
    return PipelineTemplate(
        id="two-phase",
        name="Two Phase",
        version="1.0",
        phases=[
            PhaseDefinition(
                id="phase_a",
                name="Phase A",
                depends_on=[],
                prompt_template="Input: {input[key]}",
            ),
            PhaseDefinition(
                id="phase_b",
                name="Phase B",
                depends_on=["phase_a"],
                prompt_template="After A: {previous_output[phase_a]}",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# PipelineRunner Unit Tests
# ---------------------------------------------------------------------------

class TestPipelineRunnerConstruction:
    """Test PipelineRunner instantiation and factory methods."""

    def test_dry_run_factory_creates_runner(self):
        runner = PipelineRunner.dry_run()
        assert runner.queue is not None
        assert len(runner.executors) == 1
        assert isinstance(runner.executors[0], DryRunExecutor)

    def test_dry_run_has_queue_and_executors_attributes(self):
        """PhaseSequencer contract: runner.queue and runner.executors."""
        runner = PipelineRunner.dry_run()
        assert hasattr(runner, 'queue')
        assert hasattr(runner, 'executors')

    def test_context_manager_closes_db(self):
        with PipelineRunner.dry_run() as runner:
            assert runner.queue is not None
        # After exit, close() should have been called without error

    def test_standalone_no_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            PipelineRunner.standalone(api_key="")

    def test_standalone_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        runner = PipelineRunner.standalone()
        assert runner.executors[0].api_key == "sk-ant-test"
        runner.close()

    def test_standalone_explicit_api_key(self):
        runner = PipelineRunner.standalone(api_key="sk-ant-explicit")
        assert runner.executors[0].api_key == "sk-ant-explicit"
        runner.close()

    def test_in_memory_db_by_default(self):
        runner = PipelineRunner.dry_run()
        assert runner._db_path == ":memory:"
        runner.close()

    def test_temp_db_creates_file(self, tmp_path):
        runner = PipelineRunner.dry_run(db_path="temp")
        assert runner._db_path != ":memory:"
        assert runner._db_path != "temp"
        runner.close()


class TestPipelineRunnerIntegration:
    """Test PipelineRunner integrated with PhaseSequencer."""

    def test_dry_run_two_phase_pipeline(self, two_phase_template):
        with PipelineRunner.dry_run(delay_seconds=0.0) as runner:
            seq = PhaseSequencer(two_phase_template, runner)
            result = seq.execute({"key": "test_value"})

        assert "phase_outputs" in result
        assert "phase_a" in result["phase_outputs"]
        assert "phase_b" in result["phase_outputs"]
        assert result.get("aborted") is not True

    def test_phase_failure_aborts_pipeline(self, two_phase_template):
        with PipelineRunner.dry_run(delay_seconds=0.0, failure_rate=1.0) as runner:
            seq = PhaseSequencer(two_phase_template, runner)
            result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("failed_phase") == "phase_a"
        assert "phase_b" not in result["phase_outputs"]

    def test_standalone_mode_calls_anthropic(self, two_phase_template):
        """AnthropicExecutor is called when mode=standalone."""
        mock_response = {
            "content": [{"type": "text", "text": "mocked output"}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        from orchestration_engine.executors.anthropic_executor import AnthropicExecutor

        with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
            with PipelineRunner.standalone(api_key="sk-ant-test") as runner:
                seq = PhaseSequencer(two_phase_template, runner)
                result = seq.execute({"key": "value"})

        assert result.get("aborted") is not True
        assert "phase_a" in result["phase_outputs"]
        assert result["phase_outputs"]["phase_a"]["state"] == TaskState.SUCCESS.value

    def test_output_per_phase_is_dict(self, two_phase_template):
        with PipelineRunner.dry_run() as runner:
            seq = PhaseSequencer(two_phase_template, runner)
            result = seq.execute({})
        for pid, out in result["phase_outputs"].items():
            assert isinstance(out, dict), f"Phase '{pid}' output must be a dict"


# ---------------------------------------------------------------------------
# CLI Integration Tests (using Click test runner)
# ---------------------------------------------------------------------------

class TestCliRunCommand:
    """Test the `orch run` CLI command end-to-end."""

    def _invoke(self, args, env=None):
        runner = CliRunner()
        return runner.invoke(main, args, env=env or {}, catch_exceptions=False)

    def test_dry_run_succeeds(self, hello_yaml):
        result = self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--input", '{"name": "René"}',
        ])
        assert result.exit_code == 0, result.output
        assert "Pipeline completed" in result.output

    def test_dry_run_shows_phase_results(self, hello_yaml):
        result = self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
        ])
        assert "greet" in result.output
        assert "summarize" in result.output

    def test_standalone_no_key_exits_1(self, hello_yaml, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(hello_yaml),
            "--mode", "standalone",
        ], env={"ANTHROPIC_API_KEY": ""}, catch_exceptions=False)
        assert result.exit_code == 1
        assert "API key" in result.output

    def test_standalone_with_mocked_api(self, hello_yaml):
        mock_response = {
            "content": [{"type": "text", "text": "Hello, René!"}],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
        with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
            result = self._invoke([
                "run", str(hello_yaml),
                "--mode", "standalone",
                "--api-key", "sk-ant-test",
                "--input", '{"name": "René"}',
            ])
        assert result.exit_code == 0, result.output
        assert "completed" in result.output

    def test_input_file_flag(self, hello_yaml, tmp_path):
        input_file = tmp_path / "input.json"
        input_file.write_text('{"name": "Alice"}')
        result = self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--input-file", str(input_file),
        ])
        assert result.exit_code == 0, result.output

    def test_output_dir_writes_json(self, hello_yaml, tmp_path):
        out_dir = tmp_path / "results"
        result = self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--output-dir", str(out_dir),
        ])
        assert result.exit_code == 0
        assert (out_dir / "greet.json").exists()
        assert (out_dir / "summarize.json").exists()
        assert (out_dir / "_final_output.json").exists()

    def test_output_dir_contains_valid_json(self, hello_yaml, tmp_path):
        out_dir = tmp_path / "results"
        self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--output-dir", str(out_dir),
        ])
        greet_data = json.loads((out_dir / "greet.json").read_text())
        assert isinstance(greet_data, dict)

    def test_invalid_template_exits_1(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("name: Missing ID Pipeline\n")
        result = CliRunner().invoke(main, ["run", str(bad_yaml)], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Invalid template" in result.output or "error" in result.output.lower()

    def test_invalid_json_input_exits_1(self, hello_yaml):
        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--input", "not-valid-json",
        ], catch_exceptions=False)
        assert result.exit_code == 1
        assert "JSON" in result.output

    def test_phase_failure_exits_2(self, hello_yaml):
        result = self._invoke([
            "run", str(hello_yaml),
            "--mode", "dry-run",
            "--dry-run-failure-rate", "1.0",
        ])
        assert result.exit_code == 2
        assert "aborted" in result.output.lower()

    def test_validate_command_still_works(self, hello_yaml):
        """Ensure existing validate command is not broken by changes."""
        result = self._invoke(["validate", str(hello_yaml)])
        assert result.exit_code == 0
        assert "valid" in result.output
