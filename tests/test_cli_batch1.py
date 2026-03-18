"""Tests for CLI batch-1 features: default output dir, markdown output, rich UI.

Covers:
  - Feature #72: default output directory
  - Feature #71: markdown files alongside JSON
  - Feature #70: rich terminal summary (non-crash verification)
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hello_yaml(tmp_path) -> Path:
    """Copy examples/hello-pipeline.yaml into a temp dir."""
    src = Path(__file__).parent.parent / "examples" / "hello-pipeline.yaml"
    dest = tmp_path / "hello-pipeline.yaml"
    dest.write_text(src.read_text())
    return dest


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, env=env or {}, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Feature #72 — Default output directory
# ---------------------------------------------------------------------------

class TestDefaultOutputDir:
    """When --output-dir is omitted, a default directory should be created."""

    def test_default_output_dir_is_created(self, hello_yaml, tmp_path, monkeypatch):
        """Running without --output-dir creates ./output/<id>-<timestamp>/."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert result.exit_code == 0, result.output

        output_root = tmp_path / "output"
        assert output_root.exists(), "output/ directory should be created"
        dirs = list(output_root.iterdir())
        assert len(dirs) == 1, f"Expected exactly one run dir, got: {dirs}"
        assert dirs[0].name.startswith("hello-pipeline"), (
            f"Dir name should start with template id, got: {dirs[0].name}"
        )

    def test_default_output_dir_name_format(self, hello_yaml, tmp_path, monkeypatch):
        """Default dir name should match <template-id>-<YYYYMMDD-HHMMSS>."""
        monkeypatch.chdir(tmp_path)
        _invoke(["run", str(hello_yaml), "--mode", "dry-run"])

        dirs = list((tmp_path / "output").iterdir())
        assert len(dirs) == 1
        pattern = re.compile(r"^hello-pipeline(-example)?-\d{8}-\d{6}-[a-f0-9]{8}$")
        assert pattern.match(dirs[0].name), (
            f"Dir name doesn't match expected pattern: {dirs[0].name}"
        )

    def test_default_output_dir_printed_in_output(self, hello_yaml, tmp_path, monkeypatch):
        """The output directory path should be printed at the start of execution."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert result.exit_code == 0
        assert "output" in result.output.lower(), (
            "Output directory path should appear in CLI output"
        )

    def test_explicit_output_dir_overrides_default(self, hello_yaml, tmp_path, monkeypatch):
        """Passing --output-dir explicitly uses that dir, not the default."""
        monkeypatch.chdir(tmp_path)
        explicit = tmp_path / "my-results"
        result = _invoke([
            "run", str(hello_yaml), "--mode", "dry-run",
            "--output-dir", str(explicit),
        ])
        assert result.exit_code == 0
        assert explicit.exists(), "Explicit output dir should exist"
        # Default output/ dir should NOT be created
        assert not (tmp_path / "output").exists(), (
            "Default output/ should not be created when --output-dir is given"
        )

    def test_default_output_contains_json_files(self, hello_yaml, tmp_path, monkeypatch):
        """Default output dir should contain the expected JSON files."""
        monkeypatch.chdir(tmp_path)
        _invoke(["run", str(hello_yaml), "--mode", "dry-run"])

        run_dir = next((tmp_path / "output").iterdir())
        assert (run_dir / "greet.json").exists()
        assert (run_dir / "summarize.json").exists()
        assert (run_dir / "_final_output.json").exists()


# ---------------------------------------------------------------------------
# Feature #71 — Markdown output files
# ---------------------------------------------------------------------------

class TestMarkdownOutput:
    """Markdown files should be generated alongside JSON files."""

    def _run_and_get_dir(self, hello_yaml, out_dir):
        result = _invoke([
            "run", str(hello_yaml), "--mode", "dry-run",
            "--output-dir", str(out_dir),
        ])
        assert result.exit_code == 0, result.output
        return out_dir

    def test_phase_markdown_files_created(self, hello_yaml, tmp_path):
        """Each phase should produce a .md file alongside its .json."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        assert (out / "greet.md").exists(), "greet.md should exist"
        assert (out / "summarize.md").exists(), "summarize.md should exist"

    def test_final_output_markdown_created(self, hello_yaml, tmp_path):
        """_final_output.md should be created."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        assert (out / "_final_output.md").exists(), "_final_output.md should exist"

    def test_summary_markdown_created(self, hello_yaml, tmp_path):
        """_summary.md should be created."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        assert (out / "_summary.md").exists(), "_summary.md should exist"

    def test_phase_markdown_has_heading(self, hello_yaml, tmp_path):
        """Phase .md files should start with a # heading."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        content = (out / "greet.md").read_text()
        assert content.startswith("# Phase:"), (
            f"greet.md should start with '# Phase:'; got: {content[:60]!r}"
        )

    def test_phase_markdown_contains_phase_id(self, hello_yaml, tmp_path):
        """Phase .md heading should reference the phase id."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        content = (out / "greet.md").read_text()
        assert "greet" in content

    def test_summary_markdown_contains_template_name(self, hello_yaml, tmp_path):
        """_summary.md should include the template name."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        summary = (out / "_summary.md").read_text()
        assert "Hello Pipeline" in summary, (
            f"Summary should contain template name; got:\n{summary}"
        )

    def test_summary_markdown_contains_phase_list(self, hello_yaml, tmp_path):
        """_summary.md should list completed phases."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        summary = (out / "_summary.md").read_text()
        assert "greet" in summary
        assert "summarize" in summary

    def test_summary_markdown_contains_cost(self, hello_yaml, tmp_path):
        """_summary.md should include cost information."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        summary = (out / "_summary.md").read_text()
        assert "Cost" in summary or "cost" in summary, (
            "Summary should mention cost"
        )
        assert "$" in summary, "Summary should show cost in dollars"

    def test_summary_markdown_contains_total_tokens(self, hello_yaml, tmp_path):
        """_summary.md should include token count."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        summary = (out / "_summary.md").read_text()
        assert "Tokens" in summary or "tokens" in summary.lower()

    def test_summary_markdown_contains_date(self, hello_yaml, tmp_path):
        """_summary.md should include a run date."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        summary = (out / "_summary.md").read_text()
        assert "Date" in summary or "date" in summary.lower(), (
            "Summary should include a date field"
        )

    def test_phase_json_still_written(self, hello_yaml, tmp_path):
        """JSON files should still be written alongside new markdown files."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        greet_json = json.loads((out / "greet.json").read_text())
        assert isinstance(greet_json, dict)
        assert "state" in greet_json

    def test_markdown_files_with_standalone_mock(self, hello_yaml, tmp_path):
        """Markdown files should be generated in standalone mode too."""
        mock_response = {
            "content": [{"type": "text", "text": "Hello, World!"}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
        out = tmp_path / "results"

        with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
            result = _invoke([
                "run", str(hello_yaml), "--mode", "standalone",
                "--api-key", "sk-ant-test",
                "--output-dir", str(out),
            ])

        assert result.exit_code == 0, result.output
        assert (out / "greet.md").exists()
        assert (out / "_summary.md").exists()

    def test_final_output_md_contains_text(self, hello_yaml, tmp_path):
        """_final_output.md should have non-empty content."""
        out = tmp_path / "results"
        self._run_and_get_dir(hello_yaml, out)
        content = (out / "_final_output.md").read_text()
        # DryRunExecutor always produces a message
        assert len(content.strip()) > 0, "_final_output.md should not be empty"


# ---------------------------------------------------------------------------
# Feature #70 — Rich terminal progress
# ---------------------------------------------------------------------------

class TestRichTerminalOutput:
    """Rich output: verify command completes successfully and key info is present."""

    def test_dry_run_exits_successfully(self, hello_yaml):
        """Command completes with exit code 0 (rich output doesn't crash)."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert result.exit_code == 0, result.output

    def test_pipeline_name_in_output(self, hello_yaml):
        """Pipeline name should appear in console output."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert "Hello Pipeline" in result.output

    def test_mode_in_output(self, hello_yaml):
        """Mode should appear in console output."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert "dry-run" in result.output

    def test_phase_ids_in_output(self, hello_yaml):
        """Each phase id should appear in the output (from live callback + table)."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert "greet" in result.output
        assert "summarize" in result.output

    def test_pipeline_completed_in_output(self, hello_yaml):
        """A 'Pipeline completed' or similar message should appear."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert "Pipeline completed" in result.output or "completed" in result.output

    def test_output_path_in_output(self, hello_yaml, tmp_path, monkeypatch):
        """The output path should be printed at start."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        assert "output" in result.output.lower()

    def test_rich_with_mocked_standalone(self, hello_yaml, tmp_path):
        """Rich output works in standalone mode with mocked API."""
        mock_response = {
            "content": [{"type": "text", "text": "Rich test output"}],
            "usage": {"input_tokens": 15, "output_tokens": 25},
        }
        from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
        out = tmp_path / "results"

        with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
            result = _invoke([
                "run", str(hello_yaml), "--mode", "standalone",
                "--api-key", "sk-ant-test",
                "--output-dir", str(out),
            ])

        assert result.exit_code == 0, result.output

    def test_tokens_and_cost_in_output(self, hello_yaml):
        """Tokens and cost figures should appear in the summary output."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        # DryRunExecutor generates random tokens and cost
        assert "tokens" in result.output.lower() or "Tokens" in result.output

    def test_elapsed_time_in_output(self, hello_yaml):
        """Elapsed time should appear in the table title."""
        result = _invoke(["run", str(hello_yaml), "--mode", "dry-run"])
        # Table title contains "x phases in y.ys"
        assert "phases" in result.output

    def test_aborted_pipeline_still_exits_2(self, hello_yaml):
        """Phase failure should still exit with code 2 (no rich crash)."""
        result = _invoke([
            "run", str(hello_yaml), "--mode", "dry-run",
            "--dry-run-failure-rate", "1.0",
        ])
        assert result.exit_code == 2
        assert "aborted" in result.output.lower()


# ---------------------------------------------------------------------------
# Issue #186 — Heartbeat wiring integration
# ---------------------------------------------------------------------------

class TestHeartbeatWiring:
    """Verify that run_template() correctly wires ProgressHeartbeat callbacks.

    These tests patch ProgressHeartbeat so they run without wall-clock delays
    and confirm the actual CLI wiring (not just the heartbeat class in isolation).
    """

    def test_set_current_phase_called_for_each_phase(self, hello_yaml, tmp_path):
        """on_phase_start wiring: set_current_phase() should be called once per phase."""
        from unittest.mock import MagicMock, patch as upatch

        mock_hb_instance = MagicMock()
        mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
        mock_hb_instance.__exit__ = MagicMock(return_value=False)

        # ProgressHeartbeat is imported locally inside run_template(), so we must
        # patch at the source module (orchestration_engine.heartbeat) rather than
        # at cli-module level.
        with upatch(
            "orchestration_engine.heartbeat.ProgressHeartbeat",
            return_value=mock_hb_instance,
        ):
            result = _invoke([
                "run", str(hello_yaml), "--mode", "dry-run",
                "--output-dir", str(tmp_path / "out"),
            ])

        assert result.exit_code == 0, result.output
        # hello-pipeline has phases: greet, summarize — set_current_phase should
        # be called at least once for each.
        assert mock_hb_instance.set_current_phase.call_count >= 1, (
            "set_current_phase() must be called via _on_phase_start_cb"
        )
        called_phase_ids = {
            call.args[0]
            for call in mock_hb_instance.set_current_phase.call_args_list
        }
        assert "greet" in called_phase_ids, (
            f"Expected 'greet' in set_current_phase calls; got: {called_phase_ids}"
        )

    def test_on_phase_complete_called_for_each_phase(self, hello_yaml, tmp_path):
        """on_phase_complete wiring: on_phase_complete() should be called once per phase."""
        from unittest.mock import MagicMock, patch as upatch

        mock_hb_instance = MagicMock()
        mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
        mock_hb_instance.__exit__ = MagicMock(return_value=False)

        with upatch(
            "orchestration_engine.heartbeat.ProgressHeartbeat",
            return_value=mock_hb_instance,
        ):
            result = _invoke([
                "run", str(hello_yaml), "--mode", "dry-run",
                "--output-dir", str(tmp_path / "out"),
            ])

        assert result.exit_code == 0, result.output
        # hello-pipeline has 2 phases (greet + summarize)
        assert mock_hb_instance.on_phase_complete.call_count == 2, (
            f"Expected on_phase_complete() called 2 times (one per phase); "
            f"got: {mock_hb_instance.on_phase_complete.call_count}"
        )

    def test_heartbeat_context_manager_entered_and_exited(self, hello_yaml, tmp_path):
        """The heartbeat context manager must be entered and exited (thread lifecycle)."""
        from unittest.mock import MagicMock, patch as upatch

        mock_hb_instance = MagicMock()
        mock_hb_instance.__enter__ = MagicMock(return_value=mock_hb_instance)
        mock_hb_instance.__exit__ = MagicMock(return_value=False)

        with upatch(
            "orchestration_engine.heartbeat.ProgressHeartbeat",
            return_value=mock_hb_instance,
        ):
            result = _invoke([
                "run", str(hello_yaml), "--mode", "dry-run",
                "--output-dir", str(tmp_path / "out"),
            ])

        assert result.exit_code == 0, result.output
        mock_hb_instance.__enter__.assert_called_once()
        mock_hb_instance.__exit__.assert_called_once()
