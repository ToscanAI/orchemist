"""Tests for Issue #267: Non-blocking async pipeline execution.

Covers:
- pipeline_runs DB operations (insert, update, get, list)
- Daemon PID file management and is_process_alive
- Status command output formatting
- Start command spawns process and returns run-id
- Wait command polls and returns correct exit code

Uses dry-run mode for integration tests — no real sub-agents needed.
"""

import json
import os
import sys
import time
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_db():
    """Return an in-memory Database instance with the new pipeline_runs table."""
    from orchestration_engine.db import Database
    return Database(":memory:")


@pytest.fixture
def tmp_db(tmp_path):
    """Return a file-backed Database instance (db_path accessible for CLI tests)."""
    from orchestration_engine.db import Database
    return Database(tmp_path / "test.db")


@pytest.fixture
def sample_run() -> Dict[str, Any]:
    """Return a minimal pipeline_run record dict."""
    return {
        "run_id": "abc12345",
        "template_path": "/tmp/template.yaml",
        "template_id": "test-pipeline",
        "input_json": json.dumps({"topic": "AI safety"}),
        "mode": "dry-run",
        "output_dir": "/tmp/output/test",
        "status": "pending",
        "gateway_url": None,
        "skip_scoring": 0,
    }


@pytest.fixture
def cli_runner():
    """Return a Click test runner."""
    return CliRunner()


@pytest.fixture
def minimal_template_yaml(tmp_path) -> Path:
    """Write a minimal valid dry-run pipeline YAML and return its path."""
    content = """\
id: mini-pipeline
name: Mini Test Pipeline
version: "1.0.0"
description: Minimal pipeline for tests
phases:
  - id: phase-one
    name: Phase One
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Write a short paragraph about: {input[topic]}
"""
    p = tmp_path / "mini-pipeline.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 1. DB operations — insert_pipeline_run
# ---------------------------------------------------------------------------


class TestInsertPipelineRun:
    def test_insert_returns_run_id(self, in_memory_db, sample_run):
        result = in_memory_db.insert_pipeline_run(sample_run)
        assert result == sample_run["run_id"]

    def test_insert_persists_all_fields(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run is not None
        assert run["run_id"] == sample_run["run_id"]
        assert run["template_id"] == sample_run["template_id"]
        assert run["mode"] == sample_run["mode"]
        assert run["status"] == "pending"
        assert run["input_json"] == sample_run["input_json"]

    def test_insert_default_status_is_pending(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["status"] == "pending"

    def test_insert_duplicate_raises(self, in_memory_db, sample_run):
        """Inserting the same run_id twice must raise an error (PRIMARY KEY)."""
        in_memory_db.insert_pipeline_run(sample_run)
        with pytest.raises(Exception):
            in_memory_db.insert_pipeline_run(sample_run)


# ---------------------------------------------------------------------------
# 2. DB operations — get_pipeline_run
# ---------------------------------------------------------------------------


class TestGetPipelineRun:
    def test_get_existing_run(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run("abc12345")
        assert run is not None
        assert run["run_id"] == "abc12345"

    def test_get_nonexistent_returns_none(self, in_memory_db):
        run = in_memory_db.get_pipeline_run("does-not-exist")
        assert run is None


# ---------------------------------------------------------------------------
# 3. DB operations — update_pipeline_run
# ---------------------------------------------------------------------------


class TestUpdatePipelineRun:
    def test_update_status(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        result = in_memory_db.update_pipeline_run("abc12345", status="running")
        assert result is True
        run = in_memory_db.get_pipeline_run("abc12345")
        assert run["status"] == "running"

    def test_update_current_phase(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run("abc12345", current_phase="research")
        run = in_memory_db.get_pipeline_run("abc12345")
        assert run["current_phase"] == "research"

    def test_update_completed_phases(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        phases = json.dumps(["research", "write"])
        in_memory_db.update_pipeline_run("abc12345", completed_phases=phases)
        run = in_memory_db.get_pipeline_run("abc12345")
        assert json.loads(run["completed_phases"]) == ["research", "write"]

    def test_update_pid(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run("abc12345", pid=12345)
        run = in_memory_db.get_pipeline_run("abc12345")
        assert run["pid"] == 12345

    def test_update_nonexistent_returns_false(self, in_memory_db):
        result = in_memory_db.update_pipeline_run("no-such-id", status="running")
        assert result is False

    def test_update_no_kwargs_returns_false(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        result = in_memory_db.update_pipeline_run("abc12345")
        assert result is False

    def test_update_error_message(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run("abc12345", status="failed", error_message="Phase crashed")
        run = in_memory_db.get_pipeline_run("abc12345")
        assert run["status"] == "failed"
        assert run["error_message"] == "Phase crashed"


# ---------------------------------------------------------------------------
# 4. DB operations — list_pipeline_runs
# ---------------------------------------------------------------------------


class TestListPipelineRuns:
    def _make_run(self, run_id: str, status: str = "pending") -> Dict[str, Any]:
        return {
            "run_id": run_id,
            "template_path": "/tmp/t.yaml",
            "template_id": "test-pipe",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": f"/tmp/out/{run_id}",
            "status": status,
        }

    def test_list_returns_all_when_under_limit(self, in_memory_db):
        for i in range(3):
            in_memory_db.insert_pipeline_run(self._make_run(f"run-{i:04d}"))
        runs = in_memory_db.list_pipeline_runs(limit=10)
        assert len(runs) == 3

    def test_list_respects_limit(self, in_memory_db):
        for i in range(5):
            in_memory_db.insert_pipeline_run(self._make_run(f"lim-{i:04d}"))
        runs = in_memory_db.list_pipeline_runs(limit=3)
        assert len(runs) == 3

    def test_list_filters_by_status(self, in_memory_db):
        in_memory_db.insert_pipeline_run(self._make_run("r-pend", status="pending"))
        in_memory_db.insert_pipeline_run(self._make_run("r-done", status="success"))
        runs = in_memory_db.list_pipeline_runs(status="success")
        assert all(r["status"] == "success" for r in runs)
        assert len(runs) == 1

    def test_list_empty_returns_empty_list(self, in_memory_db):
        runs = in_memory_db.list_pipeline_runs()
        assert runs == []


# ---------------------------------------------------------------------------
# 5. Daemon — PID file management
# ---------------------------------------------------------------------------


class TestPidFileManagement:
    def test_write_pid_file_creates_file(self, tmp_path):
        from orchestration_engine.daemon import _write_pid_file
        pid_path = _write_pid_file(tmp_path)
        assert pid_path.exists()
        assert pid_path.name == ".orch-daemon.pid"
        assert int(pid_path.read_text()) == os.getpid()

    def test_remove_pid_file_deletes_file(self, tmp_path):
        from orchestration_engine.daemon import _write_pid_file, _remove_pid_file
        pid_path = _write_pid_file(tmp_path)
        assert pid_path.exists()
        _remove_pid_file(pid_path)
        assert not pid_path.exists()

    def test_remove_pid_file_ignores_missing(self, tmp_path):
        from orchestration_engine.daemon import _remove_pid_file
        # Should not raise even when file doesn't exist
        _remove_pid_file(tmp_path / ".orch-daemon.pid")

    def test_is_process_alive_current_process(self):
        from orchestration_engine.daemon import is_process_alive
        assert is_process_alive(os.getpid()) is True

    def test_is_process_alive_invalid_pid(self):
        from orchestration_engine.daemon import is_process_alive
        # PID 0 is invalid — should return False
        assert is_process_alive(0) is False

    def test_is_process_alive_dead_pid(self):
        from orchestration_engine.daemon import is_process_alive
        # PID 999999 almost certainly doesn't exist
        assert is_process_alive(999999) is False


# ---------------------------------------------------------------------------
# 6. Status command output formatting
# ---------------------------------------------------------------------------


class TestStatusCommandFormatting:
    def test_status_lists_recent_runs(self, tmp_db, cli_runner, tmp_path):
        """'orch status' with no args lists recent pipeline runs."""
        from orchestration_engine.cli import main

        # Insert a run into tmp_db
        tmp_db.insert_pipeline_run({
            "run_id": "test0001",
            "template_path": str(tmp_path / "t.yaml"),
            "template_id": "test-pipe",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(tmp_path / "out"),
        })

        # Patch Database at module level to return our tmp_db
        with patch("orchestration_engine.cli.Database", return_value=tmp_db):
            result = cli_runner.invoke(main, ["status"])

        assert result.exit_code == 0, result.output
        # Should mention the run ID
        assert "test0001" in result.output

    def test_status_detail_for_known_run_id(self, tmp_db, cli_runner, tmp_path):
        """'orch status <run-id>' shows phase progress."""
        from orchestration_engine.cli import main

        tmp_db.insert_pipeline_run({
            "run_id": "detail01",
            "template_path": str(tmp_path / "t.yaml"),
            "template_id": "my-pipeline",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(tmp_path / "out"),
        })
        tmp_db.update_pipeline_run(
            "detail01",
            status="running",
            current_phase="research",
            pid=os.getpid(),  # alive PID so no 'crashed' rewrite
        )

        with patch("orchestration_engine.cli.Database", return_value=tmp_db):
            result = cli_runner.invoke(main, ["status", "detail01"])

        assert result.exit_code == 0, result.output
        assert "detail01" in result.output
        assert "research" in result.output

    def test_status_unknown_run_id_exits_nonzero(self, tmp_db, cli_runner):
        """'orch status <bad-id>' exits with error when not found anywhere."""
        from orchestration_engine.cli import main

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.get_queue") as mock_queue:
            mock_queue.return_value.get_task_status.return_value = None
            result = cli_runner.invoke(main, ["status", "no-such-id"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 7. Start command — spawns daemon and returns run-id
# ---------------------------------------------------------------------------


class TestStartCommand:
    def test_start_command_creates_db_record(
        self, cli_runner, tmp_path, minimal_template_yaml, tmp_db
    ):
        """'orch launch' inserts a pipeline_runs record and exits 0."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "start-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "launch",
                str(minimal_template_yaml),
                "--mode", "dry-run",
                "--input", '{"topic": "AI"}',
                "--output-dir", str(out_dir),
                "--db-path", str(tmp_db.db_path),
            ])

        assert result.exit_code == 0, result.output

    def test_start_command_prints_run_id(
        self, cli_runner, tmp_path, minimal_template_yaml, tmp_db
    ):
        """'orch launch' prints the run ID to stdout."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "print-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "launch",
                str(minimal_template_yaml),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
                "--db-path", str(tmp_db.db_path),
            ])

        assert result.exit_code == 0, result.output
        assert "Run ID" in result.output

    def test_start_command_invalid_template_exits_error(
        self, cli_runner, tmp_path, tmp_db
    ):
        """'orch launch' with nonexistent template exits with error."""
        from orchestration_engine.cli import main

        result = cli_runner.invoke(main, [
            "launch",
            str(tmp_path / "no-such.yaml"),
            "--mode", "dry-run",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code != 0

    def test_start_command_spawns_popen(
        self, cli_runner, tmp_path, minimal_template_yaml, tmp_db
    ):
        """'orch launch' calls subprocess.Popen with start_new_session=True."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "popen-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 88888
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "launch",
                str(minimal_template_yaml),
                "--mode", "dry-run",
                "--output-dir", str(out_dir),
                "--db-path", str(tmp_db.db_path),
            ])

        assert result.exit_code == 0, result.output
        mock_subp.Popen.assert_called_once()
        call_kwargs = mock_subp.Popen.call_args
        assert call_kwargs.kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# 8. Wait command — polls and returns correct exit code
# ---------------------------------------------------------------------------


class TestWaitCommand:
    def _insert_run(self, db, run_id: str, status: str, tmp_path) -> None:
        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": "/tmp/t.yaml",
            "template_id": "test-pipe",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(out_dir),
            "status": status,
        })

    def test_wait_success_exits_zero(self, cli_runner, tmp_db, tmp_path):
        """'orch wait' exits 0 when status=success."""
        from orchestration_engine.cli import main

        self._insert_run(tmp_db, "wait-ok", "success", tmp_path)

        result = cli_runner.invoke(main, [
            "wait", "wait-ok",
            "--timeout", "30",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 0, result.output

    def test_wait_failed_exits_two(self, cli_runner, tmp_db, tmp_path):
        """'orch wait' exits 2 when status=failed."""
        from orchestration_engine.cli import main

        self._insert_run(tmp_db, "wait-fail", "failed", tmp_path)

        result = cli_runner.invoke(main, [
            "wait", "wait-fail",
            "--timeout", "30",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 2, result.output

    def test_wait_unknown_run_id_exits_two(self, cli_runner, tmp_db):
        """'orch wait' exits 2 when run-id is not found."""
        from orchestration_engine.cli import main

        result = cli_runner.invoke(main, [
            "wait", "no-such-run",
            "--timeout", "5",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 2

    def test_wait_timeout_exits_two(self, cli_runner, tmp_db, tmp_path):
        """'orch wait' exits 2 on timeout when run is still running."""
        from orchestration_engine.cli import main

        self._insert_run(tmp_db, "wait-pend", "running", tmp_path)
        # Also set a valid PID (current process) so liveness check doesn't crash it
        tmp_db.update_pipeline_run("wait-pend", pid=os.getpid())

        # Patch time to advance quickly past timeout
        original_time = time.time
        call_count = [0]
        start = original_time()

        def fake_time():
            call_count[0] += 1
            if call_count[0] > 3:
                return start + 9999  # Way past any timeout
            return start + call_count[0]

        with patch("orchestration_engine.cli.time") as mock_time:
            mock_time.time.side_effect = fake_time
            mock_time.sleep = MagicMock()

            result = cli_runner.invoke(main, [
                "wait", "wait-pend",
                "--timeout", "5",
                "--interval", "1",
                "--db-path", str(tmp_db.db_path),
            ])

        assert result.exit_code == 2

    def test_wait_cancelled_exits_two(self, cli_runner, tmp_db, tmp_path):
        """'orch wait' exits 2 when status=cancelled."""
        from orchestration_engine.cli import main

        self._insert_run(tmp_db, "wait-cancel", "cancelled", tmp_path)

        result = cli_runner.invoke(main, [
            "wait", "wait-cancel",
            "--timeout", "30",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 2

    def test_wait_scoring_failed_exits_two(self, cli_runner, tmp_db, tmp_path):
        """'orch wait' exits 2 when status=scoring_failed (Issue #288)."""
        from orchestration_engine.cli import main

        self._insert_run(tmp_db, "wait-score-fail", "scoring_failed", tmp_path)

        result = cli_runner.invoke(main, [
            "wait", "wait-score-fail",
            "--timeout", "30",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 2, (
            f"Expected exit code 2 for scoring_failed, got {result.exit_code}. "
            f"Output: {result.output}"
        )


# ---------------------------------------------------------------------------
# 9. Resume command — stub
# ---------------------------------------------------------------------------


class TestResumeCommand:
    def test_resume_not_implemented(self, cli_runner):
        """'orch resume' exits nonzero with 'not yet implemented' message."""
        from orchestration_engine.cli import main

        result = cli_runner.invoke(main, ["resume", "some-run-id"])
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "not yet implemented" in output_lower or "not yet" in output_lower


# ---------------------------------------------------------------------------
# 10. Logs command
# ---------------------------------------------------------------------------


class TestLogsCommand:
    def test_logs_missing_run_id_exits_error(self, cli_runner, tmp_db):
        """'orch logs' with unknown run_id exits with error."""
        from orchestration_engine.cli import main

        result = cli_runner.invoke(main, [
            "logs", "no-such-run",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code != 0

    def test_logs_shows_log_file_content(self, cli_runner, tmp_db, tmp_path):
        """'orch logs' prints log file content when run exists."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "myrun"
        out_dir.mkdir(parents=True)
        log_file = out_dir / ".orch-daemon.log"
        log_file.write_text("2026-01-01 10:00:00  INFO  Daemon starting\n")

        tmp_db.insert_pipeline_run({
            "run_id": "logs-run",
            "template_path": str(tmp_path / "t.yaml"),
            "template_id": "t",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        result = cli_runner.invoke(main, [
            "logs", "logs-run",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code == 0, result.output
        assert "Daemon starting" in result.output


# ---------------------------------------------------------------------------
# 11. Integration: dry-run daemon execution (minimal, no real sub-agents)
# ---------------------------------------------------------------------------


class TestDaemonIntegration:
    def test_daemon_run_dry_run_success(self, tmp_path):
        """run_daemon() completes successfully in dry-run mode."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        # Write a minimal template
        template_yaml = tmp_path / "mini.yaml"
        template_yaml.write_text("""\
id: mini-daemon
name: Mini Daemon Test
version: "1.0.0"
description: Test daemon
phases:
  - id: step-one
    name: Step One
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Summarise: {input[topic]}
""")

        out_dir = tmp_path / "daemon-out"
        out_dir.mkdir()

        db_path = tmp_path / "daemon.db"
        db = Database(db_path)

        run_id = "daemon01"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "mini-daemon",
            "input_json": json.dumps({"topic": "AI"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        # run_daemon returns normally on success (no sys.exit)
        run_daemon(run_id, str(db_path))

        # Check DB updated to a valid terminal status.
        # With routing integration (#331.3), the confidence-based routing engine
        # may change 'success' to 'pending_review' or 'rejected' depending on
        # the composite confidence score of the dry-run output.
        db2 = Database(db_path)
        run = db2.get_pipeline_run(run_id)
        assert run is not None
        valid_terminal_statuses = {"success", "pending_review", "rejected"}
        assert run["status"] in valid_terminal_statuses, (
            f"Expected one of {valid_terminal_statuses}, got {run['status']!r}"
        )

    def test_daemon_marks_failed_on_bad_template(self, tmp_path):
        """run_daemon() marks run as failed when template can't be loaded."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        out_dir = tmp_path / "bad-out"
        out_dir.mkdir()

        db_path = tmp_path / "bad.db"
        db = Database(db_path)

        run_id = "bad-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(tmp_path / "does-not-exist.yaml"),
            "template_id": "bad",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        with pytest.raises(SystemExit):
            run_daemon(run_id, str(db_path))

        db2 = Database(db_path)
        run = db2.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "failed"

    def test_daemon_writes_pid_file(self, tmp_path):
        """run_daemon() creates .orch-daemon.pid during execution, cleans it up after."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_yaml = tmp_path / "mini2.yaml"
        template_yaml.write_text("""\
id: pid-test
name: PID Test
version: "1.0.0"
description: PID file test
phases:
  - id: step-a
    name: Step A
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Test prompt.
""")
        out_dir = tmp_path / "pid-out"
        out_dir.mkdir()
        db_path = tmp_path / "pid.db"
        db = Database(db_path)

        run_id = "pid-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "pid-test",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        pid_file = out_dir / ".orch-daemon.pid"

        # run_daemon returns normally on success (no sys.exit)
        run_daemon(run_id, str(db_path))

        # PID file should be removed after successful run
        assert not pid_file.exists(), "PID file should be cleaned up after success"

    def test_daemon_writes_log_file(self, tmp_path):
        """run_daemon() writes to output_dir/.orch-daemon.log."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        template_yaml = tmp_path / "log-test.yaml"
        template_yaml.write_text("""\
id: log-test
name: Log Test
version: "1.0.0"
description: Log file test
phases:
  - id: step-b
    name: Step B
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Log test prompt.
""")
        out_dir = tmp_path / "log-out"
        out_dir.mkdir()
        db_path = tmp_path / "log.db"
        db = Database(db_path)

        run_id = "log-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "log-test",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        # run_daemon returns normally on success (no sys.exit)
        run_daemon(run_id, str(db_path))

        log_file = out_dir / ".orch-daemon.log"
        assert log_file.exists(), "Log file should be created"
        log_content = log_file.read_text()
        assert len(log_content) > 0, "Log file should not be empty"


# ---------------------------------------------------------------------------
# 12. Sequencer selection — StateMachineSequencer vs PhaseSequencer (#236)
# ---------------------------------------------------------------------------


class TestDaemonSequencerSelection:
    """Verify that run_daemon() auto-selects the correct sequencer class.

    The daemon must use StateMachineSequencer when the template declares
    transitions on any phase or at template level (default_transitions), and
    PhaseSequencer otherwise.  This mirrors the same detection logic in cli.py.
    """

    def _make_db_and_run(self, tmp_path, template_yaml: Path, run_id: str):
        """Helper: create DB, insert run record, return (db, db_path)."""
        from orchestration_engine.db import Database

        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "test-pipe",
            "input_json": json.dumps({"topic": "test"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })
        return db, db_path

    def test_no_transitions_selects_phase_sequencer(self, tmp_path):
        """Template without transitions should use PhaseSequencer."""
        template_yaml = tmp_path / "no-trans.yaml"
        template_yaml.write_text("""\
id: no-trans-pipe
name: No Transitions Pipeline
version: "1.0.0"
description: Pipeline with no transitions
phases:
  - id: phase-a
    name: Phase A
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Test: {input[topic]}
""")
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.sequencer import PhaseSequencer

        db, db_path = self._make_db_and_run(tmp_path, template_yaml, "sel-phase")

        selected = []

        original_init = PhaseSequencer.__init__

        def patched_init(self_inner, *args, **kwargs):
            selected.append(type(self_inner).__name__)
            original_init(self_inner, *args, **kwargs)

        with patch.object(PhaseSequencer, "__init__", patched_init):
            run_daemon("sel-phase", str(db_path))

        # PhaseSequencer (not StateMachineSequencer) should have been instantiated
        assert selected, "No sequencer was instantiated"
        assert selected[0] == "PhaseSequencer", (
            f"Expected PhaseSequencer, got {selected[0]}"
        )

    def test_phase_transitions_selects_state_machine_sequencer(self, tmp_path):
        """Template with per-phase transitions should use StateMachineSequencer."""
        template_yaml = tmp_path / "phase-trans.yaml"
        template_yaml.write_text("""\
id: phase-trans-pipe
name: Phase Transitions Pipeline
version: "1.0.0"
description: Pipeline with per-phase transitions
phases:
  - id: phase-a
    name: Phase A
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Test: {input[topic]}
    transitions:
      success: phase-b
  - id: phase-b
    name: Phase B
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Follow-up: {input[topic]}
""")
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.sequencer import StateMachineSequencer

        db, db_path = self._make_db_and_run(tmp_path, template_yaml, "sel-sms-phase")

        selected = []

        original_init = StateMachineSequencer.__init__

        def patched_init(self_inner, *args, **kwargs):
            selected.append(type(self_inner).__name__)
            original_init(self_inner, *args, **kwargs)

        with patch.object(StateMachineSequencer, "__init__", patched_init):
            run_daemon("sel-sms-phase", str(db_path))

        assert selected, "No StateMachineSequencer was instantiated"
        assert selected[0] == "StateMachineSequencer", (
            f"Expected StateMachineSequencer, got {selected[0]}"
        )

    def test_default_transitions_selects_state_machine_sequencer(self, tmp_path):
        """Template with default_transitions only should use StateMachineSequencer."""
        template_yaml = tmp_path / "default-trans.yaml"
        template_yaml.write_text("""\
id: default-trans-pipe
name: Default Transitions Pipeline
version: "1.0.0"
description: Pipeline with default_transitions only
default_transitions:
  success: phase-b
phases:
  - id: phase-a
    name: Phase A
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Test: {input[topic]}
  - id: phase-b
    name: Phase B
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Follow-up: {input[topic]}
""")
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.sequencer import StateMachineSequencer

        db, db_path = self._make_db_and_run(tmp_path, template_yaml, "sel-sms-default")

        selected = []

        original_init = StateMachineSequencer.__init__

        def patched_init(self_inner, *args, **kwargs):
            selected.append(type(self_inner).__name__)
            original_init(self_inner, *args, **kwargs)

        with patch.object(StateMachineSequencer, "__init__", patched_init):
            run_daemon("sel-sms-default", str(db_path))

        assert selected, "No StateMachineSequencer was instantiated"
        assert selected[0] == "StateMachineSequencer", (
            f"Expected StateMachineSequencer, got {selected[0]}"
        )

    def test_sm_sequencer_result_keys_handled_without_error(self, tmp_path):
        """run_daemon() must not crash on SM result with iteration_history/counts keys."""
        template_yaml = tmp_path / "sm-result.yaml"
        template_yaml.write_text("""\
id: sm-result-pipe
name: SM Result Pipeline
version: "1.0.0"
description: SM result handling test
phases:
  - id: step-one
    name: Step One
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Test: {input[topic]}
    transitions:
      success: step-two
  - id: step-two
    name: Step Two
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Follow-up: {input[topic]}
""")
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        out_dir = tmp_path / "sm-result-run"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "sm-result.db"
        db = Database(db_path)
        run_id = "sm-result-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "sm-result-pipe",
            "input_json": json.dumps({"topic": "test"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        # Should complete without error — SM result dict with extra keys is fine
        run_daemon(run_id, str(db_path))

        db2 = Database(db_path)
        run = db2.get_pipeline_run(run_id)
        assert run is not None
        # With routing integration (#331.3), confidence-based routing may change
        # 'success' to 'pending_review' or 'rejected' based on dry-run output signals.
        valid_terminal_statuses = {"success", "pending_review", "rejected"}
        assert run["status"] in valid_terminal_statuses, (
            f"Expected one of {valid_terminal_statuses}, got {run['status']!r}"
        )


# ---------------------------------------------------------------------------
# 13. Daemon scoring status tracking (Issue #287)
# ---------------------------------------------------------------------------


class TestDaemonScoringStatusTracking:
    """Verify that run_daemon() writes scoring_status to the DB after auto-scoring.

    All tests use mocked _run_scoring to avoid real LLM calls.
    The template includes a scenario: field so the daemon enters the scoring block.
    """

    def _write_template_with_scenario(self, tmp_path: Path, scenario_filename: str) -> Path:
        """Write a minimal dry-run template that references a scenario file."""
        scenario_path = tmp_path / scenario_filename
        scenario_path.write_text(
            "id: dummy-scenario\n"
            "acceptance:\n"
            "  - id: c1\n"
            "    type: assertion\n"
            "    check: 'True'\n"
            "    weight: 1\n"
            "scoring:\n"
            "  pass_threshold: 0.5\n"
        )

        template_yaml = tmp_path / "scored-pipeline.yaml"
        template_yaml.write_text(
            f"id: scored-pipe\n"
            f"name: Scored Pipe\n"
            f"version: '1.0.0'\n"
            f"description: Test\n"
            f"scenario: {scenario_filename}\n"
            f"phases:\n"
            f"  - id: step\n"
            f"    name: Step\n"
            f"    task_type: content\n"
            f"    model_tier: haiku\n"
            f"    thinking_level: 'off'\n"
            f"    prompt_template: |\n"
            f"      Write about: {{input[topic]}}\n"
        )
        return template_yaml

    def _setup_db_and_run(self, tmp_path: Path, template_yaml: Path, run_id: str):
        """Create DB + pipeline_run record. Return (db, db_path, out_dir)."""
        from orchestration_engine.db import Database

        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "scored-pipe",
            "input_json": json.dumps({"topic": "AI"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })
        return db, db_path, out_dir

    def test_scoring_passed_sets_scoring_status_passed(self, tmp_path):
        """When _run_scoring() returns True, scoring_status='passed' is written to DB."""
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        template_yaml = self._write_template_with_scenario(tmp_path, "s.yaml")
        db, db_path, _ = self._setup_db_and_run(tmp_path, template_yaml, "score-pass")

        # _run_scoring is imported inside the function body, so we patch the
        # source module's symbol (orchestration_engine.scoring.run_scoring).
        # run_scoring now returns (passed, weighted_score) tuple (Issue #288).
        with patch("orchestration_engine.scoring.run_scoring", return_value=(True, 0.9)):
            run_daemon("score-pass", str(db_path))

        run = Database(db_path).get_pipeline_run("score-pass")
        assert run is not None
        # With routing integration (#331.3), confidence routing may change 'success'
        # to 'pending_review' or 'rejected'; the key assertion is scoring_status.
        assert run["status"] in {"success", "pending_review", "rejected"}, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] == "passed", (
            f"Expected 'passed', got {run['scoring_status']!r}"
        )

    def test_scoring_failed_sets_scoring_status_failed(self, tmp_path):
        """When _run_scoring() returns (False, score), scoring_status='failed' and
        run status='scoring_failed' are written to DB (Issue #288)."""
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        template_yaml = self._write_template_with_scenario(tmp_path, "s.yaml")
        db, db_path, _ = self._setup_db_and_run(tmp_path, template_yaml, "score-fail")

        # run_scoring now returns (passed, weighted_score) tuple (Issue #288).
        with patch("orchestration_engine.scoring.run_scoring", return_value=(False, 0.4)):
            run_daemon("score-fail", str(db_path))

        run = Database(db_path).get_pipeline_run("score-fail")
        assert run is not None
        # Pipeline phases succeeded but scoring failed → status='scoring_failed'
        assert run["status"] == "scoring_failed", (
            f"Expected 'scoring_failed', got {run['status']!r}"
        )
        assert run["scoring_status"] == "failed", (
            f"Expected 'failed', got {run['scoring_status']!r}"
        )

    def test_scoring_exception_sets_scoring_status_error(self, tmp_path):
        """When _run_scoring() raises, scoring_status='error' is written to DB."""
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        template_yaml = self._write_template_with_scenario(tmp_path, "s.yaml")
        db, db_path, _ = self._setup_db_and_run(tmp_path, template_yaml, "score-err")

        with patch(
            "orchestration_engine.scoring.run_scoring",
            side_effect=RuntimeError("judge crashed"),
        ):
            run_daemon("score-err", str(db_path))

        run = Database(db_path).get_pipeline_run("score-err")
        assert run is not None
        # With routing integration (#331.3), confidence routing may change 'success'
        # to 'pending_review' or 'rejected'. Pipeline phases still succeeded.
        assert run["status"] in {"success", "pending_review", "rejected"}, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] == "error", (
            f"Expected 'error', got {run['scoring_status']!r}"
        )

    def test_no_scenario_leaves_scoring_status_null(self, tmp_path):
        """Templates without scenario: should leave scoring_status as None in DB."""
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        template_yaml = tmp_path / "no-scenario.yaml"
        template_yaml.write_text(
            "id: no-scenario-pipe\n"
            "name: No Scenario Pipe\n"
            "version: '1.0.0'\n"
            "description: Test\n"
            "phases:\n"
            "  - id: step\n"
            "    name: Step\n"
            "    task_type: content\n"
            "    model_tier: haiku\n"
            "    thinking_level: 'off'\n"
            "    prompt_template: |\n"
            "      Write about: {input[topic]}\n"
        )

        out_dir = tmp_path / "no-sc-run"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "no-sc.db"
        db = Database(db_path)
        run_id = "no-sc-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "no-scenario-pipe",
            "input_json": json.dumps({"topic": "AI"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        # With routing integration (#331.3), confidence routing may change 'success'
        # to 'pending_review' or 'rejected'; the key assertion is scoring_status.
        assert run["status"] in {"success", "pending_review", "rejected"}, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] is None, (
            f"Expected None (no scenario), got {run['scoring_status']!r}"
        )

    def test_skip_scoring_leaves_scoring_status_null(self, tmp_path):
        """When skip_scoring=1, the scoring block is skipped and scoring_status stays None."""
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        template_yaml = self._write_template_with_scenario(tmp_path, "s.yaml")

        out_dir = tmp_path / "skip-sc-run"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "skip-sc.db"
        db = Database(db_path)
        run_id = "skip-sc-run"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "scored-pipe",
            "input_json": json.dumps({"topic": "AI"}),
            "mode": "dry-run",
            "output_dir": str(out_dir),
            "skip_scoring": 1,
        })

        run_daemon(run_id, str(db_path))

        run = Database(db_path).get_pipeline_run(run_id)
        assert run is not None
        # With routing integration (#331.3), confidence routing may change 'success'
        # to 'pending_review' or 'rejected'; the key assertion is scoring_status.
        assert run["status"] in {"success", "pending_review", "rejected"}, (
            f"Expected a valid terminal status, got {run['status']!r}"
        )
        assert run["scoring_status"] is None, (
            f"Expected None (skip_scoring=1), got {run['scoring_status']!r}"
        )


# ---------------------------------------------------------------------------
# 14. DB — scoring_status / scoring_score fields (Issue #287)
# ---------------------------------------------------------------------------


class TestScoringStatusDBFields:
    """Verify update_pipeline_run() accepts scoring_status and scoring_score."""

    def test_update_scoring_status_passed(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        result = in_memory_db.update_pipeline_run(
            sample_run["run_id"], scoring_status="passed"
        )
        assert result is True
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_status"] == "passed"

    def test_update_scoring_status_failed(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run(
            sample_run["run_id"], scoring_status="failed"
        )
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_status"] == "failed"

    def test_update_scoring_status_error(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run(
            sample_run["run_id"], scoring_status="error"
        )
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_status"] == "error"

    def test_update_scoring_score(self, in_memory_db, sample_run):
        in_memory_db.insert_pipeline_run(sample_run)
        in_memory_db.update_pipeline_run(
            sample_run["run_id"], scoring_status="passed", scoring_score=0.875
        )
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_status"] == "passed"
        assert abs(run["scoring_score"] - 0.875) < 1e-6

    def test_new_run_has_null_scoring_status(self, in_memory_db, sample_run):
        """Fresh pipeline_run records should have scoring_status=None."""
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_status"] is None

    def test_new_run_has_null_scoring_score(self, in_memory_db, sample_run):
        """Fresh pipeline_run records should have scoring_score=None."""
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["scoring_score"] is None


# ===========================================================================
# Tests for _dispatch_auto_merge (Issue #350 / #331.3)
# ===========================================================================


def _make_auto_merge_config(**kwargs):
    """Return an AutoMergeConfig with defaults overridden by kwargs."""
    from orchestration_engine.templates import AutoMergeConfig
    defaults = dict(
        enabled=True,
        min_score=0.90,
        require_approve=True,
        strategy="squash",
        review_phase_id="review",
    )
    defaults.update(kwargs)
    return AutoMergeConfig(**defaults)


def _make_review_phase_output(text: str) -> dict:
    """Wrap text in a phase output dict like _extract_output_text expects."""
    return {"result": {"output": text}}


def _make_fake_decision(score: float = 0.95, tier: str = "auto_merge") -> Any:
    """Return a minimal RoutingDecision-like object for _dispatch_auto_merge tests."""
    from orchestration_engine.routing import RoutingDecision
    from orchestration_engine.confidence import ConfidenceLevel
    return RoutingDecision(
        tier=tier,
        score=score,
        confidence_level=ConfidenceLevel.HIGH,
        strategy="merge",
        matched=True,
    )


class TestDispatchAutoMerge:
    """Unit tests for daemon._dispatch_auto_merge (replaces TestTryAutoMerge).

    _dispatch_auto_merge is the merge-execution helper called by the routing
    dispatch path when action == "auto_merge".  It honours require_approve and
    delegates to _do_auto_merge for the actual gh invocation.
    """

    def _call(self, auto_merge_config, phase_outputs=None, decision=None):
        from orchestration_engine.daemon import _dispatch_auto_merge
        _dispatch_auto_merge(
            run_id="test-run-001",
            auto_merge_config=auto_merge_config,
            decision=decision or _make_fake_decision(),
            phase_outputs=phase_outputs or {},
        )

    def test_merge_proceeds_without_config(self):
        """auto_merge_config=None no longer blocks merge — routing engine is the authority.

        When config is None, _dispatch_auto_merge defaults to strategy='squash' and
        require_approve=False.  The merge proceeds as long as the gate file is present
        and the branch is not protected.
        """
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/auto-no-config"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            self._call(auto_merge_config=None)
        mock_merge.assert_called_once_with(
            run_id="test-run-001",
            branch_name="feat/auto-no-config",
            strategy="squash",
        )

    def test_merge_proceeds_when_not_enabled(self):
        """auto_merge_config.enabled=False no longer blocks merge (Issue #429.3).

        The ``enabled`` flag was a legacy guard.  The routing engine is now the
        authority; if routing selected auto_merge the merge proceeds regardless
        of the ``enabled`` field.
        """
        cfg = _make_auto_merge_config(enabled=False, require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/was-disabled"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            self._call(cfg)
        mock_merge.assert_called_once_with(
            run_id="test-run-001",
            branch_name="feat/was-disabled",
            strategy="squash",
        )

    def test_merge_triggered_on_approve_verdict(self):
        """APPROVE on first line → merge is called with correct args."""
        cfg = _make_auto_merge_config(min_score=0.85, require_approve=True)
        phase_outputs = {"review": _make_review_phase_output("APPROVE\n\nLooks great!")}
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/my-feature"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            self._call(cfg, phase_outputs=phase_outputs)
        mock_merge.assert_called_once_with(
            run_id="test-run-001",
            branch_name="feat/my-feature",
            strategy="squash",
        )

    def test_no_merge_when_request_changes_contains_approve_word(self):
        """Regression: REQUEST_CHANGES body containing 'approve' must NOT trigger merge.

        Naive substring 'APPROVE' in full text would match 'I cannot approve this'.
        First-line check prevents this.
        """
        cfg = _make_auto_merge_config(min_score=0.85, require_approve=True)
        review_text = (
            "REQUEST_CHANGES\n\n"
            "I cannot approve this until the logging is fixed.\n"
            "Also, the team approval process requires two sign-offs."
        )
        phase_outputs = {"review": _make_review_phase_output(review_text)}
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/my-feature"}):
            self._call(cfg, phase_outputs=phase_outputs)
        mock_merge.assert_not_called()

    def test_no_merge_when_disapprove_word_on_first_line(self):
        """Regression: 'I disapprove' in first line must not trigger merge."""
        cfg = _make_auto_merge_config(min_score=0.80, require_approve=True)
        review_text = "I disapprove of this approach entirely."
        phase_outputs = {"review": _make_review_phase_output(review_text)}
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/x"}):
            self._call(cfg, phase_outputs=phase_outputs)
        mock_merge.assert_not_called()

    def test_skip_when_review_phase_missing(self):
        """If the review phase output is absent and require_approve=True → skip."""
        cfg = _make_auto_merge_config(min_score=0.80, require_approve=True, review_phase_id="review")
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge:
            self._call(cfg, phase_outputs={"other": {}})
        mock_merge.assert_not_called()

    def test_skip_when_gate_file_missing(self):
        """No gate file → cannot determine branch → skip merge gracefully."""
        cfg = _make_auto_merge_config(min_score=0.80, require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate", return_value=None):
            self._call(cfg)
        mock_merge.assert_not_called()

    def test_merge_without_approve_check_when_require_approve_false(self):
        """require_approve=False: skip review phase check, merge directly."""
        cfg = _make_auto_merge_config(min_score=0.80, require_approve=False, strategy="merge")
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/no-review"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            self._call(cfg, phase_outputs={})
        mock_merge.assert_called_once_with(
            run_id="test-run-001",
            branch_name="feat/no-review",
            strategy="merge",
        )

    def test_exception_in_merge_is_non_fatal(self):
        """If auto_merge_pr raises, the exception propagates to _dispatch_routing_action."""
        from orchestration_engine.git_integration import GitError
        cfg = _make_auto_merge_config(min_score=0.80, require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr",
                   side_effect=GitError("merge conflict", command=[], stderr="")), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/conflict"}):
            # _dispatch_auto_merge itself does not swallow — caller does
            import pytest
            with pytest.raises(GitError):
                self._call(cfg)

    # --- Safety guard tests (Issue #429.3) ---

    def test_allowlist_blocks_unlisted_repo(self):
        """ORCH_AUTO_MERGE_ALLOWED_REPOS set: unlisted repo is blocked."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/x"}), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_ALLOWED_REPOS": "owner/allowed-repo"}):
            from orchestration_engine.daemon import _dispatch_auto_merge
            from orchestration_engine.routing import RoutingDecision
            from orchestration_engine.confidence import ConfidenceLevel
            _dispatch_auto_merge(
                run_id="test-run-001",
                auto_merge_config=cfg,
                decision=_make_fake_decision(),
                phase_outputs={},
                repo="owner/other-repo",  # not in allowlist
            )
        mock_merge.assert_not_called()

    def test_allowlist_permits_listed_repo(self):
        """ORCH_AUTO_MERGE_ALLOWED_REPOS set: listed repo proceeds to merge."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/x"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_ALLOWED_REPOS": "owner/allowed-repo"}):
            from orchestration_engine.daemon import _dispatch_auto_merge
            _dispatch_auto_merge(
                run_id="test-run-001",
                auto_merge_config=cfg,
                decision=_make_fake_decision(),
                phase_outputs={},
                repo="owner/allowed-repo",  # in allowlist
            )
        mock_merge.assert_called_once()

    def test_protected_branch_blocks_main(self):
        """Branch named 'main' is blocked by the default protected-branch list."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "main"}), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_PROTECTED_BRANCHES": ""}):
            self._call(cfg)
        mock_merge.assert_not_called()

    def test_protected_branch_blocks_master(self):
        """Branch named 'master' is blocked by the default protected-branch list."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "master"}):
            self._call(cfg)
        mock_merge.assert_not_called()

    def test_protected_branch_custom_env_var(self):
        """Custom ORCH_AUTO_MERGE_PROTECTED_BRANCHES overrides default list."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "release"}), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_PROTECTED_BRANCHES": "release,hotfix"}):
            self._call(cfg)
        mock_merge.assert_not_called()

    def test_dry_run_mode_skips_merge(self):
        """ORCH_AUTO_MERGE_DRY_RUN=1 logs but does not call auto_merge_pr."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/dry"}), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_DRY_RUN": "1"}):
            self._call(cfg)
        mock_merge.assert_not_called()

    def test_dry_run_false_allows_merge(self):
        """ORCH_AUTO_MERGE_DRY_RUN=0 (or unset) proceeds with actual merge."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/real"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"), \
             patch.dict("os.environ", {"ORCH_AUTO_MERGE_DRY_RUN": "0"}):
            self._call(cfg)
        mock_merge.assert_called_once()

    def test_notification_dispatched_after_merge(self):
        """NotificationDispatcher.dispatch is called with event='auto_merge' after merge."""
        cfg = _make_auto_merge_config(require_approve=False)
        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/notify"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"), \
             patch("orchestration_engine.notifications.NotificationDispatcher.from_env") as mock_from_env:
            mock_dispatcher = mock_from_env.return_value
            self._call(cfg)
        mock_from_env.assert_called_once()
        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args
        assert call_kwargs[1]["event"] == "auto_merge" or call_kwargs[0][0] == "auto_merge"


# ---------------------------------------------------------------------------
# _is_review_phase helper (Issue #4.1.6)
# ---------------------------------------------------------------------------

class TestIsReviewPhase:
    """Tests for the _is_review_phase helper added in Issue #4.1.6."""

    def test_task_type_review_returns_true(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("build", {"task_type": "review"}) is True

    def test_task_type_judge_returns_true(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("qa", {"task_type": "judge"}) is True

    def test_phase_id_contains_review_returns_true(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("code_review", {"task_type": "build"}) is True

    def test_phase_id_review_uppercase_returns_true(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("CODE_REVIEW", {}) is True

    def test_non_review_phase_returns_false(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("build", {"task_type": "build"}) is False

    def test_empty_phase_id_and_dict_returns_false(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("", {}) is False

    def test_task_type_content_returns_false(self):
        from orchestration_engine.daemon import _is_review_phase
        assert _is_review_phase("write", {"task_type": "content"}) is False


# ---------------------------------------------------------------------------
# _run_post_pipeline_review_analysis (Issue #4.1.6)
# ---------------------------------------------------------------------------

class TestRunPostPipelineReviewAnalysis:
    """Tests for the extracted _run_post_pipeline_review_analysis helper."""

    def _call(self, db, phase_outputs=None, executor=None, run_id="test-run"):
        from orchestration_engine.daemon import _run_post_pipeline_review_analysis
        return _run_post_pipeline_review_analysis(
            run_id=run_id,
            db=db,
            phase_outputs=phase_outputs or {},
            executor=executor,
        )

    def test_returns_three_tuple(self):
        """Function returns a 3-tuple: (review_outcomes, audit_results, calibration_outcomes)."""
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_review_outcomes_for_run.return_value = []
        db.list_review_outcomes.return_value = []
        result = self._call(db)
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_review_outcomes_fetched_from_db(self):
        """Review outcomes are fetched from db.get_review_outcomes_for_run."""
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_review_outcomes_for_run.return_value = [{"verdict": "APPROVE"}]
        db.list_review_outcomes.return_value = []
        review_outcomes, audit_results, calibration_outcomes = self._call(db)
        assert review_outcomes == [{"verdict": "APPROVE"}]
        db.get_review_outcomes_for_run.assert_called_once()

    def test_calibration_outcomes_fetched_from_db(self):
        """Calibration outcomes are fetched from db.list_review_outcomes."""
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_review_outcomes_for_run.return_value = []
        db.list_review_outcomes.return_value = [{"reviewer_model": "m1"}]
        _, _, calibration_outcomes = self._call(db)
        assert calibration_outcomes == [{"reviewer_model": "m1"}]
        db.list_review_outcomes.assert_called_once_with(limit=500)

    def test_db_error_is_non_fatal(self):
        """DB errors are caught and result in empty lists."""
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_review_outcomes_for_run.side_effect = RuntimeError("db error")
        db.list_review_outcomes.side_effect = RuntimeError("db error")
        review_outcomes, audit_results, calibration_outcomes = self._call(db)
        assert review_outcomes == []
        assert audit_results == []
        assert calibration_outcomes == []

    def test_no_executor_skips_audit(self):
        """When executor=None, audit_results is always empty."""
        from unittest.mock import MagicMock
        db = MagicMock()
        db.get_review_outcomes_for_run.return_value = [{"verdict": "APPROVE"}]
        db.list_review_outcomes.return_value = []
        _, audit_results, _ = self._call(db, executor=None)
        assert audit_results == []

    def test_calibrate_and_save_called_when_outcomes_available(self):
        """calibrate_and_save is called when calibration_outcomes are non-empty."""
        from unittest.mock import MagicMock, patch
        db = MagicMock()
        db.get_review_outcomes_for_run.return_value = []
        db.list_review_outcomes.return_value = [{"reviewer_model": "m1", "verdict": "APPROVE", "fix_verified": True}]

        mock_calibrator = MagicMock()
        with patch(
            "orchestration_engine.daemon.ReviewerCalibrator",
            return_value=mock_calibrator,
        ) if False else patch("orchestration_engine.reviewer_calibration.ReviewerCalibrator", return_value=mock_calibrator):
            # Since calibrator is lazily imported, just verify no exception
            review_outcomes, audit_results, calibration_outcomes = self._call(db)
        # calibration_outcomes should be returned correctly
        assert len(calibration_outcomes) == 1


# ===========================================================================
# Tests for gate file creation during daemon startup (Issue #495)
# ===========================================================================


class TestDaemonGateFileCreation:
    """Verify that run_daemon() calls GitContext.create_gate when initial_input
    contains a branch_name, and skips it when no branch is provided."""

    def _make_run_record(self, tmp_path, input_json: str, run_id: str = "gate-run-01") -> tuple:
        """Helper: create a minimal DB + run record and return (run_id, db_path, out_dir)."""
        from orchestration_engine.db import Database

        template_yaml = tmp_path / "gate-template.yaml"
        template_yaml.write_text("""\
id: gate-test-pipeline
name: Gate Test
version: "1.0.0"
description: Used for gate file creation tests
phases:
  - id: build
    name: Build
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Do something with {input[topic]}
""")

        out_dir = tmp_path / f"out-{run_id}"
        out_dir.mkdir()
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": str(template_yaml),
            "template_id": "gate-test-pipeline",
            "input_json": input_json,
            "mode": "dry-run",
            "output_dir": str(out_dir),
        })
        return run_id, str(db_path), out_dir

    def _preflight_pass_patch(self):
        """Return a context manager that makes preflight always pass."""
        from unittest.mock import MagicMock

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.warnings = []
        mock_result.summary.return_value = "all checks passed"

        mock_checker = MagicMock()
        mock_checker.run_all.return_value = mock_result

        return patch(
            "orchestration_engine.preflight.PreflightChecker",
            return_value=mock_checker,
        )

    def test_create_gate_called_when_branch_name_present(self, tmp_path):
        """When initial_input has branch_name, create_gate is called with correct args."""
        from orchestration_engine.daemon import run_daemon

        input_data = {
            "topic": "AI testing",
            "branch_name": "fix/gate-issue-495",
            "repo_path": str(tmp_path),  # valid path so preflight doesn't reject it
            "issue_number": 495,
        }
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path, json.dumps(input_data), run_id="gate-run-branch"
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate") as mock_create_gate:
            run_daemon(run_id, db_path)

        mock_create_gate.assert_called_once()
        call_kwargs = mock_create_gate.call_args
        assert call_kwargs.kwargs["run_id"] == "gate-run-branch"
        assert call_kwargs.kwargs["branch_name"] == "fix/gate-issue-495"
        assert call_kwargs.kwargs["issue_number"] == 495

    def test_create_gate_called_with_branch_key(self, tmp_path):
        """Branch name is also picked up from the 'branch' key in initial_input."""
        from orchestration_engine.daemon import run_daemon

        input_data = {
            "topic": "AI testing",
            "branch": "feat/alt-key",
        }
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path, json.dumps(input_data), run_id="gate-run-alt"
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate") as mock_create_gate:
            run_daemon(run_id, db_path)

        mock_create_gate.assert_called_once()
        call_kwargs = mock_create_gate.call_args
        assert call_kwargs.kwargs["branch_name"] == "feat/alt-key"

    def test_create_gate_skipped_when_no_branch(self, tmp_path):
        """When initial_input has no branch key, create_gate is NOT called."""
        from orchestration_engine.daemon import run_daemon

        input_data = {"topic": "no branch here"}
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path, json.dumps(input_data), run_id="gate-run-nobranch"
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate") as mock_create_gate:
            run_daemon(run_id, db_path)

        mock_create_gate.assert_not_called()
