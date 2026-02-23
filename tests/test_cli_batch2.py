"""Tests for CLI batch-2 features: orch templates list, orch templates info.

Covers:
  - Feature #67: orch templates list
  - Feature #68: orch templates info <name|path>
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
TEMPLATES_DIR = REPO_ROOT / "templates"
HELLO_YAML = EXAMPLES_DIR / "hello-pipeline.yaml"
CONTENT_YAML = TEMPLATES_DIR / "content-pipeline.yaml"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, env=env or {}, catch_exceptions=False)


def _invoke_in(cwd, args, env=None):
    """Invoke CLI with cwd changed to *cwd*."""
    runner = CliRunner()
    # CliRunner doesn't support cwd directly; we rely on monkeypatch via fixture.
    # This helper is used when tests don't need a chdir.
    return runner.invoke(main, args, env=env or {}, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Feature #67 — orch templates list
# ---------------------------------------------------------------------------

class TestTemplatesList:
    """Tests for 'orch templates list'."""

    def test_list_finds_content_pipeline(self, monkeypatch):
        """templates list finds content-pipeline in ./templates/."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0, result.output
        assert "Content Pipeline MVP" in result.output

    def test_list_finds_hello_pipeline(self, monkeypatch):
        """templates list finds hello-pipeline in ./examples/."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0, result.output
        assert "Hello Pipeline" in result.output

    def test_list_shows_version(self, monkeypatch):
        """templates list shows version numbers."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0
        assert "2.1.0" in result.output  # content-pipeline version

    def test_list_shows_phase_count(self, monkeypatch):
        """templates list shows phase count."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0
        # content-pipeline has 5 phases, hello has 2
        assert "5" in result.output
        assert "2" in result.output

    def test_list_shows_source(self, monkeypatch):
        """templates list shows source directory labels."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0
        # Source label for ./templates/ is now "project" (consistent with TemplateEngine)
        assert "project" in result.output
        assert "examples" in result.output

    def test_list_json_is_valid_json(self, monkeypatch):
        """templates list --json produces valid JSON."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_list_json_has_expected_keys(self, monkeypatch):
        """templates list --json entries have all expected fields."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) >= 1
        for entry in data:
            assert "id" in entry
            assert "name" in entry
            assert "version" in entry
            assert "phases" in entry
            assert "description" in entry
            assert "source" in entry
            assert "path" in entry

    def test_list_json_contains_both_templates(self, monkeypatch):
        """templates list --json includes both templates."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list", "--json"])
        data = json.loads(result.output)
        names = [e["name"] for e in data]
        assert "Content Pipeline MVP" in names
        assert "Hello Pipeline" in names

    def test_list_json_phases_is_integer(self, monkeypatch):
        """templates list --json phases field is an integer."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list", "--json"])
        data = json.loads(result.output)
        for entry in data:
            assert isinstance(entry["phases"], int)

    def test_list_empty_directory_exits_zero(self, tmp_path, monkeypatch):
        """templates list exits 0 and prints helpful message when no templates exist."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0, result.output
        # Should print a helpful "no templates found" message
        assert "no templates" in result.output.lower() or "No templates" in result.output

    def test_list_empty_directory_shows_search_paths(self, tmp_path, monkeypatch):
        """templates list with no templates shows the search paths."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0
        # Should mention the search path directories
        assert "templates" in result.output or "examples" in result.output

    def test_list_json_empty_directory_is_empty_list(self, tmp_path, monkeypatch):
        """templates list --json with no templates returns []."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["templates", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_list_truncates_long_description(self, monkeypatch):
        """templates list truncates descriptions to 60 chars."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "list"])
        assert result.exit_code == 0
        # The full description of content-pipeline is >60 chars; it should be truncated.
        full_desc = (
            "Simplified content pipeline: Research → Write → Fact-Check → Apply Fixes → Final Output"
        )
        # Full desc should NOT appear verbatim (it's too long)
        assert full_desc not in result.output


# ---------------------------------------------------------------------------
# Feature #68 — orch templates info <name|path>
# ---------------------------------------------------------------------------

class TestTemplatesInfo:
    """Tests for 'orch templates info <name_or_path>'."""

    # ---- Path-based loading ----

    def test_info_path_exits_zero(self):
        """templates info <path> exits 0 for a valid template."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert result.exit_code == 0, result.output

    def test_info_path_shows_name(self):
        """templates info <path> shows the template name."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert result.exit_code == 0
        assert "Hello Pipeline" in result.output

    def test_info_path_shows_version(self):
        """templates info <path> shows version."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert "1.0.0" in result.output

    def test_info_path_shows_description(self):
        """templates info <path> shows full (non-truncated) description."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert "Greet" in result.output or "Summarize" in result.output

    def test_info_path_shows_phases_table(self):
        """templates info <path> shows a phases table with all phase IDs."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert result.exit_code == 0
        assert "greet" in result.output
        assert "summarize" in result.output

    def test_info_path_shows_execution_order(self):
        """templates info <path> shows Execution Order section."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert "Execution Order" in result.output
        assert "Wave 1" in result.output
        assert "Wave 2" in result.output

    def test_info_path_shows_example_command(self):
        """templates info <path> shows an example orch run command."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert "orch run" in result.output
        assert "dry-run" in result.output

    # ---- Content pipeline (config schema) ----

    def test_info_content_pipeline_shows_config_schema(self):
        """templates info shows Config Schema for templates with config_schema."""
        result = _invoke(["templates", "info", str(CONTENT_YAML)])
        assert result.exit_code == 0, result.output
        assert "Config Schema" in result.output

    def test_info_content_pipeline_shows_fields(self):
        """templates info shows config fields (brief, target_audience, etc.)."""
        result = _invoke(["templates", "info", str(CONTENT_YAML)])
        assert "brief" in result.output
        assert "target_audience" in result.output

    def test_info_content_pipeline_shows_phases_table(self):
        """templates info shows all 5 phase IDs for content-pipeline."""
        result = _invoke(["templates", "info", str(CONTENT_YAML)])
        for phase_id in ("research", "write", "fact_check", "apply_fixes", "final_output"):
            assert phase_id in result.output, (
                f"Phase '{phase_id}' not found in output:\n{result.output}"
            )

    def test_info_content_pipeline_execution_order_5_waves(self):
        """content-pipeline has 5 sequential phases → 5 waves."""
        result = _invoke(["templates", "info", str(CONTENT_YAML)])
        assert "Wave 5" in result.output

    def test_info_content_pipeline_shows_model_tiers(self):
        """templates info shows model tiers in phases table."""
        result = _invoke(["templates", "info", str(CONTENT_YAML)])
        assert "sonnet" in result.output

    # ---- Name-based lookup ----

    def test_info_name_lookup_by_id(self, monkeypatch):
        """templates info finds template by ID (e.g. 'content-pipeline-mvp')."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "info", "content-pipeline-mvp"])
        assert result.exit_code == 0, result.output
        assert "Content Pipeline MVP" in result.output

    def test_info_name_lookup_by_name(self, monkeypatch):
        """templates info finds template by name (case-insensitive)."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "info", "Hello Pipeline"])
        assert result.exit_code == 0, result.output
        assert "Hello Pipeline" in result.output

    def test_info_name_lookup_by_name_case_insensitive(self, monkeypatch):
        """templates info name lookup is case-insensitive."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["templates", "info", "hello pipeline"])
        assert result.exit_code == 0, result.output
        assert "Hello Pipeline" in result.output

    # ---- Error handling ----

    def test_info_nonexistent_exits_nonzero(self, tmp_path, monkeypatch):
        """templates info with nonexistent name exits with non-zero code."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["templates", "info", "nonexistent-template-xyz"])
        assert result.exit_code != 0

    def test_info_nonexistent_shows_error_message(self, tmp_path, monkeypatch):
        """templates info with nonexistent name shows a helpful error."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["templates", "info", "nonexistent-template-xyz"])
        # Error should be in output or stderr
        combined = result.output + (result.output or "")
        assert "not found" in combined.lower() or "nonexistent" in combined.lower()

    def test_info_suggests_similar_names(self, monkeypatch):
        """templates info suggests similar templates on partial match.

        "content-pipeline" is a substring of the real template id
        "content-pipeline-mvp", so it should appear in suggestions.
        """
        monkeypatch.chdir(REPO_ROOT)
        # "content-pipeline" doesn't exactly match "content-pipeline-mvp"
        # but IS a substring of it → should show as suggestion
        result = _invoke(["templates", "info", "content-pipeline"])
        # Should suggest "Content Pipeline MVP"
        assert result.exit_code != 0
        assert "Content Pipeline MVP" in result.output or "content-pipeline-mvp" in result.output

    def test_info_missing_file_path_exits_nonzero(self):
        """templates info with a non-existent .yaml path exits with error."""
        result = _invoke(["templates", "info", "/nonexistent/path/to/template.yaml"])
        assert result.exit_code != 0

    # ---- Hello Pipeline display (thinking_level: off → YAML bool) ----

    def test_info_hello_thinking_level_displayed(self):
        """templates info renders thinking_level 'off' (a YAML boolean False) correctly."""
        result = _invoke(["templates", "info", str(HELLO_YAML)])
        assert result.exit_code == 0, result.output
        # Should show "off" not "False"
        assert "off" in result.output
        assert "False" not in result.output
