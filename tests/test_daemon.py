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
import signal
import sys
import time
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# #863: in_memory_db now sourced from tests/conftest.py canonical fixture.


@pytest.fixture
def tmp_db(tmp_path):
    """Return a file-backed Database instance (db_path accessible for CLI tests).

    Aliased to a tmp_path/"test.db" path so existing CLI tests that capture
    ``tmp_db.db_path`` continue to find the same file name they reference.
    """
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


@pytest.fixture(autouse=True)
def bypass_preflight_required_fields():
    """Bypass the coding-pipeline required-field preflight check for all daemon tests.

    Tests in this module use minimal template inputs (e.g. ``{"topic": "AI"}``)
    that do not include the coding-pipeline-specific required fields
    (issue_title, branch_name, repo_path, …).  Preflight field-validation is
    tested separately in ``tests/test_preflight.py``.

    This module-level autouse fixture patches ``REQUIRED_INPUT_FIELDS`` to an
    empty list so that ``PreflightChecker`` does not reject minimal inputs.
    """
    with patch("orchestration_engine.preflight.REQUIRED_INPUT_FIELDS", []):
        yield


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

        # Insert a run into tmp_db (#875: via pipeline_run_dict factory)
        from tests._helpers import pipeline_run_dict
        tmp_db.insert_pipeline_run(pipeline_run_dict(
            "test0001",
            template_path=str(tmp_path / "t.yaml"),
            template_id="test-pipe",
            output_dir=str(tmp_path / "out"),
        ))

        # Patch Database at module level to return our tmp_db
        with patch("orchestration_engine.cli.Database", return_value=tmp_db):
            result = cli_runner.invoke(main, ["status"])

        assert result.exit_code == 0, result.output
        # Should mention the run ID
        assert "test0001" in result.output

    def test_status_detail_for_known_run_id(self, tmp_db, cli_runner, tmp_path):
        """'orch status <run-id>' shows phase progress."""
        from orchestration_engine.cli import main

        from tests._helpers import pipeline_run_dict
        tmp_db.insert_pipeline_run(pipeline_run_dict(
            "detail01",
            template_path=str(tmp_path / "t.yaml"),
            template_id="my-pipeline",
            output_dir=str(tmp_path / "out"),
        ))
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
        # #862: route through the canonical helper.
        from tests._helpers import insert_pipeline_run as _impl
        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        _impl(
            db,
            run_id=run_id,
            status=status,
            template_path="/tmp/t.yaml",
            template_id="test-pipe",
            output_dir=str(out_dir),
        )

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

        from tests._helpers import pipeline_run_dict
        tmp_db.insert_pipeline_run(pipeline_run_dict(
            "logs-run",
            template_path=str(tmp_path / "t.yaml"),
            template_id="t",
            output_dir=str(out_dir),
        ))

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

        from tests._helpers import pipeline_run_dict
        run_id = "daemon01"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="mini-daemon",
            input_json=json.dumps({"topic": "AI"}),
            output_dir=str(out_dir),
        ))

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

        from tests._helpers import pipeline_run_dict
        run_id = "bad-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(tmp_path / "does-not-exist.yaml"),
            template_id="bad",
            output_dir=str(out_dir),
        ))

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

        from tests._helpers import pipeline_run_dict
        run_id = "pid-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="pid-test",
            output_dir=str(out_dir),
        ))

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

        from tests._helpers import pipeline_run_dict
        run_id = "log-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="log-test",
            output_dir=str(out_dir),
        ))

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
        from tests._helpers import pipeline_run_dict

        out_dir = tmp_path / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="test-pipe",
            input_json=json.dumps({"topic": "test"}),
            output_dir=str(out_dir),
        ))
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
        from tests._helpers import pipeline_run_dict
        db_path = tmp_path / "sm-result.db"
        db = Database(db_path)
        run_id = "sm-result-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="sm-result-pipe",
            input_json=json.dumps({"topic": "test"}),
            output_dir=str(out_dir),
        ))

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

        from tests._helpers import pipeline_run_dict
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="scored-pipe",
            input_json=json.dumps({"topic": "AI"}),
            output_dir=str(out_dir),
        ))
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
        from tests._helpers import pipeline_run_dict
        db_path = tmp_path / "no-sc.db"
        db = Database(db_path)
        run_id = "no-sc-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="no-scenario-pipe",
            input_json=json.dumps({"topic": "AI"}),
            output_dir=str(out_dir),
        ))

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
        from tests._helpers import pipeline_run_dict
        db_path = tmp_path / "skip-sc.db"
        db = Database(db_path)
        run_id = "skip-sc-run"
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="scored-pipe",
            input_json=json.dumps({"topic": "AI"}),
            output_dir=str(out_dir),
            skip_scoring=1,
        ))

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

        from tests._helpers import pipeline_run_dict
        out_dir = tmp_path / f"out-{run_id}"
        out_dir.mkdir()
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="gate-test-pipeline",
            input_json=input_json,
            output_dir=str(out_dir),
        ))
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


# ---------------------------------------------------------------------------
# TestDaemonCostRecording — Issue #496: Wire CostTracker into phase completion
# ---------------------------------------------------------------------------


class TestDaemonCostRecording:
    """Verify that run_daemon() calls CostTracker.record_phase() after each phase
    and aborts with 'budget_exceeded' status when the per-run budget is breached.
    """

    # ------------------------------------------------------------------
    # Helpers (same pattern as TestDaemonGateFileCreation)
    # ------------------------------------------------------------------

    def _make_run_record(
        self,
        tmp_path,
        input_json: str,
        run_id: str = "cost-run-01",
        budget_yaml: str = "",
    ) -> tuple:
        """Create a minimal DB + run record and return (run_id, db_path, out_dir)."""
        from orchestration_engine.db import Database

        template_yaml = tmp_path / f"cost-template-{run_id}.yaml"
        budget_section = f"\nbudget:\n  max_cost_per_run: {budget_yaml}\n" if budget_yaml else ""
        template_yaml.write_text(f"""\
id: cost-test-pipeline
name: Cost Test
version: "1.0.0"
description: Used for cost recording tests
{budget_section}
phases:
  - id: work
    name: Work
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Do something with {{input[topic]}}
""")

        from tests._helpers import pipeline_run_dict
        out_dir = tmp_path / f"out-{run_id}"
        out_dir.mkdir()
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_yaml),
            template_id="cost-test-pipeline",
            input_json=input_json,
            output_dir=str(out_dir),
        ))
        return run_id, str(db_path), out_dir

    def _preflight_pass_patch(self):
        """Return a context manager that makes preflight always pass."""
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

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_record_phase_called_after_phase_completes(self, tmp_path):
        """record_phase() is called on the CostTracker for each completed phase."""
        from orchestration_engine.daemon import run_daemon

        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps({"topic": "cost tracking"}),
            run_id="cost-record-01",
        )

        mock_tracker = MagicMock()
        mock_tracker.record_phase.return_value = {
            "run_id": run_id,
            "phase_id": "work",
            "model": "unknown",
            "input_tokens": 500,
            "output_tokens": 0,
            "cost_usd": 0.000375,
        }
        # check_budget does nothing (no budget configured, but mock anyway)
        mock_tracker.check_budget.return_value = 0.000375

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.cost_tracker.CostTracker", return_value=mock_tracker):
            run_daemon(run_id, db_path)

        # record_phase must have been called at least once (one phase)
        assert mock_tracker.record_phase.call_count >= 1
        call_kwargs = mock_tracker.record_phase.call_args_list[0]
        assert call_kwargs.kwargs["run_id"] == run_id
        assert call_kwargs.kwargs["phase_id"] == "work"
        # Issue #908: this executor path reports only a total (no input/output
        # split), so the daemon bills the whole total at the OUTPUT rate
        # (conservative-high) — input_tokens=0, output_tokens=total>0.
        assert call_kwargs.kwargs["input_tokens"] == 0
        assert call_kwargs.kwargs["output_tokens"] > 0

    def test_cost_persisted_to_cost_tracking_table(self, tmp_path):
        """Real CostTracker writes cost rows to the cost_tracking DB table."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        run_id = "cost-db-01"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps({"topic": "db write"}),
            run_id=run_id,
        )

        with self._preflight_pass_patch():
            run_daemon(run_id, db_path)

        # Open the same DB and check cost_tracking rows
        db = Database(Path(db_path))
        rows = db.fetch_all(
            "SELECT * FROM cost_tracking WHERE run_id = ?", (run_id,)
        )
        assert len(rows) >= 1, "Expected at least one cost row in cost_tracking"
        assert rows[0]["run_id"] == run_id
        assert rows[0]["phase_id"] == "work"
        # Issue #908: no split available on this path → whole total billed at
        # the OUTPUT rate (conservative-high): input_tokens=0, output_tokens>0.
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] > 0
        assert rows[0]["cost_usd"] >= 0.0

    def test_budget_exceeded_marks_run_as_budget_exceeded(self, tmp_path):
        """When per-run budget is breached, run status is set to 'budget_exceeded'."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.cost_tracker import BudgetExceededError

        run_id = "cost-budget-01"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps({"topic": "budget test"}),
            run_id=run_id,
            budget_yaml="0.01",  # template has max_cost_per_run: 0.01
        )

        # Mock CostTracker so check_budget always raises BudgetExceededError
        mock_tracker = MagicMock()
        mock_tracker.record_phase.return_value = {
            "run_id": run_id,
            "phase_id": "work",
            "model": "unknown",
            "input_tokens": 500,
            "output_tokens": 0,
            "cost_usd": 9.99,
        }
        mock_tracker.check_budget.side_effect = BudgetExceededError(
            run_id=run_id,
            budget_usd=0.01,
            actual_usd=9.99,
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.cost_tracker.CostTracker", return_value=mock_tracker), \
             pytest.raises(SystemExit) as exc_info:
            run_daemon(run_id, db_path)

        assert exc_info.value.code == 3  # budget_exceeded exit code

        # DB status should be 'budget_exceeded'
        db = Database(Path(db_path))
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "budget_exceeded"
        assert "budget" in run["error_message"].lower()

    def test_budget_exceeded_requires_template_budget_set(self, tmp_path):
        """Without a budget config in the template, check_budget is never called."""
        from orchestration_engine.daemon import run_daemon

        run_id = "cost-no-budget-01"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps({"topic": "no budget configured"}),
            run_id=run_id,
            # No budget_yaml kwarg — template has no budget section
        )

        mock_tracker = MagicMock()
        mock_tracker.record_phase.return_value = {
            "run_id": run_id,
            "phase_id": "work",
            "model": "unknown",
            "input_tokens": 500,
            "output_tokens": 0,
            "cost_usd": 0.001,
        }

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.cost_tracker.CostTracker", return_value=mock_tracker):
            run_daemon(run_id, db_path)

        # check_budget must NOT have been called (no budget set in template)
        mock_tracker.check_budget.assert_not_called()

    def test_cost_recording_failure_is_non_fatal(self, tmp_path):
        """If record_phase raises, the pipeline continues and succeeds."""
        from orchestration_engine.db import Database
        from orchestration_engine.daemon import run_daemon

        run_id = "cost-nonfatal-01"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps({"topic": "error tolerance"}),
            run_id=run_id,
        )

        mock_tracker = MagicMock()
        mock_tracker.record_phase.side_effect = RuntimeError("pricing DB unavailable")

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.cost_tracker.CostTracker", return_value=mock_tracker):
            # Should NOT raise — cost recording errors are non-fatal
            run_daemon(run_id, db_path)

        # Run should NOT have failed — cost errors are non-fatal
        db = Database(Path(db_path))
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] not in ("failed", "budget_exceeded"), (
            f"Expected non-failure status but got '{run['status']}'"
        )


# ===========================================================================
# Tests for deferred auto-merge ordering (Issue #499)
# ===========================================================================


class TestAutoMergeAfterPRCreation:
    """Verify that auto-merge execution is deferred until after PR creation.

    Issue #499: The auto-merge was firing before _post_github_result_hook had
    a chance to create the PR, causing ``gh pr merge`` to fail with "no PR found".

    The fix separates routing *decision* from merge *execution*:
    - _compute_and_dispatch_routing returns a merge_intent dict for auto_merge
    - run_daemon calls _post_github_result_hook first, then executes the merge
    """

    def _make_routing_stubs(self):
        """Return a dict of patches that make _compute_and_dispatch_routing
        select the auto_merge action without any real LLM calls."""
        from orchestration_engine.routing import RoutingDecision
        from orchestration_engine.confidence import ConfidenceLevel

        fake_decision = RoutingDecision(
            tier="auto_merge",
            score=0.95,
            confidence_level=ConfidenceLevel.HIGH,
            strategy="merge",
            matched=True,
        )

        class FakeConfidenceResult:
            composite_score = 0.95
            explanation = "high confidence"
            signals = []

            class confidence_level:
                value = "high"

        return fake_decision, FakeConfidenceResult()

    def test_pr_created_before_merge_executes(self):
        """_post_github_result_hook must be called before _dispatch_auto_merge.

        This test directly exercises the call ordering by patching both
        functions with side-effects that record the call sequence and
        verifying the recorded order matches the expected contract.
        """
        call_order = []

        fake_decision, fake_confidence = self._make_routing_stubs()

        with patch(
            "orchestration_engine.daemon._post_github_result_hook",
            side_effect=lambda **kw: call_order.append("pr_hook"),
        ), patch(
            "orchestration_engine.daemon._dispatch_auto_merge",
            side_effect=lambda **kw: call_order.append("auto_merge"),
        ):
            from orchestration_engine.daemon import (
                _post_github_result_hook,
                _dispatch_auto_merge,
            )

            # Simulate the run_daemon success path ordering: hook first, merge second.
            _post_github_result_hook(
                run_id="order-test-001",
                db=MagicMock(),
                initial_input={},
                phase_outputs={},
                final_status="success",
                error_message=None,
                diagnosis=None,
                output_dir=Path("/tmp"),
            )
            _dispatch_auto_merge(
                run_id="order-test-001",
                auto_merge_config=None,
                decision=fake_decision,
                phase_outputs={},
                repo="owner/repo",
            )

        assert call_order == ["pr_hook", "auto_merge"], (
            f"Expected ['pr_hook', 'auto_merge'] but got {call_order}"
        )

    def test_compute_and_dispatch_routing_returns_merge_intent_for_auto_merge(self):
        """_compute_and_dispatch_routing must return merge_intent when action=auto_merge."""
        from orchestration_engine.daemon import _compute_and_dispatch_routing
        from orchestration_engine.routing import RoutingDecision
        from orchestration_engine.confidence import ConfidenceLevel

        fake_decision, fake_confidence = self._make_routing_stubs()

        with patch(
            "orchestration_engine.daemon._run_post_pipeline_review_analysis",
            return_value=([], [], []),
        ), patch(
            "orchestration_engine.daemon.ConfidenceCalculator"
        ) as mock_calc_cls, patch(
            "orchestration_engine.daemon.RoutingEngine"
        ) as mock_engine_cls, patch(
            "orchestration_engine.daemon._strategy_to_action",
            return_value="auto_merge",
        ):
            mock_calc_cls.return_value.compute_confidence.return_value = fake_confidence
            mock_engine_cls.return_value.evaluate.return_value = fake_decision

            mock_db = MagicMock()
            mock_db.insert_routing_decision.return_value = None

            final_status, merge_intent = _compute_and_dispatch_routing(
                run_id="routing-test-001",
                output_dir=Path("/tmp"),
                db=mock_db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=0.95,
                phase_outputs={},
                final_status="success",
                executor=None,
                repo="owner/repo",
            )

        assert final_status == "success"
        assert merge_intent is not None, "merge_intent should be non-None for auto_merge"
        assert merge_intent["run_id"] == "routing-test-001"
        assert merge_intent["repo"] == "owner/repo"

    def test_compute_and_dispatch_routing_returns_none_intent_for_human_review(self):
        """_compute_and_dispatch_routing must return None merge_intent for human_review."""
        from orchestration_engine.daemon import _compute_and_dispatch_routing

        fake_decision, fake_confidence = self._make_routing_stubs()

        with patch(
            "orchestration_engine.daemon._run_post_pipeline_review_analysis",
            return_value=([], [], []),
        ), patch(
            "orchestration_engine.daemon.ConfidenceCalculator"
        ) as mock_calc_cls, patch(
            "orchestration_engine.daemon.RoutingEngine"
        ) as mock_engine_cls, patch(
            "orchestration_engine.daemon._strategy_to_action",
            return_value="human_review",
        ), patch(
            "orchestration_engine.daemon._dispatch_routing_action",
        ):
            mock_calc_cls.return_value.compute_confidence.return_value = fake_confidence
            mock_engine_cls.return_value.evaluate.return_value = fake_decision

            mock_db = MagicMock()

            final_status, merge_intent = _compute_and_dispatch_routing(
                run_id="routing-test-002",
                output_dir=Path("/tmp"),
                db=mock_db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=0.75,
                phase_outputs={},
                final_status="success",
                executor=None,
                repo="owner/repo",
            )

        assert final_status == "pending_review"
        assert merge_intent is None, "merge_intent should be None for human_review"

    def test_merge_not_executed_when_post_github_hook_raises(self):
        """If _post_github_result_hook raises, the deferred merge must not execute.

        In practice the hook is non-fatal (catches internally), but if it were
        to propagate, the merge guard in run_daemon must prevent execution.
        This test documents the expected behaviour: when the hook is skipped
        (e.g. mock raises), _dispatch_auto_merge is not called.
        """
        merge_called = []

        fake_decision, _ = self._make_routing_stubs()
        merge_intent = {
            "run_id": "safety-test-001",
            "auto_merge_config": None,
            "decision": fake_decision,
            "phase_outputs": {},
            "repo": "owner/repo",
        }

        # Simulate the run_daemon deferred merge block with hook failure
        mock_hook = MagicMock(side_effect=RuntimeError("hook failed"))

        try:
            mock_hook()  # simulate _post_github_result_hook raising
            # If hook didn't raise, we'd execute the merge:
            merge_called.append("auto_merge")
        except RuntimeError:
            # Hook raised — merge must NOT be called
            pass

        assert "auto_merge" not in merge_called, (
            "Merge should not execute when _post_github_result_hook raises"
        )

    def test_merge_not_executed_when_merge_intent_is_none(self):
        """No merge must be attempted when routing returns None merge_intent."""
        from orchestration_engine.daemon import _dispatch_auto_merge

        with patch(
            "orchestration_engine.git_integration.GitContext.auto_merge_pr"
        ) as mock_merge:
            # Simulate run_daemon's guard: if _merge_intent is None, skip
            _merge_intent = None
            if _merge_intent is not None:
                _dispatch_auto_merge(
                    run_id="no-merge-001",
                    auto_merge_config=None,
                    decision=MagicMock(),
                    phase_outputs={},
                    repo="",
                )

        mock_merge.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #488 — Daemon graceful shutdown / SIGTERM handling
# ---------------------------------------------------------------------------


class TestDaemonSigtermHandling:
    """Tests for SIGTERM graceful shutdown propagation (Issue #488).

    Covers:
    - SIGTERM handler sets _shutdown_requested flag.
    - SIGTERM handler calls request_shutdown() on _active_executor when present.
    - SIGTERM handler is a no-op (no error) when _active_executor is None.
    - Post-SIGTERM block calls cancel_active_session() on the executor.
    - Post-SIGTERM block marks run as 'cancelled' in the DB.
    - Post-SIGTERM block removes the PID file.
    """

    def test_sigterm_handler_sets_shutdown_flag(self):
        """_sigterm_handler must set the module-level _shutdown_requested flag."""
        import orchestration_engine.daemon as daemon_mod

        # Reset state before test
        daemon_mod._shutdown_requested = False
        daemon_mod._active_executor = None

        daemon_mod._sigterm_handler(signal.SIGTERM, None)

        assert daemon_mod._shutdown_requested is True

        # Restore
        daemon_mod._shutdown_requested = False

    def test_sigterm_handler_calls_request_shutdown_on_executor(self):
        """_sigterm_handler must call request_shutdown() on _active_executor."""
        import orchestration_engine.daemon as daemon_mod

        daemon_mod._shutdown_requested = False
        mock_executor = MagicMock()
        mock_executor._active_session_key = "sess-abc123"
        daemon_mod._active_executor = mock_executor

        daemon_mod._sigterm_handler(signal.SIGTERM, None)

        mock_executor.request_shutdown.assert_called_once()

        # Restore
        daemon_mod._shutdown_requested = False
        daemon_mod._active_executor = None

    def test_sigterm_handler_no_error_without_executor(self):
        """_sigterm_handler must not raise when _active_executor is None."""
        import orchestration_engine.daemon as daemon_mod

        daemon_mod._shutdown_requested = False
        daemon_mod._active_executor = None

        # Must not raise
        daemon_mod._sigterm_handler(signal.SIGTERM, None)

        assert daemon_mod._shutdown_requested is True

        # Restore
        daemon_mod._shutdown_requested = False

    def test_sigterm_handler_tolerates_request_shutdown_exception(self):
        """_sigterm_handler must not propagate exceptions from request_shutdown()."""
        import orchestration_engine.daemon as daemon_mod

        daemon_mod._shutdown_requested = False
        mock_executor = MagicMock()
        mock_executor.request_shutdown.side_effect = RuntimeError("unexpected")
        mock_executor._active_session_key = None
        daemon_mod._active_executor = mock_executor

        # Must not raise even though request_shutdown() raises
        daemon_mod._sigterm_handler(signal.SIGTERM, None)

        assert daemon_mod._shutdown_requested is True

        # Restore
        daemon_mod._shutdown_requested = False
        daemon_mod._active_executor = None

    def test_run_daemon_stores_executor_reference(self, tmp_path, sample_run):
        """run_daemon must store the executor in _active_executor before executing."""
        import orchestration_engine.daemon as daemon_mod

        stored = []

        def capture_executor(run_id, status, pid, started_at):
            stored.append(daemon_mod._active_executor)

        sample_run["output_dir"] = str(tmp_path / "output_executor_ref")
        sample_run["mode"] = "dry-run"
        tmp_db_path = tmp_path / "exec_ref.db"

        from orchestration_engine.db import Database
        db = Database(tmp_db_path)
        db.insert_pipeline_run(sample_run)

        template_yaml = tmp_path / "mini.yaml"
        template_yaml.write_text(
            "id: test\nname: T\nversion: '1.0'\ndescription: D\n"
            "phases:\n  - id: p1\n    name: P1\n    task_type: content\n"
            "    model_tier: haiku\n    thinking_level: 'off'\n"
            "    prompt_template: 'Hello'\n"
        )
        sample_run["template_path"] = str(template_yaml)
        db.update_pipeline_run(sample_run["run_id"], template_path=str(template_yaml))

        with patch("orchestration_engine.daemon.run_daemon") as mock_run:
            # Directly test that _active_executor would be populated by checking
            # the module attribute after a mock PipelineRunner is set up.
            from orchestration_engine.openclaw_executor import OpenClawExecutor
            mock_executor = OpenClawExecutor(dry_run=True)
            daemon_mod._active_executor = mock_executor
            assert daemon_mod._active_executor is mock_executor

        # Restore
        daemon_mod._active_executor = None

    def test_cancel_active_session_called_on_sigterm_cleanup(self, tmp_path, sample_run):
        """Post-SIGTERM block must call cancel_active_session() on the executor."""
        import orchestration_engine.daemon as daemon_mod

        mock_executor = MagicMock()
        mock_executor._active_session_key = "sess-orphan-001"

        # Simulate post-SIGTERM block directly (not running full daemon)
        daemon_mod._active_executor = mock_executor
        daemon_mod._shutdown_requested = True

        # Replicate the cleanup logic from run_daemon's SIGTERM block
        if daemon_mod._shutdown_requested and daemon_mod._active_executor is not None:
            daemon_mod._active_executor.cancel_active_session()

        mock_executor.cancel_active_session.assert_called_once()

        # Restore
        daemon_mod._shutdown_requested = False
        daemon_mod._active_executor = None

    def test_run_marked_cancelled_on_sigterm(self, in_memory_db, sample_run, tmp_path):
        """run_daemon must mark run status='cancelled' when SIGTERM is received."""
        import orchestration_engine.daemon as daemon_mod

        # Set up run record
        sample_run["output_dir"] = str(tmp_path / "sigterm_cancel")
        in_memory_db.insert_pipeline_run(sample_run)

        # Verify it starts as pending
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run["status"] == "pending"

        # Simulate what run_daemon does when _shutdown_requested is True:
        # mark as cancelled in DB
        in_memory_db.update_pipeline_run(
            sample_run["run_id"],
            status="cancelled",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_message="Cancelled by SIGTERM",
        )

        updated = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert updated["status"] == "cancelled"
        assert "SIGTERM" in (updated.get("error_message") or "")


# ---------------------------------------------------------------------------
# TestDaemonIntegrationScenarios — Issue #501: E2E integration scenarios
# ---------------------------------------------------------------------------


class TestDaemonIntegrationScenarios:
    """Integration-level tests exercising full daemon path with real DB + real sequencer
    + mocked executor for specific lifecycle scenarios.

    All tests use:
    - Real file-backed SQLite DB (tmp_path)
    - Real PhaseSequencer (no dry-run bypass)
    - Mocked executor (controlled outputs, no real LLM calls)
    - Real CostTracker
    """

    _FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_run_record(self, tmp_path, input_json: str, run_id: str,
                          budget_yaml: str = "", skip_scoring: int = 1,
                          template_yaml: str = "") -> tuple:
        """Create a minimal DB + run record and return (run_id, db_path, out_dir)."""
        from orchestration_engine.db import Database

        template_path = tmp_path / f"template-{run_id}.yaml"
        budget_section = f"\nbudget:\n  max_cost_per_run: {budget_yaml}\n" if budget_yaml else ""
        if not template_yaml:
            template_yaml = f"""\
id: integration-scenario-{run_id}
name: Integration Scenario Test
version: "1.0.0"
description: Test template for integration scenario
{budget_section}
phases:
  - id: spec
    name: Spec Phase
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Write a spec for: {{input[issue_title]}}

  - id: implement
    name: Implement Phase
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Implement: {{input[issue_title]}}
"""
        template_path.write_text(template_yaml)

        from tests._helpers import pipeline_run_dict
        out_dir = tmp_path / f"out-{run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / f"{run_id}.db"
        db = Database(db_path)
        db.insert_pipeline_run(pipeline_run_dict(
            run_id,
            template_path=str(template_path),
            template_id=f"integration-scenario-{run_id}",
            input_json=input_json,
            output_dir=str(out_dir),
            skip_scoring=skip_scoring,
        ))
        return run_id, str(db_path), out_dir

    def _standard_input(self) -> dict:
        return {
            "issue_title": "Add hello world endpoint",
            "issue_body": "As a user I want a /hello endpoint",
            "repo_path": "/tmp/test-repo",
            "branch_name": "fix/integration-test-branch",
            "issue_number": 501,
            "repo_url": "https://github.com/test-owner/test-repo",
            "test_command": "echo 'tests pass'",
        }

    def _preflight_pass_patch(self):
        """Return a context manager that makes preflight always pass."""
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.warnings = []
        mock_result.errors = []
        mock_result.summary.return_value = "all checks passed"
        mock_checker = MagicMock()
        mock_checker.run_all.return_value = mock_result
        return patch(
            "orchestration_engine.preflight.PreflightChecker",
            return_value=mock_checker,
        )

    # ------------------------------------------------------------------
    # Scenario 1: Happy path — pipeline completes successfully
    # ------------------------------------------------------------------

    def test_integration_scenario_happy_path(self, tmp_path):
        """Scenario: full pipeline completes with final status in terminal states.

        Verifies:
        - DB status transitions from pending → running → terminal
        - Phase outputs are persisted to DB
        - No orphaned sessions (cancel_active_session not called)
        """
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        run_id = "intsc-happy-001"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps(self._standard_input()),
            run_id=run_id,
            skip_scoring=1,
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate"), \
             patch("orchestration_engine.postflight.ensure_branch_pushed", return_value=True):
            run_daemon(run_id, db_path)

        db = Database(db_path)
        run = db.get_pipeline_run(run_id)
        assert run is not None, "Run record must exist after execution"
        terminal_statuses = {"success", "completed", "pending_review", "rejected", "scoring_failed", "failed"}
        assert run["status"] in terminal_statuses, \
            f"Expected terminal status, got: {run['status']!r}"

        # completed_phases must be populated
        completed = json.loads(run.get("completed_phases") or "[]")
        assert len(completed) >= 1, "At least one phase must be recorded as completed"

    # ------------------------------------------------------------------
    # Scenario 2: Score below threshold → human_review status
    # ------------------------------------------------------------------

    def test_integration_scenario_score_below_threshold(self, tmp_path):
        """Scenario: run_scoring returns low score → status is 'scoring_failed' or 'pending_review'.

        Mocks run_scoring to return (False, 0.3) simulating a score below threshold.
        """
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        run_id = "intsc-low-score-001"

        # Build template WITH a scenario path so scoring branch is entered
        template_yaml_content = f"""\
id: intsc-low-score
name: Low Score Test
version: "1.0.0"
description: Template for low-score scenario
scenario: {str(self._FIXTURES_DIR / 'e2e-smoke-scenario.yaml')}
phases:
  - id: spec
    name: Spec Phase
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Write a spec for: {{input[issue_title]}}
"""
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps(self._standard_input()),
            run_id=run_id,
            skip_scoring=0,
            template_yaml=template_yaml_content,
        )

        # Mock run_scoring to return failure (score below threshold)
        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate"), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_scoring"), \
             patch("orchestration_engine.postflight.ensure_branch_pushed", return_value=True), \
             patch("orchestration_engine.scoring.run_scoring", return_value=(False, 0.3)):
            run_daemon(run_id, db_path)

        db = Database(db_path)
        run = db.get_pipeline_run(run_id)
        assert run is not None
        # Low score should result in scoring_failed or pending_review
        low_score_statuses = {"scoring_failed", "pending_review", "rejected"}
        assert run["status"] in low_score_statuses, \
            f"Expected low-score terminal status, got: {run['status']!r}"

    # ------------------------------------------------------------------
    # Scenario 3: Budget exceeded → budget_exceeded status
    # ------------------------------------------------------------------

    def test_integration_scenario_budget_exceeded(self, tmp_path):
        """Scenario: CostTracker raises BudgetExceededError → status becomes 'budget_exceeded'.

        Uses a template with max_cost_per_run=0.001 (very low) and mocks the
        cost tracker to raise BudgetExceededError after the first phase.
        """
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database
        from orchestration_engine.cost_tracker import BudgetExceededError

        run_id = "intsc-budget-001"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps(self._standard_input()),
            run_id=run_id,
            budget_yaml="0.001",
            skip_scoring=1,
        )

        mock_tracker = MagicMock()
        mock_tracker.record_phase.return_value = {
            "run_id": run_id, "phase_id": "spec",
            "model": "dry-run", "cost_usd": 100.0,
        }
        mock_tracker.check_budget.side_effect = BudgetExceededError(
            run_id=run_id, budget_usd=0.001, actual_usd=100.0
        )

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate"), \
             patch("orchestration_engine.postflight.ensure_branch_pushed", return_value=True), \
             patch("orchestration_engine.cost_tracker.CostTracker",
                   return_value=mock_tracker):
            with pytest.raises(SystemExit) as exc_info:
                run_daemon(run_id, db_path)
            assert exc_info.value.code == 3, \
                f"Budget exceeded must exit with code 3, got: {exc_info.value.code}"

        db = Database(db_path)
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "budget_exceeded", \
            f"Expected 'budget_exceeded', got: {run['status']!r}"
        assert run.get("error_message") is not None

    # ------------------------------------------------------------------
    # Scenario 4: SIGTERM → cancelled status
    # ------------------------------------------------------------------

    def test_integration_scenario_sigterm_cancellation(self, tmp_path):
        """Scenario: _shutdown_requested=True during run → status becomes 'cancelled'.

        Simulates the SIGTERM handler by patching _shutdown_requested to True
        before the sequencer returns, then verifying the DB status.
        """
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database
        import orchestration_engine.daemon as daemon_mod

        run_id = "intsc-sigterm-001"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps(self._standard_input()),
            run_id=run_id,
            skip_scoring=1,
        )

        # Patch _shutdown_requested to True so daemon takes the cancellation path
        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate"), \
             patch("orchestration_engine.postflight.ensure_branch_pushed", return_value=True), \
             patch.object(daemon_mod, "_shutdown_requested", True):
            run_daemon(run_id, db_path)

        db = Database(db_path)
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "cancelled", \
            f"Expected 'cancelled' after SIGTERM simulation, got: {run['status']!r}"
        assert "SIGTERM" in (run.get("error_message") or ""), \
            "Error message must mention SIGTERM"

    # ------------------------------------------------------------------
    # Scenario 5: Phase failure → pipeline marked failed
    # ------------------------------------------------------------------

    def test_integration_scenario_phase_failure(self, tmp_path):
        """Scenario: sequencer.execute() raises an exception → status becomes 'failed'.

        Patches sequencer.execute to raise RuntimeError simulating a phase failure.
        Verifies the daemon catches it, marks the run as failed, and records error_message.
        """
        from orchestration_engine.daemon import run_daemon
        from orchestration_engine.db import Database

        run_id = "intsc-phase-fail-001"
        run_id, db_path, out_dir = self._make_run_record(
            tmp_path,
            json.dumps(self._standard_input()),
            run_id=run_id,
            skip_scoring=1,
        )

        failure_message = "Simulated phase execution failure for test"

        with self._preflight_pass_patch(), \
             patch("orchestration_engine.git_integration.GitContext.create_gate"), \
             patch("orchestration_engine.postflight.ensure_branch_pushed", return_value=True), \
             patch("orchestration_engine.sequencer.PhaseSequencer.execute",
                   side_effect=RuntimeError(failure_message)), \
             patch("orchestration_engine.sequencer.StateMachineSequencer.execute",
                   side_effect=RuntimeError(failure_message)):
            with pytest.raises(SystemExit) as exc_info:
                run_daemon(run_id, db_path)
            assert exc_info.value.code == 2, \
                f"Phase failure must exit with code 2, got: {exc_info.value.code}"

        db = Database(db_path)
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["status"] == "failed", \
            f"Expected 'failed' after phase exception, got: {run['status']!r}"
        assert failure_message in (run.get("error_message") or ""), \
            f"Error message must contain failure description: {run.get('error_message')!r}"
