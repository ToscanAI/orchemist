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
        """'orch start' inserts a pipeline_runs record and exits 0."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "start-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "start",
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
        """'orch start' prints the run ID to stdout."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "print-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "start",
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
        """'orch start' with nonexistent template exits with error."""
        from orchestration_engine.cli import main

        result = cli_runner.invoke(main, [
            "start",
            str(tmp_path / "no-such.yaml"),
            "--mode", "dry-run",
            "--db-path", str(tmp_db.db_path),
        ])

        assert result.exit_code != 0

    def test_start_command_spawns_popen(
        self, cli_runner, tmp_path, minimal_template_yaml, tmp_db
    ):
        """'orch start' calls subprocess.Popen with start_new_session=True."""
        from orchestration_engine.cli import main

        out_dir = tmp_path / "popen-out"

        with patch("orchestration_engine.cli.Database", return_value=tmp_db), \
             patch("orchestration_engine.cli.subprocess") as mock_subp:
            mock_proc = MagicMock()
            mock_proc.pid = 88888
            mock_subp.Popen.return_value = mock_proc

            result = cli_runner.invoke(main, [
                "start",
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

        # Check DB updated to success
        db2 = Database(db_path)
        run = db2.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "success", f"Expected success, got {run['status']}"

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
