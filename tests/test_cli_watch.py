"""Tests for orch watch command (#414)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.db import Database


@pytest.fixture
def db():
    """In-memory database with a pipeline run."""
    from tests._helpers import pipeline_run_dict
    _db = Database(db_path=Path(":memory:"))
    _db.insert_pipeline_run(pipeline_run_dict(
        "watch-test-1",
        template_path="templates/coding-pipeline-v1.yaml",
        template_id="coding-pipeline-v1",
        mode="openclaw",
        output_dir="/tmp/watch-test",
        status="pending_review",
    ))
    return _db


class TestWatchPipelineRun:
    """Test _watch_pipeline_run helper."""

    def test_completed_run_prints_summary(self, db):
        """A terminal-state run prints status and exits immediately."""
        from orchestration_engine.cli import _watch_pipeline_run

        runner = CliRunner()
        # Capture output by calling the function directly
        # (it uses click.echo internally)
        with runner.isolated_filesystem():
            # The run is already in pending_review state
            _watch_pipeline_run("watch-test-1", db, json_mode=False, refresh=1)
            # Should not hang — returns immediately for terminal states

    def test_completed_run_json_mode(self, db):
        """JSON mode outputs structured data."""
        from orchestration_engine.cli import _watch_pipeline_run

        runner = CliRunner()
        with runner.isolated_filesystem():
            _watch_pipeline_run("watch-test-1", db, json_mode=True, refresh=1)

    def test_nonexistent_run_exits(self):
        """Non-existent run ID exits with error."""
        from orchestration_engine.cli import _watch_pipeline_run

        _db = Database(db_path=Path(":memory:"))
        with pytest.raises(SystemExit):
            _watch_pipeline_run("nonexistent", _db, json_mode=False, refresh=1)


class TestPrintWatchEvent:
    """Test _print_watch_event formatting."""

    def test_phase_started_event(self, capsys):
        """Phase started events show ▶ icon."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "phase_started",
            "phase_id": "spec",
            "tokens_consumed": None,
            "state": None,
            "metadata_json": "{}",
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "▶" in output
        assert "spec" in output

    def test_phase_completed_event(self, capsys):
        """Phase completed events show ✓ icon and tokens."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "phase_completed",
            "phase_id": "implement",
            "tokens_consumed": 42000,
            "state": "success",
            "metadata_json": "{}",
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "✓" in output
        assert "implement" in output
        assert "42,000" in output

    def test_stall_event(self, capsys):
        """Stall events show warning with message."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "stall_detected",
            "phase_id": "review",
            "tokens_consumed": None,
            "state": None,
            "metadata_json": json.dumps({
                "message": "No token progress for 90s",
            }),
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "No token progress for" in output

    def test_json_mode_output(self, capsys):
        """JSON mode outputs valid JSON."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "phase_started",
            "phase_id": "spec",
            "tokens_consumed": None,
            "state": None,
            "metadata_json": "{}",
        }, json_mode=True)

        output = capsys.readouterr().out.strip()
        parsed = json.loads(output)
        assert parsed["type"] == "phase_started"
        assert parsed["phase"] == "spec"

    def test_unknown_event_type(self, capsys):
        """Unknown event types are displayed generically."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "custom_event",
            "phase_id": "test",
            "tokens_consumed": None,
            "state": None,
            "metadata_json": "{}",
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "custom_event" in output

    def test_status_changed_event(self, capsys):
        """Status changed events show transition."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "status_changed",
            "phase_id": None,
            "tokens_consumed": None,
            "state": "running",
            "metadata_json": json.dumps({"new_status": "running"}),
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "running" in output

    def test_missing_metadata(self, capsys):
        """Events with missing/null metadata don't crash."""
        from orchestration_engine.cli import _print_watch_event

        _print_watch_event({
            "event_type": "phase_started",
            "phase_id": "spec",
            "tokens_consumed": None,
            "state": None,
            "metadata_json": None,
        }, json_mode=False)

        output = capsys.readouterr().out
        assert "spec" in output


class TestWatchWithEvents:
    """Test watch with SSE events in the database."""

    def test_events_displayed_before_completion(self):
        """Events are fetched and displayed for completed runs."""
        from tests._helpers import pipeline_run_dict
        _db = Database(db_path=Path(":memory:"))
        _db.insert_pipeline_run(pipeline_run_dict(
            "evt-run",
            template_path="t.yaml",
            template_id="test",
            mode="openclaw",
            output_dir="/tmp/test",
            status="success",
            scoring_status="passed",
            scoring_score=1.0,
        ))

        _db.insert_pipeline_run_event(
            run_id="evt-run",
            event_type="phase_started",
            phase_id="spec",
        )
        _db.insert_pipeline_run_event(
            run_id="evt-run",
            event_type="phase_completed",
            phase_id="spec",
            tokens_consumed=15000,
            state="success",
        )

        from orchestration_engine.cli import _watch_pipeline_run
        # Should print events then exit (success is terminal)
        _watch_pipeline_run("evt-run", _db, json_mode=False, refresh=1)
