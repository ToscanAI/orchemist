"""tests/test_review_outcomes.py — Tests for Issue #4.1.2: Review Outcome Tracking.

Covers:
- ReviewOutcome dataclass in review_parser.py
- DB table creation and migration (014_add_review_outcomes_table)
- insert_review_outcome, get_review_outcomes_for_run, list_review_outcomes
- issues_found JSON parsing in _row_to_dict
- PhaseSequencer._record_review_outcome wiring (sequential and parallel)

Test classes:
    TestReviewOutcomeDataclass   — shape, defaults, __post_init__, to_dict
    TestReviewOutcomeDbTable     — table/index created in fresh & migrated DBs
    TestInsertReviewOutcome      — insert one row, return rowid
    TestGetReviewOutcomesForRun  — filter by run_id, order by created_at ASC
    TestListReviewOutcomes       — global listing, pagination, order DESC
    TestIssuesFoundJsonParsing   — _row_to_dict auto-deserialises issues_found
    TestRecordReviewOutcomeWiring — sequencer wires recording for review phases
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from orchestration_engine.db import Database
from orchestration_engine.review_parser import ReviewOutcome, ReviewResult, Severity, parse_review_output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return an in-memory Database with all migrations applied."""
    return Database(":memory:")


def _make_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:8]}"


# #862: route through the canonical helper so a future schema column is
# automatically picked up via insert_pipeline_run's column-default behaviour.
def _insert_pipeline_run(db: Database, run_id: str) -> None:
    """Insert a minimal pipeline_run row so FK constraints pass."""
    from tests._helpers import insert_pipeline_run as _impl
    _impl(
        db,
        run_id=run_id,
        template_path="/tmp/test.yaml",
        output_dir="/tmp/output",
    )


def _outcome_data(run_id: str, phase_id: str = "review", **kwargs) -> Dict[str, Any]:
    """Build a minimal outcome dict for insert_review_outcome."""
    base: Dict[str, Any] = {
        "review_id": str(uuid.uuid4()),
        "run_id": run_id,
        "phase_id": phase_id,
        "reviewer_model": "opus",
        "verdict": "APPROVE",
        "issues_found": [],
        "fix_verified": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(kwargs)
    return base


# ===========================================================================
# TestReviewOutcomeDataclass
# ===========================================================================


class TestReviewOutcomeDataclass(unittest.TestCase):
    """Shape, defaults, __post_init__, and to_dict for ReviewOutcome."""

    def test_exported_in_all(self) -> None:
        from orchestration_engine import review_parser
        self.assertIn("ReviewOutcome", review_parser.__all__)

    def test_fields_present(self) -> None:
        """All expected fields are present in the dataclass."""
        names = {f.name for f in fields(ReviewOutcome)}
        expected = {"review_id", "run_id", "phase_id", "reviewer_model",
                    "verdict", "issues_found", "fix_verified", "created_at"}
        self.assertEqual(expected, names)

    def test_fix_verified_default(self) -> None:
        """fix_verified defaults to False."""
        outcome = ReviewOutcome(
            review_id="r1",
            run_id="run1",
            phase_id="review",
            reviewer_model="opus",
            verdict="APPROVE",
            issues_found=[],
        )
        self.assertFalse(outcome.fix_verified)

    def test_created_at_auto_set(self) -> None:
        """created_at is populated by __post_init__ when not supplied."""
        outcome = ReviewOutcome(
            review_id="r2",
            run_id="run2",
            phase_id="review",
            reviewer_model="opus",
            verdict=None,
            issues_found=[],
        )
        self.assertIsNotNone(outcome.created_at)
        # Must be parseable as ISO-8601
        datetime.fromisoformat(outcome.created_at)  # raises on bad format

    def test_created_at_explicit(self) -> None:
        """When created_at is provided it is not overwritten."""
        ts = "2024-01-01T00:00:00"
        outcome = ReviewOutcome(
            review_id="r3",
            run_id="run3",
            phase_id="review",
            reviewer_model="sonnet",
            verdict="REQUEST_CHANGES",
            issues_found=[],
            created_at=ts,
        )
        self.assertEqual(ts, outcome.created_at)

    def test_to_dict_structure(self) -> None:
        """to_dict returns a plain dict with all expected keys."""
        outcome = ReviewOutcome(
            review_id="r4",
            run_id="run4",
            phase_id="review",
            reviewer_model="haiku",
            verdict="APPROVE",
            issues_found=[{"severity": "MINOR", "category": "style", "description": "foo"}],
            created_at="2024-06-01T12:00:00",
        )
        d = outcome.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["review_id"], "r4")
        self.assertEqual(d["verdict"], "APPROVE")
        self.assertIsInstance(d["issues_found"], list)
        self.assertFalse(d["fix_verified"])

    def test_reviewer_model_optional(self) -> None:
        """reviewer_model can be None (unknown model)."""
        outcome = ReviewOutcome(
            review_id="r5",
            run_id="run5",
            phase_id="review",
            reviewer_model=None,
            verdict=None,
            issues_found=[],
        )
        self.assertIsNone(outcome.reviewer_model)


# ===========================================================================
# TestReviewOutcomeDbTable
# ===========================================================================


class TestReviewOutcomeDbTable(unittest.TestCase):
    """review_outcomes table and index exist on fresh and migrated databases."""

    def test_table_exists_on_fresh_db(self) -> None:
        db = _make_db()
        conn = db.get_connection()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='review_outcomes'"
        )
        self.assertIsNotNone(cursor.fetchone(), "review_outcomes table not found")

    def test_index_exists(self) -> None:
        db = _make_db()
        conn = db.get_connection()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_review_outcomes_run_id'"
        )
        self.assertIsNotNone(cursor.fetchone(), "idx_review_outcomes_run_id index not found")

    def test_migration_014_recorded(self) -> None:
        """Migration 014 appears in the migrations table."""
        db = _make_db()
        conn = db.get_connection()
        cursor = conn.execute(
            "SELECT name FROM migrations WHERE name = '014_add_review_outcomes_table'"
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Migration 014 not found in migrations table")

    def test_table_columns(self) -> None:
        """review_outcomes table has all expected columns."""
        db = _make_db()
        conn = db.get_connection()
        cursor = conn.execute("PRAGMA table_info(review_outcomes)")
        col_names = {row["name"] for row in cursor.fetchall()}
        expected = {"review_id", "run_id", "phase_id", "reviewer_model",
                    "verdict", "issues_found", "fix_verified", "created_at"}
        self.assertTrue(expected.issubset(col_names),
                        f"Missing columns: {expected - col_names}")


# ===========================================================================
# TestInsertReviewOutcome
# ===========================================================================


class TestInsertReviewOutcome(unittest.TestCase):
    """insert_review_outcome inserts one row and returns the rowid."""

    def setUp(self) -> None:
        self.db = _make_db()
        self.run_id = _make_run_id()
        _insert_pipeline_run(self.db, self.run_id)

    def test_returns_integer_rowid(self) -> None:
        data = _outcome_data(self.run_id)
        rowid = self.db.insert_review_outcome(data)
        self.assertIsInstance(rowid, int)
        self.assertGreater(rowid, 0)

    def test_row_persisted(self) -> None:
        data = _outcome_data(self.run_id, verdict="APPROVE")
        self.db.insert_review_outcome(data)
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "APPROVE")

    def test_issues_found_serialised(self) -> None:
        issues = [{"severity": "BLOCKER", "category": "security", "description": "SQL injection"}]
        data = _outcome_data(self.run_id, issues_found=issues)
        self.db.insert_review_outcome(data)
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        self.assertEqual(rows[0]["issues_found"], issues)

    def test_fix_verified_stored(self) -> None:
        data = _outcome_data(self.run_id, fix_verified=True)
        self.db.insert_review_outcome(data)
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        # Stored as 1 in SQLite — may come back as bool or int
        self.assertTrue(rows[0]["fix_verified"])

    def test_null_verdict_allowed(self) -> None:
        data = _outcome_data(self.run_id, verdict=None)
        self.db.insert_review_outcome(data)
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        self.assertIsNone(rows[0]["verdict"])

    def test_null_reviewer_model_allowed(self) -> None:
        data = _outcome_data(self.run_id, reviewer_model=None)
        self.db.insert_review_outcome(data)
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        self.assertIsNone(rows[0]["reviewer_model"])

    def test_unique_review_id_enforced(self) -> None:
        """Inserting duplicate review_id raises an IntegrityError."""
        import sqlite3
        data = _outcome_data(self.run_id, review_id="fixed-uuid")
        self.db.insert_review_outcome(data)
        with self.assertRaises(Exception):
            self.db.insert_review_outcome(data)  # same review_id

    def test_multiple_inserts_same_run(self) -> None:
        """Multiple outcomes for the same run are all persisted."""
        for _ in range(3):
            self.db.insert_review_outcome(_outcome_data(self.run_id))
        rows = self.db.get_review_outcomes_for_run(self.run_id)
        self.assertEqual(len(rows), 3)


# ===========================================================================
# TestGetReviewOutcomesForRun
# ===========================================================================


class TestGetReviewOutcomesForRun(unittest.TestCase):
    """get_review_outcomes_for_run returns rows filtered by run_id, oldest first."""

    def setUp(self) -> None:
        self.db = _make_db()
        self.run_a = _make_run_id()
        self.run_b = _make_run_id()
        _insert_pipeline_run(self.db, self.run_a)
        _insert_pipeline_run(self.db, self.run_b)

    def test_empty_when_no_outcomes(self) -> None:
        rows = self.db.get_review_outcomes_for_run(self.run_a)
        self.assertEqual(rows, [])

    def test_filters_by_run_id(self) -> None:
        self.db.insert_review_outcome(_outcome_data(self.run_a, verdict="APPROVE"))
        self.db.insert_review_outcome(_outcome_data(self.run_b, verdict="REQUEST_CHANGES"))
        rows_a = self.db.get_review_outcomes_for_run(self.run_a)
        rows_b = self.db.get_review_outcomes_for_run(self.run_b)
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["verdict"], "APPROVE")
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["verdict"], "REQUEST_CHANGES")

    def test_returns_list_of_dicts(self) -> None:
        self.db.insert_review_outcome(_outcome_data(self.run_a))
        rows = self.db.get_review_outcomes_for_run(self.run_a)
        self.assertIsInstance(rows, list)
        self.assertIsInstance(rows[0], dict)

    def test_nonexistent_run_returns_empty(self) -> None:
        rows = self.db.get_review_outcomes_for_run("does-not-exist")
        self.assertEqual(rows, [])

    def test_run_id_field_in_row(self) -> None:
        self.db.insert_review_outcome(_outcome_data(self.run_a))
        rows = self.db.get_review_outcomes_for_run(self.run_a)
        self.assertEqual(rows[0]["run_id"], self.run_a)

    def test_phase_id_field_in_row(self) -> None:
        self.db.insert_review_outcome(_outcome_data(self.run_a, phase_id="code_review"))
        rows = self.db.get_review_outcomes_for_run(self.run_a)
        self.assertEqual(rows[0]["phase_id"], "code_review")


# ===========================================================================
# TestListReviewOutcomes
# ===========================================================================


class TestListReviewOutcomes(unittest.TestCase):
    """list_review_outcomes returns global listing, newest first, with pagination."""

    def setUp(self) -> None:
        self.db = _make_db()
        self.run_id = _make_run_id()
        _insert_pipeline_run(self.db, self.run_id)

    def test_empty_when_no_outcomes(self) -> None:
        rows = self.db.list_review_outcomes()
        self.assertEqual(rows, [])

    def test_returns_list(self) -> None:
        self.db.insert_review_outcome(_outcome_data(self.run_id))
        rows = self.db.list_review_outcomes()
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)

    def test_default_limit(self) -> None:
        """Default limit is 50 — inserting 5 rows returns all 5."""
        for _ in range(5):
            self.db.insert_review_outcome(_outcome_data(self.run_id))
        rows = self.db.list_review_outcomes()
        self.assertEqual(len(rows), 5)

    def test_limit_respected(self) -> None:
        for _ in range(10):
            self.db.insert_review_outcome(_outcome_data(self.run_id))
        rows = self.db.list_review_outcomes(limit=3)
        self.assertEqual(len(rows), 3)

    def test_offset_pagination(self) -> None:
        for i in range(6):
            self.db.insert_review_outcome(
                _outcome_data(self.run_id, verdict="APPROVE" if i % 2 == 0 else "REQUEST_CHANGES")
            )
        page1 = self.db.list_review_outcomes(limit=3, offset=0)
        page2 = self.db.list_review_outcomes(limit=3, offset=3)
        self.assertEqual(len(page1), 3)
        self.assertEqual(len(page2), 3)
        # Pages must not overlap on review_id
        ids1 = {r["review_id"] for r in page1}
        ids2 = {r["review_id"] for r in page2}
        self.assertEqual(len(ids1 & ids2), 0)

    def test_issues_found_deserialized(self) -> None:
        """issues_found is a Python list, not a raw JSON string."""
        issues = [{"severity": "MAJOR", "category": "correctness", "description": "bug"}]
        self.db.insert_review_outcome(_outcome_data(self.run_id, issues_found=issues))
        rows = self.db.list_review_outcomes()
        self.assertIsInstance(rows[0]["issues_found"], list)
        self.assertEqual(rows[0]["issues_found"], issues)


# ===========================================================================
# TestIssuesFoundJsonParsing
# ===========================================================================


class TestIssuesFoundJsonParsing(unittest.TestCase):
    """_row_to_dict auto-deserialises issues_found from JSON to a Python list."""

    def test_issues_found_in_json_fields(self) -> None:
        """'issues_found' is in the json_fields list used by _row_to_dict."""
        db = _make_db()
        # We test indirectly: insert a row with a non-empty issues list and
        # confirm the retrieved row has a Python list, not a JSON string.
        run_id = _make_run_id()
        _insert_pipeline_run(db, run_id)
        issues = [
            {"severity": "BLOCKER", "category": "security", "description": "XSS"},
            {"severity": "MINOR", "category": "style", "description": "typo"},
        ]
        db.insert_review_outcome(_outcome_data(run_id, issues_found=issues))
        rows = db.get_review_outcomes_for_run(run_id)
        value = rows[0]["issues_found"]
        self.assertIsInstance(value, list, f"Expected list, got {type(value)}: {value!r}")
        self.assertEqual(value, issues)

    def test_empty_issues_found_deserialised(self) -> None:
        """An empty issues list is returned as [] not '[]'."""
        db = _make_db()
        run_id = _make_run_id()
        _insert_pipeline_run(db, run_id)
        db.insert_review_outcome(_outcome_data(run_id, issues_found=[]))
        rows = db.list_review_outcomes()
        self.assertEqual(rows[0]["issues_found"], [])


# ===========================================================================
# TestRecordReviewOutcomeWiring
# ===========================================================================


class TestRecordReviewOutcomeWiring(unittest.TestCase):
    """PhaseSequencer._record_review_outcome is correctly wired and behaves per spec."""

    def _make_sequencer(self, db=None, run_id=None):
        """Return a minimal PhaseSequencer with mocked internals."""
        from orchestration_engine.sequencer import PhaseSequencer
        from orchestration_engine.templates import PipelineTemplate, PhaseDefinition

        # Build a minimal template
        phase = PhaseDefinition(
            id="review",
            name="Review",
            prompt_template="Review this code.",
            task_type="review",
            model_tier="opus",
        )
        template = PipelineTemplate(
            id="test-pipeline",
            name="Test Pipeline",
            version="1.0",
            description="Test",
            author="test",
            phases=[phase],
        )

        mock_runner = MagicMock()
        seq = PhaseSequencer(
            template=template,
            runner=mock_runner,
            run_id=run_id,
            db=db,
        )
        return seq, phase

    def test_no_op_when_no_db(self) -> None:
        """_record_review_outcome does nothing when db is None."""
        seq, phase = self._make_sequencer(db=None, run_id="run-123")
        # Must not raise even without a db
        result = {"state": "success", "result": {"text": "APPROVE\n"}}
        try:
            seq._record_review_outcome(phase, result)
        except Exception as exc:
            self.fail(f"_record_review_outcome raised with no db: {exc}")

    def test_no_op_when_no_run_id(self) -> None:
        """_record_review_outcome does nothing when run_id is None."""
        db = _make_db()
        seq, phase = self._make_sequencer(db=db, run_id=None)
        result = {"state": "success", "result": {"text": "APPROVE\n"}}
        seq._record_review_outcome(phase, result)
        rows = db.list_review_outcomes()
        self.assertEqual(rows, [])

    def test_no_op_for_non_review_phase(self) -> None:
        """Non-review phases are skipped even when run_id + db are set."""
        from orchestration_engine.templates import PhaseDefinition
        db = _make_db()
        seq, _ = self._make_sequencer(db=db, run_id="run-456")
        build_phase = PhaseDefinition(
            id="build",
            name="Build",
            prompt_template="Build the code.",
            task_type="content",   # not "review"
            model_tier="sonnet",
        )
        result = {"state": "success", "result": {"text": "BUILD OUTPUT\n"}}
        seq._record_review_outcome(build_phase, result)
        rows = db.list_review_outcomes()
        self.assertEqual(rows, [])

    def test_records_approve_outcome(self) -> None:
        """APPROVE verdict is recorded for a review-typed phase."""
        db = _make_db()
        run_id = _make_run_id()
        _insert_pipeline_run(db, run_id)
        seq, phase = self._make_sequencer(db=db, run_id=run_id)
        raw_output = "APPROVE\n"
        result = {"state": "success", "result": {"text": raw_output}}
        seq._record_review_outcome(phase, result)
        rows = db.get_review_outcomes_for_run(run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "APPROVE")
        self.assertEqual(rows[0]["phase_id"], "review")

    def test_records_request_changes_with_issues(self) -> None:
        """REQUEST_CHANGES verdict with issues is recorded correctly."""
        db = _make_db()
        run_id = _make_run_id()
        _insert_pipeline_run(db, run_id)
        seq, phase = self._make_sequencer(db=db, run_id=run_id)
        raw_output = (
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] SQL injection in db.py:42\n"
            "[MINOR][style] Missing docstring\n"
        )
        result = {"state": "success", "result": {"text": raw_output}}
        seq._record_review_outcome(phase, result)
        rows = db.get_review_outcomes_for_run(run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "REQUEST_CHANGES")
        self.assertEqual(len(rows[0]["issues_found"]), 2)

    def test_db_error_does_not_crash_pipeline(self) -> None:
        """A DB failure inside _record_review_outcome is swallowed as a warning."""
        mock_db = MagicMock()
        mock_db.insert_review_outcome.side_effect = RuntimeError("DB down")
        seq, phase = self._make_sequencer(db=mock_db, run_id="run-789")
        result = {"state": "success", "result": {"text": "APPROVE\n"}}
        try:
            seq._record_review_outcome(phase, result)
        except Exception as exc:
            self.fail(f"DB error propagated out of _record_review_outcome: {exc}")

    def test_run_id_stored_in_outcome(self) -> None:
        """The outcome row contains the correct run_id."""
        db = _make_db()
        run_id = _make_run_id()
        _insert_pipeline_run(db, run_id)
        seq, phase = self._make_sequencer(db=db, run_id=run_id)
        result = {"state": "success", "result": {"text": "APPROVE\n"}}
        seq._record_review_outcome(phase, result)
        rows = db.get_review_outcomes_for_run(run_id)
        self.assertEqual(rows[0]["run_id"], run_id)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
