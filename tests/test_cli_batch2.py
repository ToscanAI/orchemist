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
        assert "templates" in result.output
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

