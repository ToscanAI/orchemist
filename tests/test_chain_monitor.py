"""Tests for Issue #508 — Chain Monitoring CLI (``orch chain``).

Covers:
- DB layer: get_full_chain (AC-DB-01 through AC-DB-05)
- DB layer: list_active_chain_roots (AC-DB-06 through AC-DB-11)
- chain_monitor module functions (AC-CM-01 through AC-CM-20)
- CLI integration: ``orch chain`` command (AC-CLI-01 through AC-CLI-08)
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from orchestration_engine.db import Database
from orchestration_engine import chain_monitor
from orchestration_engine.chain_monitor import (
    find_chain_root,
    get_issue_for_run,
    compute_elapsed,
    _fmt_elapsed,
    format_chain_row,
    build_chain_display,
    build_active_chains_display,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_db() -> Database:
    """Return an in-memory Database with all migrations applied."""
    return Database(":memory:")


@pytest.fixture
def cli_runner():
    """Return a Click test runner."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _insert_run(
    db: Database,
    run_id: str,
    parent_run_id: Optional[str] = None,
    chain_depth: int = 0,
    status: str = "pending",
    template_id: str = "test-pipeline",
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    scoring_score: Optional[float] = None,
) -> str:
    """Insert a minimal pipeline_run record and return run_id.

    Also sets started_at / completed_at / scoring_score via update_pipeline_run
    since insert_pipeline_run does not accept those fields.
    """
    run_data = {
        "run_id": run_id,
        "template_path": f"/tmp/{template_id}.yaml",
        "template_id": template_id,
        "input_json": json.dumps({}),
        "mode": "dry-run",
        "output_dir": f"/tmp/output/{run_id}",
        "status": status,
        "gateway_url": None,
        "skip_scoring": 0,
        "parent_run_id": parent_run_id,
        "chain_depth": chain_depth,
    }
    db.insert_pipeline_run(run_data)

    update_kwargs: Dict[str, Any] = {}
    if started_at is not None:
        update_kwargs["started_at"] = started_at
    if completed_at is not None:
        update_kwargs["completed_at"] = completed_at
    if scoring_score is not None:
        update_kwargs["scoring_score"] = scoring_score
    if status != "pending":
        update_kwargs["status"] = status
    if update_kwargs:
        db.update_pipeline_run(run_id, **update_kwargs)

    return run_id


def _insert_issue_map(
    db: Database,
    run_id: str,
    issue_number: int,
    repo: str = "owner/repo",
) -> None:
    """Insert a minimal issue_pipeline_map row linking run_id to issue_number."""
    db.insert_issue_classification({
        "issue_number": issue_number,
        "repo": repo,
        "classification_type": "coding",
        "confidence": 0.9,
        "template_id": "test-pipeline",
        "run_id": run_id,
    })


# ===========================================================================
# DB Layer — get_full_chain
# ===========================================================================


class TestDbGetFullChain:
    def test_get_full_chain_single_run(self, in_memory_db):
        """Root with no children → returns list with exactly 1 run."""
        rid = _insert_run(in_memory_db, "root-aaa", chain_depth=0)
        result = in_memory_db.get_full_chain(rid)
        assert len(result) == 1
        assert result[0]["run_id"] == rid

    def test_get_full_chain_with_children(self, in_memory_db):
        """Root + 2 direct children → returns 3 runs ordered depth then time."""
        root = _insert_run(in_memory_db, "root-bbb", chain_depth=0)
        child1 = _insert_run(in_memory_db, "child-bbb-1", parent_run_id=root, chain_depth=1)
        child2 = _insert_run(in_memory_db, "child-bbb-2", parent_run_id=root, chain_depth=1)
        result = in_memory_db.get_full_chain(root)
        assert len(result) == 3
        run_ids = [r["run_id"] for r in result]
        assert run_ids[0] == root
        assert set(run_ids[1:]) == {child1, child2}

    def test_get_full_chain_deep_chain(self, in_memory_db):
        """3-level chain (root→child→grandchild) → returns 3 runs in depth order."""
        root = _insert_run(in_memory_db, "root-ccc", chain_depth=0)
        child = _insert_run(in_memory_db, "child-ccc", parent_run_id=root, chain_depth=1)
        grandchild = _insert_run(in_memory_db, "grandchild-ccc", parent_run_id=child, chain_depth=2)
        result = in_memory_db.get_full_chain(root)
        assert len(result) == 3
        assert result[0]["run_id"] == root
        assert result[1]["run_id"] == child
        assert result[2]["run_id"] == grandchild

    def test_get_full_chain_not_found(self, in_memory_db):
        """Unknown run_id → returns empty list."""
        result = in_memory_db.get_full_chain("does-not-exist")
        assert result == []

    def test_get_full_chain_branching(self, in_memory_db):
        """Root with 2 children each with 1 grandchild → returns 5 runs."""
        root = _insert_run(in_memory_db, "root-ddd", chain_depth=0)
        c1 = _insert_run(in_memory_db, "child-ddd-1", parent_run_id=root, chain_depth=1)
        c2 = _insert_run(in_memory_db, "child-ddd-2", parent_run_id=root, chain_depth=1)
        gc1 = _insert_run(in_memory_db, "gc-ddd-1", parent_run_id=c1, chain_depth=2)
        gc2 = _insert_run(in_memory_db, "gc-ddd-2", parent_run_id=c2, chain_depth=2)
        result = in_memory_db.get_full_chain(root)
        assert len(result) == 5
        run_ids = {r["run_id"] for r in result}
        assert run_ids == {root, c1, c2, gc1, gc2}


# ===========================================================================
# DB Layer — list_active_chain_roots
# ===========================================================================


class TestDbListActiveChainRoots:
    def test_list_active_chain_roots_empty(self, in_memory_db):
        """No runs in DB → returns empty list."""
        result = in_memory_db.list_active_chain_roots()
        assert result == []

    def test_list_active_chain_roots_all_terminal(self, in_memory_db):
        """All runs in terminal state → returns empty list."""
        _insert_run(in_memory_db, "run-term-1", status="success", chain_depth=0)
        _insert_run(in_memory_db, "run-term-2", status="failed", chain_depth=0)
        result = in_memory_db.list_active_chain_roots()
        assert result == []

    def test_list_active_chain_roots_single_active_root(self, in_memory_db):
        """Single non-terminal root (no parent) → returned as active chain root."""
        root = _insert_run(in_memory_db, "run-active-root", status="running", chain_depth=0)
        result = in_memory_db.list_active_chain_roots()
        assert len(result) == 1
        assert result[0]["run_id"] == root

    def test_list_active_chain_roots_active_child(self, in_memory_db):
        """Terminal root + active child → root is returned (has active descendant)."""
        root = _insert_run(in_memory_db, "root-eee", status="success", chain_depth=0)
        _insert_run(in_memory_db, "child-eee", parent_run_id=root, status="running", chain_depth=1)
        result = in_memory_db.list_active_chain_roots()
        assert len(result) == 1
        assert result[0]["run_id"] == root

    def test_list_active_chain_roots_excludes_root_with_all_terminal_chain(self, in_memory_db):
        """Root is terminal, all children terminal → not returned."""
        root = _insert_run(in_memory_db, "root-fff", status="success", chain_depth=0)
        _insert_run(in_memory_db, "child-fff", parent_run_id=root, status="failed", chain_depth=1)
        result = in_memory_db.list_active_chain_roots()
        assert result == []

    def test_list_active_chain_roots_limit(self, in_memory_db):
        """More active roots than limit → respects limit."""
        for i in range(5):
            _insert_run(in_memory_db, f"run-limit-{i}", status="running", chain_depth=0)
        result = in_memory_db.list_active_chain_roots(limit=3)
        assert len(result) == 3


# ===========================================================================
# chain_monitor — find_chain_root
# ===========================================================================


class TestFindChainRoot:
    def test_find_chain_root_is_root(self, in_memory_db):
        """Run with no parent returns itself."""
        _insert_run(in_memory_db, "solo-root", chain_depth=0)
        result = find_chain_root(in_memory_db, "solo-root")
        assert result is not None
        assert result["run_id"] == "solo-root"

    def test_find_chain_root_from_child(self, in_memory_db):
        """Child run returns root run dict."""
        _insert_run(in_memory_db, "root-ggg", chain_depth=0)
        _insert_run(in_memory_db, "child-ggg", parent_run_id="root-ggg", chain_depth=1)
        result = find_chain_root(in_memory_db, "child-ggg")
        assert result is not None
        assert result["run_id"] == "root-ggg"

    def test_find_chain_root_not_found(self, in_memory_db):
        """Unknown run_id returns None."""
        result = find_chain_root(in_memory_db, "nonexistent-run")
        assert result is None

    def test_find_chain_root_deep(self, in_memory_db):
        """3-level chain: given leaf returns root."""
        _insert_run(in_memory_db, "root-hhh", chain_depth=0)
        _insert_run(in_memory_db, "child-hhh", parent_run_id="root-hhh", chain_depth=1)
        _insert_run(in_memory_db, "leaf-hhh", parent_run_id="child-hhh", chain_depth=2)
        result = find_chain_root(in_memory_db, "leaf-hhh")
        assert result is not None
        assert result["run_id"] == "root-hhh"


# ===========================================================================
# chain_monitor — get_issue_for_run
# ===========================================================================


class TestGetIssueForRun:
    def test_get_issue_for_run_found(self, in_memory_db):
        """Insert row in issue_pipeline_map → returns issue_number."""
        _insert_run(in_memory_db, "run-issue-1", chain_depth=0)
        _insert_issue_map(in_memory_db, "run-issue-1", issue_number=42)
        result = get_issue_for_run(in_memory_db, "run-issue-1")
        assert result == 42

    def test_get_issue_for_run_not_found(self, in_memory_db):
        """No row in issue_pipeline_map → returns None."""
        _insert_run(in_memory_db, "run-no-issue", chain_depth=0)
        result = get_issue_for_run(in_memory_db, "run-no-issue")
        assert result is None


# ===========================================================================
# chain_monitor — compute_elapsed
# ===========================================================================


class TestComputeElapsed:
    def test_compute_elapsed_no_start(self):
        """Run with no started_at → returns None."""
        run: Dict[str, Any] = {"run_id": "x", "started_at": None}
        assert compute_elapsed(run) is None

    def test_compute_elapsed_completed(self):
        """Run with started_at + completed_at → returns correct seconds."""
        start = "2024-01-01T10:00:00"
        end = "2024-01-01T10:01:30"
        run: Dict[str, Any] = {"started_at": start, "completed_at": end}
        elapsed = compute_elapsed(run)
        assert elapsed == pytest.approx(90.0)

    def test_compute_elapsed_in_progress(self):
        """Run with started_at, no completed_at → returns positive float."""
        # Use a start time well in the past so elapsed is always > 0
        start = "2020-01-01T00:00:00"
        run: Dict[str, Any] = {"started_at": start, "completed_at": None}
        elapsed = compute_elapsed(run)
        assert elapsed is not None
        assert elapsed > 0.0

    def test_compute_elapsed_iso_string(self):
        """Handles ISO-8601 strings (not datetime objects)."""
        run: Dict[str, Any] = {
            "started_at": "2024-06-01T08:00:00",
            "completed_at": "2024-06-01T08:00:45",
        }
        assert compute_elapsed(run) == pytest.approx(45.0)

    def test_compute_elapsed_datetime_objects(self):
        """Handles datetime objects stored in run dict."""
        start_dt = datetime(2024, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
        end_dt = datetime(2024, 6, 1, 8, 1, 0, tzinfo=timezone.utc)
        run: Dict[str, Any] = {"started_at": start_dt, "completed_at": end_dt}
        assert compute_elapsed(run) == pytest.approx(60.0)


# ===========================================================================
# chain_monitor — _fmt_elapsed
# ===========================================================================


class TestFmtElapsed:
    def test_fmt_elapsed_seconds(self):
        assert _fmt_elapsed(45) == "45s"

    def test_fmt_elapsed_minutes(self):
        assert _fmt_elapsed(90) == "1m 30s"

    def test_fmt_elapsed_hours(self):
        assert _fmt_elapsed(3661) == "1h 1m 1s"

    def test_fmt_elapsed_none(self):
        assert _fmt_elapsed(None) == "\u2014"

    def test_fmt_elapsed_zero(self):
        assert _fmt_elapsed(0) == "0s"

    def test_fmt_elapsed_exactly_60(self):
        assert _fmt_elapsed(60) == "1m 0s"


# ===========================================================================
# chain_monitor — format_chain_row
# ===========================================================================


class TestFormatChainRow:
    def _make_run(self, **kwargs) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "run_id": "abcdef1234567890",
            "status": "running",
            "template_id": "my-template",
            "scoring_score": None,
            "started_at": None,
            "completed_at": None,
            "chain_depth": 0,
        }
        base.update(kwargs)
        return base

    def test_format_chain_row_all_fields(self):
        """Run with score, issue, elapsed → correctly formatted string."""
        run = self._make_run(
            started_at="2024-01-01T10:00:00",
            completed_at="2024-01-01T10:01:30",
            scoring_score=0.875,
        )
        line = format_chain_row(run, issue_number=508, depth=0)
        assert "abcdef12" in line
        assert "#508" in line
        assert "0.875" in line
        assert "90s" in line or "1m 30s" in line
        assert "my-template" in line

    def test_format_chain_row_missing_score(self):
        """scoring_score=None → score shows em dash."""
        run = self._make_run(scoring_score=None)
        line = format_chain_row(run, issue_number=1, depth=0)
        assert "\u2014" in line

    def test_format_chain_row_missing_issue(self):
        """issue_number=None → issue shows em dash."""
        run = self._make_run()
        line = format_chain_row(run, issue_number=None, depth=0)
        assert "\u2014" in line

    def test_format_chain_row_indentation(self):
        """depth=2 → row starts with 4 spaces (2 per level)."""
        run = self._make_run()
        line = format_chain_row(run, issue_number=None, depth=2)
        assert line.startswith("    ")

    def test_format_chain_row_zero_score(self):
        """scoring_score=0.0 is falsy but valid → displays '0.000' not '—'."""
        run = self._make_run(scoring_score=0.0)
        line = format_chain_row(run, issue_number=1, depth=0)
        assert "0.000" in line

    def test_format_chain_row_depth_zero_no_indent(self):
        """depth=0 → no leading spaces."""
        run = self._make_run()
        line = format_chain_row(run, issue_number=None, depth=0)
        assert not line.startswith(" ")

    def test_format_chain_row_template_truncated(self):
        """template_id longer than 24 chars → truncated to 24 chars."""
        long_template = "a" * 40
        run = self._make_run(template_id=long_template)
        line = format_chain_row(run, issue_number=None, depth=0)
        # The template portion should not contain the full 40-char name
        assert "a" * 40 not in line
        assert "a" * 24 in line


# ===========================================================================
# chain_monitor — build_chain_display
# ===========================================================================


class TestBuildChainDisplay:
    def test_build_chain_display_not_found(self, in_memory_db):
        """Unknown root_run_id → error message string."""
        result = build_chain_display(in_memory_db, "nonexistent-xyz")
        assert "not found" in result.lower()

    def test_build_chain_display_single(self, in_memory_db):
        """Single run → header + separator + 1 data line."""
        _insert_run(in_memory_db, "solo-display", chain_depth=0)
        result = build_chain_display(in_memory_db, "solo-display")
        lines = result.splitlines()
        # header, separator, 1 data line = at least 3 lines
        assert len(lines) >= 3
        assert "RUN ID" in lines[0]
        assert "solo-di" in result  # first 8 chars of run_id

    def test_build_chain_display_full_chain(self, in_memory_db):
        """3-run chain → 3 data lines, child is indented."""
        _insert_run(in_memory_db, "root-disp", chain_depth=0)
        _insert_run(in_memory_db, "child-disp", parent_run_id="root-disp", chain_depth=1)
        _insert_run(in_memory_db, "gc-disp", parent_run_id="child-disp", chain_depth=2)
        result = build_chain_display(in_memory_db, "root-disp")
        lines = result.splitlines()
        # 2 header lines + 3 data lines
        data_lines = lines[2:]
        assert len(data_lines) == 3
        # child line (depth=1) has 2-space indent
        assert data_lines[1].startswith("  ")
        # grandchild line (depth=2) has 4-space indent
        assert data_lines[2].startswith("    ")
        # root line has no indent
        assert not data_lines[0].startswith(" ")


# ===========================================================================
# chain_monitor — build_active_chains_display
# ===========================================================================


class TestBuildActiveChainsDisplay:
    def test_build_active_chains_display_empty(self, in_memory_db):
        """No active chains → 'No active chains found.'"""
        result = build_active_chains_display(in_memory_db)
        assert result == "No active chains found."

    def test_build_active_chains_display_with_roots(self, in_memory_db):
        """Two active roots → 2 data lines."""
        _insert_run(in_memory_db, "active-root-1", status="running", chain_depth=0)
        _insert_run(in_memory_db, "active-root-2", status="running", chain_depth=0)
        result = build_active_chains_display(in_memory_db)
        lines = result.splitlines()
        data_lines = lines[2:]  # skip header + separator
        assert len(data_lines) == 2

    def test_build_active_chains_display_all_terminal(self, in_memory_db):
        """All terminal runs → 'No active chains found.'"""
        _insert_run(in_memory_db, "done-run", status="success", chain_depth=0)
        result = build_active_chains_display(in_memory_db)
        assert result == "No active chains found."


# ===========================================================================
# CLI Integration — ``orch chain``
# ===========================================================================


class TestCliChain:
    """Integration tests for the ``orch chain`` Click command."""

    def _make_db_path(self, tmp_path) -> str:
        db = Database(tmp_path / "test.db")
        return str(tmp_path / "test.db")

    def test_chain_command_no_args(self, cli_runner, tmp_path):
        """``orch chain`` with no args → UsageError."""
        from orchestration_engine.cli import main
        db_path = self._make_db_path(tmp_path)
        result = cli_runner.invoke(main, ["chain", "--db-path", db_path])
        assert result.exit_code != 0
        assert "RUN_ID" in result.output or "active" in result.output.lower() or result.exception

    def test_chain_command_both_args(self, cli_runner, tmp_path):
        """``orch chain run-id --active`` → UsageError."""
        from orchestration_engine.cli import main
        db_path = self._make_db_path(tmp_path)
        result = cli_runner.invoke(main, ["chain", "some-run-id", "--active", "--db-path", db_path])
        assert result.exit_code != 0

    def test_chain_command_run_not_found(self, cli_runner, tmp_path):
        """``orch chain unknown-id`` → exits 1."""
        from orchestration_engine.cli import main
        db_path = self._make_db_path(tmp_path)
        result = cli_runner.invoke(main, ["chain", "unknown-run-id", "--db-path", db_path])
        assert result.exit_code == 1

    def test_chain_command_shows_root(self, cli_runner, tmp_path):
        """Root run → single row in output."""
        from orchestration_engine.cli import main
        db = Database(tmp_path / "test.db")
        _insert_run(db, "root-cli-1", chain_depth=0)
        result = cli_runner.invoke(
            main, ["chain", "root-cli-1", "--db-path", str(tmp_path / "test.db")]
        )
        assert result.exit_code == 0
        assert "root-cl" in result.output  # first 8 chars

    def test_chain_command_shows_chain(self, cli_runner, tmp_path):
        """Parent+child runs → both in output, child indented."""
        from orchestration_engine.cli import main
        db = Database(tmp_path / "test.db")
        _insert_run(db, "root-cli-2", chain_depth=0)
        _insert_run(db, "child-cli-2", parent_run_id="root-cli-2", chain_depth=1)
        result = cli_runner.invoke(
            main, ["chain", "root-cli-2", "--db-path", str(tmp_path / "test.db")]
        )
        assert result.exit_code == 0
        assert "root-cl" in result.output
        assert "child-c" in result.output
        # Child line should be indented (contains leading spaces after header)
        lines = result.output.splitlines()
        data_lines = [l for l in lines if "child-c" in l]
        assert data_lines
        assert data_lines[0].startswith("  ")

    def test_chain_command_from_child(self, cli_runner, tmp_path):
        """Given child run_id → '(Showing chain from root:)' line printed."""
        from orchestration_engine.cli import main
        db = Database(tmp_path / "test.db")
        _insert_run(db, "root-cli-3", chain_depth=0)
        _insert_run(db, "child-cli-3", parent_run_id="root-cli-3", chain_depth=1)
        result = cli_runner.invoke(
            main, ["chain", "child-cli-3", "--db-path", str(tmp_path / "test.db")]
        )
        assert result.exit_code == 0
        assert "Showing chain from root" in result.output
        assert "root-cli-3" in result.output

    def test_chain_command_active_empty(self, cli_runner, tmp_path):
        """``--active`` with no active runs → 'No active chains found.'"""
        from orchestration_engine.cli import main
        db_path = self._make_db_path(tmp_path)
        result = cli_runner.invoke(main, ["chain", "--active", "--db-path", db_path])
        assert result.exit_code == 0
        assert "No active chains found." in result.output

    def test_chain_command_active_with_run(self, cli_runner, tmp_path):
        """``--active`` with one active run → run appears in output."""
        from orchestration_engine.cli import main
        db = Database(tmp_path / "test.db")
        _insert_run(db, "active-chain-root", status="running", chain_depth=0)
        result = cli_runner.invoke(
            main, ["chain", "--active", "--db-path", str(tmp_path / "test.db")]
        )
        assert result.exit_code == 0
        assert "active-c" in result.output  # first 8 chars of run_id

    def test_chain_command_db_path_override(self, cli_runner, tmp_path):
        """``--db-path`` override is respected."""
        from orchestration_engine.cli import main
        custom_db_path = tmp_path / "custom.db"
        db = Database(custom_db_path)
        _insert_run(db, "run-custom-db", chain_depth=0)
        result = cli_runner.invoke(
            main, ["chain", "run-custom-db", "--db-path", str(custom_db_path)]
        )
        assert result.exit_code == 0
        assert "run-cust" in result.output
