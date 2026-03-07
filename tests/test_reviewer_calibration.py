"""tests/test_reviewer_calibration.py — Tests for Issue #4.1.5: Reviewer Calibration.

Covers:
- CalibrationMetrics dataclass (fields, defaults, to_dict)
- ReviewerCalibrator.compute() grouping, math, edge cases
- ReviewerCalibrator.calibrate_and_save() persistence path
- DB table creation and migration (015_add_reviewer_calibration_table)
- insert_calibration_snapshot returns rowid
- get_calibration_for_model returns most-recent snapshot or None
- list_calibration_snapshots pagination and ordering
- Module exports via __init__.py

Test classes:
    TestCalibrationMetricsDataclass  — shape, defaults, to_dict
    TestReviewerCalibratorCompute    — grouping, math, None rates, edge cases
    TestCalibrateAndSave             — persistence, no-DB path
    TestDbTableCreation              — table/index exist in fresh + migrated DBs
    TestInsertCalibrationSnapshot    — insert, return rowid, field round-trip
    TestGetCalibrationForModel       — most-recent row, None when missing
    TestListCalibrationSnapshots     — ordering DESC, pagination, empty list
    TestModuleExports                — __init__.py exports
"""

from __future__ import annotations

import json
import uuid
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from orchestration_engine.db import Database
from orchestration_engine.reviewer_calibration import (
    CalibrationMetrics,
    ReviewerCalibrator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return an in-memory Database with all migrations applied."""
    return Database(":memory:")


def _outcome(
    reviewer_model: str = "opus",
    verdict: str | None = "APPROVE",
    fix_verified: bool = False,
) -> Dict[str, Any]:
    """Build a minimal outcome dict matching the review_outcomes DB schema."""
    return {
        "review_id": str(uuid.uuid4()),
        "run_id": "run-test",
        "phase_id": "review",
        "reviewer_model": reviewer_model,
        "verdict": verdict,
        "issues_found": [],
        "fix_verified": fix_verified,
    }


def _snapshot(model: str = "opus", **overrides) -> Dict[str, Any]:
    """Build a minimal calibration snapshot dict for insert_calibration_snapshot."""
    base: Dict[str, Any] = {
        "reviewer_model": model,
        "total_reviews": 10,
        "approve_count": 6,
        "request_changes_count": 4,
        "approve_held_up_count": 5,
        "request_changes_valid_count": 3,
        "approve_accuracy": 5 / 6,
        "request_changes_accuracy": 3 / 4,
        "overall_accuracy": 8 / 10,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "aggregation_window": "all-time",
    }
    base.update(overrides)
    return base


# ===========================================================================
# TestCalibrationMetricsDataclass
# ===========================================================================


class TestCalibrationMetricsDataclass:
    """Shape, defaults, and to_dict for CalibrationMetrics."""

    def test_required_field_reviewer_model(self) -> None:
        m = CalibrationMetrics(reviewer_model="opus")
        assert m.reviewer_model == "opus"

    def test_integer_defaults(self) -> None:
        m = CalibrationMetrics(reviewer_model="sonnet")
        assert m.total_reviews == 0
        assert m.approve_count == 0
        assert m.request_changes_count == 0
        assert m.approve_held_up_count == 0
        assert m.request_changes_valid_count == 0

    def test_rate_defaults_are_none(self) -> None:
        m = CalibrationMetrics(reviewer_model="haiku")
        assert m.approve_accuracy is None
        assert m.request_changes_accuracy is None
        assert m.overall_accuracy is None

    def test_aggregation_window_default_none(self) -> None:
        m = CalibrationMetrics(reviewer_model="opus")
        assert m.aggregation_window is None

    def test_computed_at_auto_populated(self) -> None:
        m = CalibrationMetrics(reviewer_model="opus")
        # Should be a valid ISO-8601 string
        dt = datetime.fromisoformat(m.computed_at)
        assert dt is not None

    def test_all_expected_fields_present(self) -> None:
        names = {f.name for f in dc_fields(CalibrationMetrics)}
        expected = {
            "reviewer_model",
            "total_reviews",
            "approve_count",
            "request_changes_count",
            "approve_held_up_count",
            "request_changes_valid_count",
            "approve_accuracy",
            "request_changes_accuracy",
            "overall_accuracy",
            "computed_at",
            "aggregation_window",
        }
        assert expected == names

    def test_to_dict_keys(self) -> None:
        m = CalibrationMetrics(reviewer_model="opus", total_reviews=5)
        d = m.to_dict()
        expected_keys = {
            "reviewer_model",
            "total_reviews",
            "approve_count",
            "request_changes_count",
            "approve_held_up_count",
            "request_changes_valid_count",
            "approve_accuracy",
            "request_changes_accuracy",
            "overall_accuracy",
            "computed_at",
            "aggregation_window",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_roundtrip(self) -> None:
        m = CalibrationMetrics(
            reviewer_model="opus",
            total_reviews=4,
            approve_count=3,
            request_changes_count=1,
            approve_held_up_count=2,
            request_changes_valid_count=1,
            approve_accuracy=2 / 3,
            request_changes_accuracy=1.0,
            overall_accuracy=3 / 4,
            aggregation_window="7d",
        )
        d = m.to_dict()
        assert d["reviewer_model"] == "opus"
        assert d["total_reviews"] == 4
        assert d["approve_accuracy"] == pytest.approx(2 / 3)
        assert d["request_changes_accuracy"] == pytest.approx(1.0)
        assert d["aggregation_window"] == "7d"


# ===========================================================================
# TestReviewerCalibratorCompute
# ===========================================================================


class TestReviewerCalibratorCompute:
    """GroupING, math, None rates, and edge cases for ReviewerCalibrator.compute()."""

    def test_empty_outcomes_returns_empty_dict(self) -> None:
        c = ReviewerCalibrator()
        result = c.compute([])
        assert result == {}

    def test_single_approve_no_fix_needed(self) -> None:
        outcomes = [_outcome("opus", "APPROVE", fix_verified=False)]
        c = ReviewerCalibrator()
        result = c.compute(outcomes)

        m = result["opus"]
        assert m.total_reviews == 1
        assert m.approve_count == 1
        assert m.request_changes_count == 0
        assert m.approve_held_up_count == 1       # fix NOT needed → held up
        assert m.approve_accuracy == pytest.approx(1.0)
        assert m.request_changes_accuracy is None  # no RC verdicts
        assert m.overall_accuracy == pytest.approx(1.0)

    def test_single_approve_fix_was_needed(self) -> None:
        """APPROVE but fix_verified=True means the approval let a bug through."""
        outcomes = [_outcome("opus", "APPROVE", fix_verified=True)]
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        m = result["opus"]
        assert m.approve_held_up_count == 0
        assert m.approve_accuracy == pytest.approx(0.0)
        assert m.overall_accuracy == pytest.approx(0.0)

    def test_single_rc_fix_verified(self) -> None:
        outcomes = [_outcome("opus", "REQUEST_CHANGES", fix_verified=True)]
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        m = result["opus"]
        assert m.request_changes_count == 1
        assert m.request_changes_valid_count == 1
        assert m.request_changes_accuracy == pytest.approx(1.0)
        assert m.approve_accuracy is None
        assert m.overall_accuracy == pytest.approx(1.0)

    def test_single_rc_not_verified(self) -> None:
        """REQUEST_CHANGES but no verified fix → false alarm."""
        outcomes = [_outcome("opus", "REQUEST_CHANGES", fix_verified=False)]
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        m = result["opus"]
        assert m.request_changes_valid_count == 0
        assert m.request_changes_accuracy == pytest.approx(0.0)
        assert m.overall_accuracy == pytest.approx(0.0)

    def test_mixed_verdicts_math(self) -> None:
        """3 APPROVEs (2 correct) + 2 RCs (1 valid) → correct rates."""
        outcomes = [
            _outcome("opus", "APPROVE", fix_verified=False),  # correct
            _outcome("opus", "APPROVE", fix_verified=False),  # correct
            _outcome("opus", "APPROVE", fix_verified=True),   # wrong (let bug through)
            _outcome("opus", "REQUEST_CHANGES", fix_verified=True),   # valid
            _outcome("opus", "REQUEST_CHANGES", fix_verified=False),  # false alarm
        ]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.total_reviews == 5
        assert m.approve_count == 3
        assert m.request_changes_count == 2
        assert m.approve_held_up_count == 2
        assert m.request_changes_valid_count == 1
        assert m.approve_accuracy == pytest.approx(2 / 3)
        assert m.request_changes_accuracy == pytest.approx(1 / 2)
        assert m.overall_accuracy == pytest.approx(3 / 5)

    def test_unknown_model_grouped_under_unknown(self) -> None:
        outcomes = [_outcome(reviewer_model=None, verdict="APPROVE", fix_verified=False)]
        # None reviewer_model → dict key is preserved as None in the outcome
        # but our calibrator coerces it to "unknown"
        outcomes[0]["reviewer_model"] = None
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        assert "unknown" in result

    def test_empty_string_model_grouped_under_unknown(self) -> None:
        outcomes = [_outcome(reviewer_model="", verdict="APPROVE", fix_verified=False)]
        outcomes[0]["reviewer_model"] = ""
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        assert "unknown" in result

    def test_multiple_models_grouped_independently(self) -> None:
        outcomes = [
            _outcome("opus", "APPROVE", fix_verified=False),
            _outcome("sonnet", "REQUEST_CHANGES", fix_verified=True),
            _outcome("opus", "REQUEST_CHANGES", fix_verified=True),
        ]
        c = ReviewerCalibrator()
        result = c.compute(outcomes)
        assert set(result.keys()) == {"opus", "sonnet"}
        assert result["opus"].total_reviews == 2
        assert result["sonnet"].total_reviews == 1

    def test_aggregation_window_passed_through(self) -> None:
        outcomes = [_outcome("opus", "APPROVE", fix_verified=False)]
        c = ReviewerCalibrator(aggregation_window="30d")
        m = c.compute(outcomes)["opus"]
        assert m.aggregation_window == "30d"

    def test_aggregation_window_none_by_default(self) -> None:
        outcomes = [_outcome("opus", "APPROVE", fix_verified=False)]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.aggregation_window is None

    def test_overall_accuracy_none_when_no_reviews(self) -> None:
        c = ReviewerCalibrator()
        result = c.compute([])
        assert result == {}

    def test_verdict_case_insensitive_handling(self) -> None:
        """Verdicts are uppercased during processing."""
        outcomes = [
            {"reviewer_model": "opus", "verdict": "approve", "fix_verified": False},
            {"reviewer_model": "opus", "verdict": "request_changes", "fix_verified": True},
        ]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.approve_count == 1
        assert m.request_changes_count == 1

    def test_unknown_verdict_not_counted(self) -> None:
        """Outcomes with unrecognised verdicts don't affect counts."""
        outcomes = [
            _outcome("opus", "APPROVE", fix_verified=False),
            {"reviewer_model": "opus", "verdict": "PENDING", "fix_verified": False},
        ]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.total_reviews == 2
        assert m.approve_count == 1
        assert m.request_changes_count == 0

    def test_fix_verified_int_truthy(self) -> None:
        """fix_verified stored as int 1 should be treated as True."""
        outcomes = [{"reviewer_model": "opus", "verdict": "REQUEST_CHANGES", "fix_verified": 1}]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.request_changes_valid_count == 1

    def test_fix_verified_int_falsy(self) -> None:
        outcomes = [{"reviewer_model": "opus", "verdict": "REQUEST_CHANGES", "fix_verified": 0}]
        c = ReviewerCalibrator()
        m = c.compute(outcomes)["opus"]
        assert m.request_changes_valid_count == 0


# ===========================================================================
# TestCalibrateAndSave
# ===========================================================================


class TestCalibrateAndSave:
    """Persistence path and no-DB path for calibrate_and_save()."""

    def test_returns_same_result_as_compute(self) -> None:
        outcomes = [
            _outcome("opus", "APPROVE", fix_verified=False),
            _outcome("opus", "REQUEST_CHANGES", fix_verified=True),
        ]
        c = ReviewerCalibrator()
        compute_result = c.compute(outcomes)
        save_result = c.calibrate_and_save(outcomes)
        # Same models, same counts
        assert set(compute_result.keys()) == set(save_result.keys())
        for model in compute_result:
            assert compute_result[model].total_reviews == save_result[model].total_reviews

    def test_persists_to_db_when_provided(self) -> None:
        db = _make_db()
        outcomes = [
            _outcome("opus", "APPROVE", fix_verified=False),
            _outcome("sonnet", "REQUEST_CHANGES", fix_verified=True),
        ]
        c = ReviewerCalibrator(db=db, aggregation_window="all-time")
        c.calibrate_and_save(outcomes)

        snapshots = db.list_calibration_snapshots()
        models_saved = {s["reviewer_model"] for s in snapshots}
        assert "opus" in models_saved
        assert "sonnet" in models_saved

    def test_no_db_does_not_raise(self) -> None:
        outcomes = [_outcome("opus", "APPROVE", fix_verified=False)]
        c = ReviewerCalibrator(db=None)
        result = c.calibrate_and_save(outcomes)
        assert "opus" in result

    def test_empty_outcomes_persists_nothing(self) -> None:
        db = _make_db()
        c = ReviewerCalibrator(db=db)
        c.calibrate_and_save([])
        assert db.list_calibration_snapshots() == []

    def test_db_error_does_not_propagate(self) -> None:
        """Persistence failures are swallowed; compute result still returned."""
        class BrokenDb:
            def insert_calibration_snapshot(self, data):
                raise RuntimeError("DB offline")

        outcomes = [_outcome("opus", "APPROVE", fix_verified=False)]
        c = ReviewerCalibrator(db=BrokenDb())  # type: ignore[arg-type]
        # Should NOT raise
        result = c.calibrate_and_save(outcomes)
        assert "opus" in result


# ===========================================================================
# TestDbTableCreation
# ===========================================================================


class TestDbTableCreation:
    """reviewer_calibration table and index are created correctly."""

    def test_table_exists_in_fresh_db(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reviewer_calibration'"
            )
            row = cursor.fetchone()
        assert row is not None, "reviewer_calibration table not found"

    def test_index_exists_in_fresh_db(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_reviewer_calibration_model'"
            )
            row = cursor.fetchone()
        assert row is not None, "idx_reviewer_calibration_model index not found"

    def test_migration_015_recorded(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM migrations WHERE name='015_add_reviewer_calibration_table'"
            )
            row = cursor.fetchone()
        assert row is not None, "migration 015 not recorded"

    def test_all_expected_columns_present(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute("PRAGMA table_info(reviewer_calibration)")
            rows = cursor.fetchall()
        col_names = {row[1] for row in rows}
        expected = {
            "id",
            "reviewer_model",
            "total_reviews",
            "approve_count",
            "request_changes_count",
            "approve_held_up_count",
            "request_changes_valid_count",
            "approve_accuracy",
            "request_changes_accuracy",
            "overall_accuracy",
            "computed_at",
            "aggregation_window",
        }
        assert expected <= col_names


# ===========================================================================
# TestInsertCalibrationSnapshot
# ===========================================================================


class TestInsertCalibrationSnapshot:
    """insert_calibration_snapshot inserts a row and returns the rowid."""

    def test_returns_positive_int_rowid(self) -> None:
        db = _make_db()
        rowid = db.insert_calibration_snapshot(_snapshot())
        assert isinstance(rowid, int)
        assert rowid > 0

    def test_rowid_increments(self) -> None:
        db = _make_db()
        id1 = db.insert_calibration_snapshot(_snapshot("opus"))
        id2 = db.insert_calibration_snapshot(_snapshot("sonnet"))
        assert id2 > id1

    def test_row_retrievable_after_insert(self) -> None:
        db = _make_db()
        data = _snapshot("opus", total_reviews=7, approve_count=4)
        db.insert_calibration_snapshot(data)
        row = db.get_calibration_for_model("opus")
        assert row is not None
        assert row["reviewer_model"] == "opus"
        assert row["total_reviews"] == 7
        assert row["approve_count"] == 4

    def test_null_rates_stored_as_none(self) -> None:
        db = _make_db()
        data = _snapshot("opus", approve_accuracy=None, request_changes_accuracy=None,
                          overall_accuracy=None)
        db.insert_calibration_snapshot(data)
        row = db.get_calibration_for_model("opus")
        assert row["approve_accuracy"] is None
        assert row["request_changes_accuracy"] is None
        assert row["overall_accuracy"] is None

    def test_aggregation_window_stored(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus", aggregation_window="7d"))
        row = db.get_calibration_for_model("opus")
        assert row["aggregation_window"] == "7d"

    def test_float_rates_stored_with_precision(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus", approve_accuracy=0.666666))
        row = db.get_calibration_for_model("opus")
        assert row["approve_accuracy"] == pytest.approx(0.666666, rel=1e-5)

    def test_multiple_models_stored_independently(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus"))
        db.insert_calibration_snapshot(_snapshot("sonnet"))
        db.insert_calibration_snapshot(_snapshot("haiku"))
        snapshots = db.list_calibration_snapshots()
        models = {s["reviewer_model"] for s in snapshots}
        assert models == {"opus", "sonnet", "haiku"}


# ===========================================================================
# TestGetCalibrationForModel
# ===========================================================================


class TestGetCalibrationForModel:
    """get_calibration_for_model returns most-recent snapshot or None."""

    def test_returns_none_when_model_not_found(self) -> None:
        db = _make_db()
        result = db.get_calibration_for_model("nonexistent-model")
        assert result is None

    def test_returns_correct_model(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus"))
        db.insert_calibration_snapshot(_snapshot("sonnet"))
        row = db.get_calibration_for_model("sonnet")
        assert row is not None
        assert row["reviewer_model"] == "sonnet"

    def test_returns_most_recent_by_computed_at(self) -> None:
        db = _make_db()
        # Insert older snapshot first
        db.insert_calibration_snapshot(_snapshot(
            "opus",
            total_reviews=5,
            computed_at="2024-01-01T00:00:00+00:00",
        ))
        # Insert newer snapshot second
        db.insert_calibration_snapshot(_snapshot(
            "opus",
            total_reviews=20,
            computed_at="2024-06-01T00:00:00+00:00",
        ))
        row = db.get_calibration_for_model("opus")
        # Should return the most recent (total_reviews=20)
        assert row["total_reviews"] == 20

    def test_does_not_return_other_model(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus"))
        result = db.get_calibration_for_model("sonnet")
        assert result is None


# ===========================================================================
# TestListCalibrationSnapshots
# ===========================================================================


class TestListCalibrationSnapshots:
    """list_calibration_snapshots ordering, pagination, and empty list."""

    def test_empty_db_returns_empty_list(self) -> None:
        db = _make_db()
        assert db.list_calibration_snapshots() == []

    def test_returns_all_rows_when_within_limit(self) -> None:
        db = _make_db()
        for model in ("opus", "sonnet", "haiku"):
            db.insert_calibration_snapshot(_snapshot(model))
        rows = db.list_calibration_snapshots()
        assert len(rows) == 3

    def test_ordered_desc_by_computed_at(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot(
            "opus", computed_at="2024-01-01T00:00:00+00:00"
        ))
        db.insert_calibration_snapshot(_snapshot(
            "sonnet", computed_at="2024-06-01T00:00:00+00:00"
        ))
        db.insert_calibration_snapshot(_snapshot(
            "haiku", computed_at="2024-03-01T00:00:00+00:00"
        ))
        rows = db.list_calibration_snapshots()
        models_in_order = [r["reviewer_model"] for r in rows]
        # sonnet (June) → haiku (March) → opus (January)
        assert models_in_order == ["sonnet", "haiku", "opus"]

    def test_limit_respected(self) -> None:
        db = _make_db()
        for i in range(10):
            db.insert_calibration_snapshot(_snapshot(f"model-{i}"))
        rows = db.list_calibration_snapshots(limit=3)
        assert len(rows) == 3

    def test_offset_paginates_correctly(self) -> None:
        db = _make_db()
        # Insert with distinct timestamps to guarantee ordering
        timestamps = [
            "2024-10-0{}T00:00:00+00:00".format(i + 1) for i in range(5)
        ]
        for i, ts in enumerate(reversed(timestamps)):  # older first in DB
            db.insert_calibration_snapshot(_snapshot(f"model-{i}", computed_at=ts))
        page1 = db.list_calibration_snapshots(limit=2, offset=0)
        page2 = db.list_calibration_snapshots(limit=2, offset=2)
        # No overlap
        ids_p1 = {r["id"] for r in page1}
        ids_p2 = {r["id"] for r in page2}
        assert ids_p1.isdisjoint(ids_p2)

    def test_default_limit_is_50(self) -> None:
        db = _make_db()
        for i in range(60):
            db.insert_calibration_snapshot(_snapshot(f"model-{i}"))
        rows = db.list_calibration_snapshots()
        assert len(rows) == 50

    def test_row_contains_id_field(self) -> None:
        db = _make_db()
        db.insert_calibration_snapshot(_snapshot("opus"))
        rows = db.list_calibration_snapshots()
        assert "id" in rows[0]
        assert isinstance(rows[0]["id"], int)


# ===========================================================================
# TestModuleExports
# ===========================================================================


class TestModuleExports:
    """Verify __init__.py exports ReviewerCalibrator and CalibrationMetrics."""

    def test_reviewer_calibrator_in_all(self) -> None:
        import orchestration_engine as oe
        assert "ReviewerCalibrator" in oe.__all__

    def test_calibration_metrics_in_all(self) -> None:
        import orchestration_engine as oe
        assert "CalibrationMetrics" in oe.__all__

    def test_reviewer_calibrator_importable_from_package(self) -> None:
        from orchestration_engine import ReviewerCalibrator  # noqa: F401
        assert ReviewerCalibrator is not None

    def test_calibration_metrics_importable_from_package(self) -> None:
        from orchestration_engine import CalibrationMetrics  # noqa: F401
        assert CalibrationMetrics is not None

    def test_reviewer_calibrator_importable_from_module(self) -> None:
        from orchestration_engine.reviewer_calibration import ReviewerCalibrator  # noqa: F401
        assert ReviewerCalibrator is not None
