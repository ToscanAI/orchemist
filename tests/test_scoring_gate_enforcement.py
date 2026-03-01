"""Tests for Issue #289: persist scoring results in gate file and enforce score gate.

Covers:
- _write_gate_file includes scoring_status/scoring_score fields (default None)
- update_gate_scoring() persists scoring results to gate file
- update_gate_status_scoring() persists status + scoring in one write
- orch gate approve blocks when scoring_status == 'failed' (no --force)
- orch gate approve succeeds with --force even when scoring failed
- orch gate approve succeeds when scoring_status == 'passed'
- orch gate approve warns but succeeds when scoring_status is None
- orch gate info shows scoring fields
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.git_integration import GitConfig, GitContext, GitError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list, cwd: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        **kw,
    )


@pytest.fixture()
def gates_dir(tmp_path: Path):
    """Patch GitContext.GATES_DIR to an isolated temp directory."""
    original = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    GitContext.GATES_DIR.mkdir(parents=True, exist_ok=True)
    yield GitContext.GATES_DIR
    GitContext.GATES_DIR = original


def _write_test_gate(
    gates_dir: Path,
    run_id: str = "abc12345",
    status: str = "awaiting_approval",
    scoring_status: Any = None,
    scoring_score: Any = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """Write a minimal gate JSON file and return the data dict."""
    gate_data: Dict[str, Any] = {
        "run_id": run_id,
        "pipeline_id": "test-pipeline",
        "status": status,
        "branch": f"feat/test-{run_id}",
        "base_branch": "main",
        "diff_stats": "+10 -2 across 3 files",
        "commits": [],
        "output_dir": output_dir or str(gates_dir.parent / "output"),
        "created_at": "2026-03-01T13:00:00+00:00",
        "approve_command": f"orch gate approve {run_id}",
        "reject_command": f"orch gate reject {run_id}",
        "create_pr": False,
        "scoring_status": scoring_status,
        "scoring_score": scoring_score,
    }
    (gates_dir / f"{run_id}.json").write_text(json.dumps(gate_data))
    return gate_data


# ---------------------------------------------------------------------------
# Tests: _write_gate_file includes scoring fields
# ---------------------------------------------------------------------------


class TestWriteGateFileIncludesScoringFields:
    """Gate files written by _write_gate_file must include scoring_status/score."""

    def test_gate_file_has_scoring_fields_default_none(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """New gate files must contain scoring_status=None and scoring_score=None."""
        # Create a minimal git repo so we can call on_pipeline_complete
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        _git(["config", "user.email", "test@example.com"], cwd=repo)
        _git(["config", "user.name", "Test User"], cwd=repo)
        (repo / "README.md").write_text("init\n")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "init"], cwd=repo)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        cfg = GitConfig(
            enabled=True,
            branch_pattern="feat/test-{run_id}",
            push=False,
            merge_gate=True,
            working_dir=str(repo),
        )
        ctx = GitContext(cfg, pipeline_id="test-pipe", run_id="run001", output_dir=output_dir)
        ctx.on_pipeline_start()
        ctx.on_pipeline_complete(success=True)

        gate_file = output_dir / "_gate.json"
        assert gate_file.exists(), "Gate file not created in output_dir"
        gate_data = json.loads(gate_file.read_text())

        assert "scoring_status" in gate_data, "scoring_status key missing from gate file"
        assert "scoring_score" in gate_data, "scoring_score key missing from gate file"
        assert gate_data["scoring_status"] is None
        assert gate_data["scoring_score"] is None


# ---------------------------------------------------------------------------
# Tests: update_gate_scoring()
# ---------------------------------------------------------------------------


class TestUpdateGateScoring:
    """update_gate_scoring() must persist scoring results correctly."""

    def test_update_scoring_passed(self, tmp_path: Path, gates_dir: Path) -> None:
        """Scoring passed — update_gate_scoring writes passed status and score."""
        _write_test_gate(gates_dir, run_id="run001")

        result = GitContext.update_gate_scoring("run001", "passed", 0.87)

        assert result is not None
        assert result["scoring_status"] == "passed"
        assert result["scoring_score"] == pytest.approx(0.87)
        assert "updated_at" in result

        # Verify persisted to disk
        reloaded = GitContext.load_gate("run001")
        assert reloaded is not None
        assert reloaded["scoring_status"] == "passed"
        assert reloaded["scoring_score"] == pytest.approx(0.87)

    def test_update_scoring_failed(self, tmp_path: Path, gates_dir: Path) -> None:
        """Scoring failed — update_gate_scoring writes failed status."""
        _write_test_gate(gates_dir, run_id="run002")

        result = GitContext.update_gate_scoring("run002", "failed", 0.42)

        assert result is not None
        assert result["scoring_status"] == "failed"
        assert result["scoring_score"] == pytest.approx(0.42)

    def test_update_scoring_error(self, tmp_path: Path, gates_dir: Path) -> None:
        """Scoring error — update_gate_scoring writes error status with None score."""
        _write_test_gate(gates_dir, run_id="run003")

        result = GitContext.update_gate_scoring("run003", "error", None)

        assert result is not None
        assert result["scoring_status"] == "error"
        assert result["scoring_score"] is None

    def test_update_scoring_missing_gate_returns_none(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """update_gate_scoring returns None for a non-existent run_id."""
        result = GitContext.update_gate_scoring("no-such-run", "passed", 0.9)
        assert result is None

    def test_update_scoring_updates_output_dir_copy(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """update_gate_scoring also updates the _gate.json in output_dir."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        # Write an output dir copy of the gate file
        gate_initial = {
            "run_id": "run004",
            "pipeline_id": "pipe",
            "status": "awaiting_approval",
            "branch": "feat/run004",
            "base_branch": "main",
            "scoring_status": None,
            "scoring_score": None,
            "output_dir": str(output_dir),
        }
        (output_dir / "_gate.json").write_text(json.dumps(gate_initial))
        (gates_dir / "run004.json").write_text(
            json.dumps({**gate_initial, "commits": [], "diff_stats": "n/a"})
        )

        GitContext.update_gate_scoring("run004", "passed", 0.95)

        out_gate = json.loads((output_dir / "_gate.json").read_text())
        assert out_gate["scoring_status"] == "passed"
        assert out_gate["scoring_score"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Tests: update_gate_status_scoring()
# ---------------------------------------------------------------------------


class TestUpdateGateStatusScoring:
    """update_gate_status_scoring() updates both status and scoring in one write."""

    def test_combined_update(self, tmp_path: Path, gates_dir: Path) -> None:
        """update_gate_status_scoring writes status + scoring in one shot."""
        _write_test_gate(gates_dir, run_id="run010", status="awaiting_approval")

        result = GitContext.update_gate_status_scoring(
            "run010", "approved", "passed", 0.80, message="All good"
        )

        assert result is not None
        assert result["status"] == "approved"
        assert result["scoring_status"] == "passed"
        assert result["scoring_score"] == pytest.approx(0.80)
        assert result["message"] == "All good"

    def test_combined_update_failed_scoring(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """update_gate_status_scoring handles failed scoring correctly."""
        _write_test_gate(gates_dir, run_id="run011", status="awaiting_approval")

        result = GitContext.update_gate_status_scoring(
            "run011", "awaiting_approval", "failed", 0.45
        )

        assert result is not None
        assert result["scoring_status"] == "failed"
        assert result["scoring_score"] == pytest.approx(0.45)

    def test_combined_update_missing_gate_returns_none(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        result = GitContext.update_gate_status_scoring(
            "no-such-run", "approved", "passed", 0.9
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests: orch gate approve score gate enforcement
# ---------------------------------------------------------------------------


class TestGateApproveScoreEnforcement:
    """orch gate approve must block when scoring_status=failed (without --force)."""

    def _run(self, *args: str) -> any:
        runner = CliRunner()
        return runner.invoke(main, list(args), catch_exceptions=False)

    def test_approve_blocked_when_scoring_failed(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """gate approve exits with code 1 and error message when scoring failed."""
        _write_test_gate(
            gates_dir,
            run_id="fail001",
            status="awaiting_approval",
            scoring_status="failed",
            scoring_score=0.42,
        )

        result = self._run("gate", "approve", "fail001")

        assert result.exit_code == 1
        assert "Score gate FAILED" in result.output
        assert "approval blocked" in result.output
        assert "42.0 / 100" in result.output
        assert "--force" in result.output

    def test_approve_allowed_with_force_when_scoring_failed(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """gate approve --force succeeds even when scoring failed."""
        _write_test_gate(
            gates_dir,
            run_id="fail002",
            status="awaiting_approval",
            scoring_status="failed",
            scoring_score=0.35,
        )

        result = self._run("gate", "approve", "--force", "fail002")

        assert result.exit_code == 0
        assert "approving anyway" in result.output.lower() or "force" in result.output.lower()
        # Gate should be updated to approved
        gate = GitContext.load_gate("fail002")
        assert gate is not None
        assert gate["status"] == "approved"

    def test_approve_allowed_when_scoring_passed(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """gate approve succeeds (no warning) when scoring passed."""
        _write_test_gate(
            gates_dir,
            run_id="pass001",
            status="awaiting_approval",
            scoring_status="passed",
            scoring_score=0.88,
        )

        result = self._run("gate", "approve", "pass001")

        assert result.exit_code == 0
        assert "FAILED" not in result.output
        # Gate should be approved
        gate = GitContext.load_gate("pass001")
        assert gate is not None
        assert gate["status"] == "approved"

    def test_approve_warns_when_no_scoring_data(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """gate approve shows a warning (but succeeds) when no scoring data available."""
        _write_test_gate(
            gates_dir,
            run_id="noscore001",
            status="awaiting_approval",
            scoring_status=None,
            scoring_score=None,
        )

        result = self._run("gate", "approve", "noscore001")

        # Should succeed with warning
        assert result.exit_code == 0
        assert "No scoring data" in result.output or "without score gate" in result.output
        gate = GitContext.load_gate("noscore001")
        assert gate is not None
        assert gate["status"] == "approved"

    def test_approve_force_short_flag_works(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        """gate approve -f (short flag) also overrides score gate."""
        _write_test_gate(
            gates_dir,
            run_id="fail003",
            status="awaiting_approval",
            scoring_status="failed",
            scoring_score=0.20,
        )

        result = self._run("gate", "approve", "-f", "fail003")

        assert result.exit_code == 0
        gate = GitContext.load_gate("fail003")
        assert gate is not None
        assert gate["status"] == "approved"


# ---------------------------------------------------------------------------
# Tests: orch gate info shows scoring fields
# ---------------------------------------------------------------------------


class TestGateInfoScoringDisplay:
    """orch gate info must display scoring_status and scoring_score."""

    def _run(self, *args: str) -> any:
        runner = CliRunner()
        return runner.invoke(main, list(args), catch_exceptions=False)

    def test_gate_info_shows_scoring_passed(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        _write_test_gate(
            gates_dir,
            run_id="info001",
            scoring_status="passed",
            scoring_score=0.92,
        )
        result = self._run("gate", "info", "info001")
        assert result.exit_code == 0
        assert "passed" in result.output
        assert "92.0" in result.output

    def test_gate_info_shows_scoring_failed(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        _write_test_gate(
            gates_dir,
            run_id="info002",
            scoring_status="failed",
            scoring_score=0.55,
        )
        result = self._run("gate", "info", "info002")
        assert result.exit_code == 0
        assert "failed" in result.output
        assert "55.0" in result.output

    def test_gate_info_shows_pending_when_no_scoring(
        self, tmp_path: Path, gates_dir: Path
    ) -> None:
        _write_test_gate(
            gates_dir,
            run_id="info003",
            scoring_status=None,
            scoring_score=None,
        )
        result = self._run("gate", "info", "info003")
        assert result.exit_code == 0
        assert "pending" in result.output.lower() or "not yet scored" in result.output.lower()
