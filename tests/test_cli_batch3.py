"""Tests for CLI batch-3 features: orch quickstart, orch start.

Covers:
  - Feature #65: orch quickstart
  - Feature #66: orch start (interactive wizard)
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
TEMPLATES_DIR = REPO_ROOT.joinpath("templates")
HELLO_YAML = EXAMPLES_DIR / "hello-pipeline.yaml"
CONTENT_YAML = TEMPLATES_DIR / "content-pipeline-v28.yaml"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _invoke(args, input=None, env=None, catch_exceptions=False):
    """Invoke the CLI, mixing stderr into result.output (same as other test batches)."""
    runner = CliRunner()
    return runner.invoke(
        main,
        args,
        input=input,
        env=env or {},
        catch_exceptions=catch_exceptions,
    )


def _invoke_in(cwd, args, input=None, env=None):
    """Invoke CLI from a specific working directory via monkeypatch."""
    runner = CliRunner()
    return runner.invoke(
        main, args, input=input, env=env or {}, catch_exceptions=False
    )


# ---------------------------------------------------------------------------
# Feature #65 — orch quickstart
# ---------------------------------------------------------------------------

class TestQuickstart:
    """Tests for 'orch quickstart'."""

    def test_quickstart_exits_zero(self, monkeypatch, tmp_path):
        """quickstart exits with code 0."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0, result.output

    def test_quickstart_shows_header(self, monkeypatch):
        """quickstart shows a Quick Start header."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "Quick Start" in result.output

    def test_quickstart_shows_next_steps(self, monkeypatch):
        """quickstart shows a 'Next steps' section."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "Next steps" in result.output

    def test_quickstart_shows_templates_list_command(self, monkeypatch):
        """quickstart mentions 'orch templates list' in Next steps."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "orch templates list" in result.output

    def test_quickstart_shows_templates_info_command(self, monkeypatch):
        """quickstart mentions 'orch templates info hello-pipeline' in Next steps."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "hello-pipeline" in result.output

    def test_quickstart_shows_run_command(self, monkeypatch):
        """quickstart mentions 'orch run hello-pipeline.yaml' in Next steps."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "orch run" in result.output

    def test_quickstart_runs_dry_run(self, monkeypatch):
        """quickstart runs the pipeline in dry-run mode (shows mode=dry-run)."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "dry-run" in result.output

    def test_quickstart_shows_hello_pipeline(self, monkeypatch):
        """quickstart executes hello-pipeline (name appears in output)."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "Hello Pipeline" in result.output

    def test_quickstart_shows_phase_count(self, monkeypatch):
        """quickstart mentions the number of phases run."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        # hello-pipeline has 2 phases → "2-phase" in footer
        assert "2" in result.output

    def test_quickstart_shows_success_message(self, monkeypatch):
        """quickstart shows a success/completion message after the pipeline."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        # "That's it!" or similar success indicator in footer
        assert "That's it!" in result.output or "✓" in result.output

    def test_quickstart_shows_pipeline_completed(self, monkeypatch):
        """quickstart shows 'Pipeline completed' from the run output."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower()

    def test_quickstart_shows_both_phases(self, monkeypatch):
        """quickstart output contains both phase IDs: greet and summarize."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "greet" in result.output
        assert "summarize" in result.output

    def test_quickstart_completes_quickly(self, monkeypatch):
        """quickstart should complete well within 10 seconds in dry-run mode."""
        import time
        monkeypatch.chdir(REPO_ROOT)
        start = time.time()
        result = _invoke(["quickstart"])
        elapsed = time.time() - start
        assert result.exit_code == 0
        assert elapsed < 10, f"quickstart took {elapsed:.1f}s — too slow"

    def test_quickstart_no_arguments_needed(self, monkeypatch):
        """quickstart requires no arguments."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Feature #66 — orch start (interactive wizard)
# ---------------------------------------------------------------------------

class TestStartWithPath:
    """Tests for 'orch start <path>' (file path mode)."""

    def test_start_valid_path_exits_zero(self):
        """start with a valid YAML path in dry-run mode exits 0."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output

    def test_start_runs_pipeline(self):
        """start actually executes the pipeline (mode + pipeline name in output)."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "dry-run" in result.output

    def test_start_shows_template_header(self):
        """start shows the template name before the wizard."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "Hello Pipeline" in result.output

    def test_start_shows_template_version(self):
        """start shows the template version."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "1.0.0" in result.output

    def test_start_shows_description(self):
        """start shows the template description."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "Greet" in result.output or "Summarize" in result.output

    def test_start_content_pipeline_path(self):
        """start works with content-pipeline.yaml path in dry-run + --yes."""
        result = _invoke(
            ["start", str(CONTENT_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output

    def test_start_nonexistent_file_exits_nonzero(self):
        """start with a nonexistent .yaml path exits with non-zero code."""
        result = _invoke(
            ["start", "/nonexistent/path/template.yaml", "--mode", "dry-run"],
        )
        assert result.exit_code != 0

    def test_start_nonexistent_file_shows_error(self):
        """start with a nonexistent .yaml path shows an error message."""
        result = _invoke(
            ["start", "/nonexistent/path/template.yaml", "--mode", "dry-run"],
        )
        combined = result.output + (result.output or "")
        assert "not found" in combined.lower() or "✗" in combined


class TestStartWithName:
    """Tests for 'orch start <name>' (template name/ID lookup mode)."""

    def test_start_by_id_exits_zero(self, monkeypatch):
        """start content-pipeline by ID exits 0."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output

    def test_start_by_id_shows_template_name(self, monkeypatch):
        """start by ID shows the template's display name."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "Content Pipeline v2.8" in result.output

    def test_start_by_name_hello_pipeline(self, monkeypatch):
        """start by display name 'hello-pipeline' works."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "hello-pipeline", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Hello Pipeline" in result.output

    def test_start_nonexistent_name_exits_nonzero(self, tmp_path, monkeypatch):
        """start with a nonexistent template name exits non-zero."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(
            ["start", "this-template-does-not-exist", "--mode", "dry-run"],
        )
        assert result.exit_code != 0

    def test_start_nonexistent_name_shows_not_found(self, tmp_path, monkeypatch):
        """start with a nonexistent template name shows error message."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(
            ["start", "this-template-does-not-exist", "--mode", "dry-run"],
        )
        combined = result.output + (result.output or "")
        assert "not found" in combined.lower() or "✗" in combined

    def test_start_suggests_similar_names(self, monkeypatch):
        """start suggests similar template names on partial match."""
        monkeypatch.chdir(REPO_ROOT)
        # "content-pipe" is a substring of "content-pipeline" → should suggest it
        result = _invoke(
            ["start", "content-pipe", "--mode", "dry-run"],
        )
        assert result.exit_code != 0
        assert "Content Pipeline v2.8" in result.output or "content-pipeline" in result.output


class TestStartYesFlag:
    """Tests for 'orch start --yes' (non-interactive mode)."""

    def test_yes_flag_skips_prompts(self, monkeypatch):
        """--yes flag skips all prompts and runs immediately."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # Should NOT show the "Fill in the pipeline inputs:" prompt
        assert "Fill in" not in result.output

    def test_yes_flag_with_hello_pipeline(self):
        """--yes works with hello-pipeline which has no config_schema."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output

    def test_yes_flag_still_runs_pipeline(self, monkeypatch):
        """--yes still runs the pipeline end-to-end."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "Pipeline" in result.output
        assert "completed" in result.output.lower()

    def test_yes_flag_no_confirmation_prompt(self, monkeypatch):
        """--yes skips the 'Proceed?' confirmation."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0
        assert "Proceed?" not in result.output


class TestStartInteractiveWizard:
    """Tests for 'orch start' interactive wizard prompts."""

    def test_wizard_prompts_for_config_fields(self, monkeypatch):
        """Wizard shows field names from config_schema.properties."""
        monkeypatch.chdir(REPO_ROOT)
        # Provide inputs: topic, author_name, author_facts, voice_style, source_material, 4 optional blanks, confirm
        user_input = "AI topic\nauthor name\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        # Field labels should appear in output
        assert "topic" in result.output

    def test_wizard_shows_field_descriptions(self, monkeypatch):
        """Wizard shows field descriptions from config_schema."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "AI topic\nauthor name\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        # Description from schema: "What to write about"
        assert "What to write about" in result.output

    def test_wizard_shows_fill_in_inputs_header(self, monkeypatch):
        """Wizard shows 'Fill in the pipeline inputs:' header."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "AI topic\nThe Author\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        assert "Fill in" in result.output

    def test_wizard_shows_summary(self, monkeypatch):
        """Wizard shows a summary of collected inputs before confirmation."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "My article topic\nThe Author\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        assert "Summary" in result.output
        assert "My article topic" in result.output

    def test_wizard_shows_proceed_confirmation(self, monkeypatch):
        """Wizard shows a 'Proceed?' confirmation prompt."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "My article topic\nThe Author\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        assert "Proceed" in result.output

    def test_wizard_abort_on_no(self, monkeypatch):
        """Wizard aborts when user answers 'n' at confirmation."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "My article topic\nThe Author\nauthor facts\nvoice style\nsource material\n\n\n\n\nn\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0
        assert "Aborted" in result.output or "aborted" in result.output.lower()
        # Pipeline should NOT have run
        assert "Pipeline:" not in result.output

    def test_wizard_runs_pipeline_after_confirm(self, monkeypatch):
        """Wizard runs the pipeline after user confirms."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "AI orchestration\nRené Rivera\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        assert "Pipeline:" in result.output
        assert "completed" in result.output.lower()

    def test_wizard_shows_all_five_fields(self, monkeypatch):
        """Wizard shows all 5 required fields from content-pipeline-v28 config_schema."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "topic value\nauthor name\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        for field in ("topic", "author_name", "author_facts", "voice_style", "source_material"):
            assert field in result.output, f"Expected field '{field}' in wizard output"

    def test_wizard_collected_input_in_summary(self, monkeypatch):
        """Wizard summary shows the actual collected values."""
        monkeypatch.chdir(REPO_ROOT)
        user_input = "UNIQUE_BRIEF_VALUE\nUnique Author\nauthor facts\nvoice style\nsource material\n\n\n\n\ny\n"
        result = _invoke(
            ["start", "content-pipeline-v28", "--mode", "dry-run"],
            input=user_input,
        )
        assert result.exit_code == 0, result.output
        assert "UNIQUE_BRIEF_VALUE" in result.output


class TestStartModeFlag:
    """Tests for 'orch start --mode' option."""

    def test_mode_dry_run_is_default(self):
        """Default mode is dry-run (safe, no API key needed)."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output

    def test_mode_dry_run_explicit(self):
        """Explicit --mode dry-run works."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output

    def test_mode_appears_in_pipeline_output(self, monkeypatch):
        """The selected mode appears in the pipeline execution output."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(
            ["start", "hello-pipeline", "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # run_template prints "Mode: dry-run"
        assert "dry-run" in result.output

    def test_mode_invalid_shows_error(self):
        """Invalid mode value shows error from click."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "invalid-mode", "--yes"],
        )
        assert result.exit_code != 0

    def test_output_dir_option_works(self, tmp_path):
        """--output-dir creates output in specified directory."""
        out = tmp_path / "wizard-results"
        result = _invoke(
            [
                "start", str(HELLO_YAML),
                "--mode", "dry-run",
                "--yes",
                "--output-dir", str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists(), "Output dir should be created"
        assert any(out.iterdir()), "Output dir should contain files"


class TestStartHelloPipelineNoSchema:
    """Tests for start with hello-pipeline (no config_schema)."""

    def test_no_schema_no_prompts(self):
        """Template with no config_schema shows 'no configurable inputs' message."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run"],
            input="y\n",  # just in case there's a confirm prompt
        )
        assert result.exit_code == 0, result.output
        assert "no configurable inputs" in result.output.lower() or "Fill in" not in result.output

    def test_no_schema_still_runs(self):
        """Template with no config_schema still runs the pipeline."""
        result = _invoke(
            ["start", str(HELLO_YAML), "--mode", "dry-run", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Pipeline:" in result.output
