"""Tests for regression data model, enum, dataclass, DB migration and CRUD (Issue #3.3a.1)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from orchestration_engine.regression import Regression, RegressionStatus
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return Database(db_path=Path(":memory:"))


# ---------------------------------------------------------------------------
# TestRegressionStatusEnum
# ---------------------------------------------------------------------------

class TestRegressionStatusEnum:
    def test_all_five_values_exist(self):
        # Updated to 6 after adding NEEDS_REVIEW in #3.3b.2
        assert len(RegressionStatus) == 6

    def test_is_str_subclass(self):
        assert isinstance(RegressionStatus.DETECTED, str)

    def test_individual_values(self):
        assert RegressionStatus.DETECTED.value == "detected"
        assert RegressionStatus.DIAGNOSING.value == "diagnosing"
        assert RegressionStatus.FIXING.value == "fixing"
        assert RegressionStatus.FIXED.value == "fixed"
        assert RegressionStatus.ESCALATED.value == "escalated"

    def test_from_string_roundtrip(self):
        assert RegressionStatus("fixing") is RegressionStatus.FIXING

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RegressionStatus("not_a_status")

    def test_string_comparison(self):
        assert RegressionStatus.DETECTED == "detected"
        assert RegressionStatus.FIXED == "fixed"


# ---------------------------------------------------------------------------
# TestRegressionDataclass
# ---------------------------------------------------------------------------

class TestRegressionDataclass:
    def test_defaults(self):
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
        )
        assert r.fix_attempt_count == 0
        assert r.status == RegressionStatus.DETECTED
        assert r.affected_files == []
        assert r.diagnosis is None
        assert r.fix_run_id is None

    def test_required_fields_missing_commit_sha(self):
        with pytest.raises(TypeError):
            Regression(ci_run_url="https://github.com/runs/1", failure_type="build_error")  # type: ignore[call-arg]

    def test_required_fields_missing_ci_run_url(self):
        with pytest.raises(TypeError):
            Regression(commit_sha="abc123", failure_type="build_error")  # type: ignore[call-arg]

    def test_required_fields_missing_failure_type(self):
        with pytest.raises(TypeError):
            Regression(commit_sha="abc123", ci_run_url="https://github.com/runs/1")  # type: ignore[call-arg]

    def test_id_is_uuid(self):
        import uuid
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
        )
        assert r.id
        # Should parse without raising
        parsed = uuid.UUID(r.id)
        assert str(parsed) == r.id

    def test_unique_ids(self):
        r1 = Regression(commit_sha="a", ci_run_url="u", failure_type="f")
        r2 = Regression(commit_sha="a", ci_run_url="u", failure_type="f")
        assert r1.id != r2.id

    def test_to_dict_serialises_affected_files_as_json_string(self):
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
            affected_files=["src/foo.py", "tests/bar.py"],
        )
        d = r.to_dict()
        assert isinstance(d["affected_files"], str)
        assert json.loads(d["affected_files"]) == ["src/foo.py", "tests/bar.py"]

    def test_to_dict_status_is_string(self):
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
        )
        d = r.to_dict()
        assert d["status"] == "detected"
        assert isinstance(d["status"], str)

    def test_to_dict_keys(self):
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
        )
        expected_keys = {
            "id", "commit_sha", "ci_run_url", "failure_type",
            "affected_files", "diagnosis", "fix_run_id", "status",
            "fix_attempt_count", "created_at",
        }
        assert set(r.to_dict().keys()) == expected_keys

    def test_created_at_is_utc(self):
        r = Regression(
            commit_sha="abc123",
            ci_run_url="https://github.com/runs/1",
            failure_type="test_failure",
        )
        assert r.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# TestRegressionDBMigration
# ---------------------------------------------------------------------------

class TestRegressionDBMigration:
    def test_table_exists(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='regressions'"
        ).fetchone()
        assert row is not None

    def test_required_columns(self, db):
        conn = db.get_connection()
        rows = conn.execute("PRAGMA table_info(regressions)").fetchall()
        columns = {r["name"] for r in rows}
        expected = {
            "id", "commit_sha", "ci_run_url", "failure_type",
            "affected_files", "diagnosis", "fix_run_id",
            "status", "fix_attempt_count", "created_at",
        }
        assert expected.issubset(columns)

    def test_migration_idempotent(self, db):
        conn = db.get_connection()
        # Running twice must not raise
        db._migration_012_add_regressions_table(conn)
        db._migration_012_add_regressions_table(conn)

    def test_indexes_exist(self, db):
        conn = db.get_connection()
        index_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_regressions_status_created" in index_names
        assert "idx_regressions_commit_sha" in index_names

    def test_migration_recorded(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM migrations WHERE name='012_add_regressions_table'"
        ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# TestRegressionCRUD
# ---------------------------------------------------------------------------

def _make_regression(**kwargs) -> Regression:
    """Helper to build a Regression with sensible defaults."""
    defaults = dict(
        commit_sha="deadbeef",
        ci_run_url="https://github.com/runs/42",
        failure_type="test_failure",
    )
    defaults.update(kwargs)
    return Regression(**defaults)


class TestRegressionCRUD:
    def test_insert_and_get(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        result = db.get_regression(r.id)
        assert result is not None
        assert result["id"] == r.id
        assert result["commit_sha"] == "deadbeef"
        assert result["ci_run_url"] == "https://github.com/runs/42"
        assert result["failure_type"] == "test_failure"
        assert result["status"] == "detected"
        assert result["fix_attempt_count"] == 0

    def test_affected_files_roundtrip(self, db):
        r = _make_regression(affected_files=["src/foo.py", "tests/bar.py"])
        db.insert_regression(r.to_dict())
        result = db.get_regression(r.id)
        assert isinstance(result["affected_files"], list)
        assert result["affected_files"] == ["src/foo.py", "tests/bar.py"]

    def test_affected_files_empty_list_roundtrip(self, db):
        r = _make_regression()  # affected_files defaults to []
        db.insert_regression(r.to_dict())
        result = db.get_regression(r.id)
        assert result["affected_files"] == []

    def test_fix_attempt_count_defaults_to_zero(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        result = db.get_regression(r.id)
        assert result["fix_attempt_count"] == 0

    def test_get_returns_none_for_missing_id(self, db):
        assert db.get_regression("nonexistent-id") is None

    def test_update_status(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        updated = db.update_regression(r.id, status="fixing")
        assert updated is True
        result = db.get_regression(r.id)
        assert result["status"] == "fixing"

    def test_update_fix_attempt_count(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        db.update_regression(r.id, fix_attempt_count=2)
        result = db.get_regression(r.id)
        assert result["fix_attempt_count"] == 2

    def test_update_diagnosis(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        db.update_regression(r.id, diagnosis="Tests failing due to import error in foo.py")
        result = db.get_regression(r.id)
        assert result["diagnosis"] == "Tests failing due to import error in foo.py"

    def test_update_fix_run_id(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        db.update_regression(r.id, fix_run_id="run-fix-001")
        result = db.get_regression(r.id)
        assert result["fix_run_id"] == "run-fix-001"

    def test_update_returns_false_for_missing_id(self, db):
        result = db.update_regression("nonexistent", status="fixed")
        assert result is False

    def test_update_no_valid_kwargs_returns_false(self, db):
        r = _make_regression()
        db.insert_regression(r.to_dict())
        # Only unrecognised kwargs — should return False
        result = db.update_regression(r.id, not_a_field="value")
        assert result is False

    def test_list_no_filter(self, db):
        for i in range(3):
            r = _make_regression(commit_sha=f"sha{i}")
            db.insert_regression(r.to_dict())
        results = db.list_regressions()
        assert len(results) >= 3

    def test_list_with_status_filter(self, db):
        r_detected = _make_regression(commit_sha="sha-detected")
        db.insert_regression(r_detected.to_dict())
        r_fixed = _make_regression(commit_sha="sha-fixed")
        data = r_fixed.to_dict()
        data["status"] = "fixed"
        db.insert_regression(data)

        detected_results = db.list_regressions(status="detected")
        fixed_results = db.list_regressions(status="fixed")

        detected_ids = {r["id"] for r in detected_results}
        assert r_detected.id in detected_ids
        assert r_fixed.id not in detected_ids

        fixed_ids = {r["id"] for r in fixed_results}
        assert r_fixed.id in fixed_ids
        assert r_detected.id not in fixed_ids

    def test_list_ordering_newest_first(self, db):
        # Insert with explicitly different created_at values
        now = datetime.now(timezone.utc)
        ids = []
        for i in range(3):
            r = _make_regression(commit_sha=f"sha-ord-{i}")
            d = r.to_dict()
            d["created_at"] = (now + timedelta(seconds=i)).isoformat()
            db.insert_regression(d)
            ids.append(r.id)

        results = db.list_regressions()
        result_ids = [r["id"] for r in results if r["id"] in ids]
        # Most recently created should come first
        assert result_ids[0] == ids[2]
        assert result_ids[-1] == ids[0]

    def test_list_pagination(self, db):
        inserted_ids = []
        now = datetime.now(timezone.utc)
        for i in range(5):
            r = _make_regression(commit_sha=f"sha-page-{i}")
            d = r.to_dict()
            d["created_at"] = (now + timedelta(seconds=i)).isoformat()
            db.insert_regression(d)
            inserted_ids.append(r.id)

        page1 = db.list_regressions(limit=2, offset=0)
        page2 = db.list_regressions(limit=2, offset=2)
        page3 = db.list_regressions(limit=2, offset=4)

        page1_ids = {r["id"] for r in page1 if r["id"] in inserted_ids}
        page2_ids = {r["id"] for r in page2 if r["id"] in inserted_ids}

        assert len(page1_ids) == 2
        assert len(page2_ids) == 2
        # Pages must not overlap
        assert page1_ids.isdisjoint(page2_ids)
        # Last page has 1 row from our 5 inserts
        page3_ids = {r["id"] for r in page3 if r["id"] in inserted_ids}
        assert len(page3_ids) == 1

    def test_insert_with_affected_files_as_pre_serialised_string(self, db):
        r = _make_regression()
        d = r.to_dict()
        # affected_files is already a JSON string from to_dict()
        assert isinstance(d["affected_files"], str)
        db.insert_regression(d)  # Should not raise
        result = db.get_regression(r.id)
        assert isinstance(result["affected_files"], list)

    def test_insert_with_affected_files_as_list(self, db):
        r = _make_regression(affected_files=["src/a.py"])
        d = r.to_dict()
        d["affected_files"] = ["src/a.py"]  # Pass as list, not JSON string
        db.insert_regression(d)
        result = db.get_regression(r.id)
        assert result["affected_files"] == ["src/a.py"]

    def test_insert_missing_optional_fields(self, db):
        r = _make_regression()
        d = r.to_dict()
        # Optional fields are None
        assert d["diagnosis"] is None
        assert d["fix_run_id"] is None
        db.insert_regression(d)
        result = db.get_regression(r.id)
        assert result["diagnosis"] is None
        assert result["fix_run_id"] is None
