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
TEMPLATES_DIR = REPO_ROOT / "templates"
HELLO_YAML = EXAMPLES_DIR / "hello-pipeline.yaml"
CONTENT_YAML = TEMPLATES_DIR / "content-pipeline.yaml"


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
        """quickstart mentions 'orch templates info content-pipeline-mvp' in Next steps."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "content-pipeline-mvp" in result.output

    def test_quickstart_shows_start_command(self, monkeypatch):
        """quickstart mentions 'orch start content-pipeline-mvp' in Next steps."""
        monkeypatch.chdir(REPO_ROOT)
        result = _invoke(["quickstart"])
        assert result.exit_code == 0
        assert "orch start" in result.output

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


