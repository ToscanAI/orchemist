"""tests/test_trust_profile.py — Tests for Issue #4.2.1: Trust Profile Data Model.

Covers:
- TrustProfile dataclass (fields, defaults, to_dict)
- TrustConfig dataclass (fields, defaults, to_dict)
- DB table creation (trust_profiles, trust_adjustments)
- Migration 016 recorded correctly
- upsert_trust_profile: insert, return id, upsert semantics, unique constraint
- get_trust_profile: retrieval, None when missing, composite key lookup
- insert_trust_adjustment: insert, return id, field round-trip
- list_trust_adjustments: ordering, pagination, profile isolation
- Module exports via __init__.py

Test classes:
    TestTrustProfileDataclass       — shape, defaults, to_dict
    TestTrustConfigDataclass        — shape, defaults, to_dict
    TestDbTableCreation             — tables, indexes, migration in fresh DB
    TestUpsertTrustProfile          — insert, upsert, unique key, returns int
    TestGetTrustProfile             — lookup, None when missing, isolation
    TestInsertTrustAdjustment       — insert, field round-trip, returns id
    TestListTrustAdjustments        — ordering DESC, pagination, profile isolation
    TestModuleExports               — __init__.py exports TrustProfile, TrustConfig
"""

from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.trust import TrustConfig, TrustProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return an in-memory Database with all migrations applied."""
    return Database(":memory:")


def _profile_data(
    repo: str = "owner/repo",
    template_id: str = "coding-pipeline-v1",
    task_type: str = "bugfix",
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a minimal trust profile dict for upsert_trust_profile."""
    base: Dict[str, Any] = {
        "repo": repo,
        "template_id": template_id,
        "task_type": task_type,
        "auto_merge_threshold": 0.85,
        "human_review_threshold": 0.70,
        "trust_score": 0.5,
        "total_runs": 0,
        "successful_merges": 0,
        "regressions": 0,
        "reverted_prs": 0,
        "last_run_at": None,
    }
    base.update(overrides)
    return base


def _adjustment_data(
    profile_id: int,
    delta: float = 0.02,
    reason: str = "successful_merge",
    score_before: float = 0.50,
    score_after: float = 0.52,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a minimal trust adjustment dict for insert_trust_adjustment."""
    base: Dict[str, Any] = {
        "profile_id": profile_id,
        "delta": delta,
        "reason": reason,
        "score_before": score_before,
        "score_after": score_after,
    }
    base.update(overrides)
    return base


# ===========================================================================
# TestTrustProfileDataclass
# ===========================================================================


class TestTrustProfileDataclass:
    """Shape, defaults, and to_dict for TrustProfile."""

    def test_required_fields(self) -> None:
        p = TrustProfile(repo="owner/repo", template_id="pipeline-v1", task_type="bugfix")
        assert p.repo == "owner/repo"
        assert p.template_id == "pipeline-v1"
        assert p.task_type == "bugfix"

    def test_default_thresholds(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.auto_merge_threshold == pytest.approx(0.85)
        assert p.human_review_threshold == pytest.approx(0.70)

    def test_default_trust_score(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.trust_score == pytest.approx(0.5)

    def test_default_counters_zero(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.total_runs == 0
        assert p.successful_merges == 0
        assert p.regressions == 0
        assert p.reverted_prs == 0

    def test_default_last_run_at_none(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.last_run_at is None

    def test_default_id_none(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.id is None

    def test_created_at_auto_populated(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        dt = datetime.fromisoformat(p.created_at)
        assert dt is not None

    def test_updated_at_auto_populated(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        dt = datetime.fromisoformat(p.updated_at)
        assert dt is not None

    def test_all_expected_fields_present(self) -> None:
        names = {f.name for f in dc_fields(TrustProfile)}
        expected = {
            "repo",
            "template_id",
            "task_type",
            "auto_merge_threshold",
            "human_review_threshold",
            "trust_score",
            "total_runs",
            "successful_merges",
            "regressions",
            "reverted_prs",
            "last_run_at",
            "id",
            "created_at",
            "updated_at",
        }
        assert expected == names

    def test_to_dict_keys(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        d = p.to_dict()
        expected_keys = {
            "id",
            "repo",
            "template_id",
            "task_type",
            "auto_merge_threshold",
            "human_review_threshold",
            "trust_score",
            "total_runs",
            "successful_merges",
            "regressions",
            "reverted_prs",
            "last_run_at",
            "created_at",
            "updated_at",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_roundtrip(self) -> None:
        p = TrustProfile(
            repo="owner/repo",
            template_id="coding-v1",
            task_type="feature",
            trust_score=0.75,
            total_runs=10,
            successful_merges=8,
            regressions=1,
            reverted_prs=1,
            auto_merge_threshold=0.90,
            human_review_threshold=0.75,
        )
        d = p.to_dict()
        assert d["repo"] == "owner/repo"
        assert d["trust_score"] == pytest.approx(0.75)
        assert d["total_runs"] == 10
        assert d["successful_merges"] == 8
        assert d["regressions"] == 1
        assert d["reverted_prs"] == 1
        assert d["auto_merge_threshold"] == pytest.approx(0.90)

    def test_to_dict_id_is_none_for_unsaved(self) -> None:
        p = TrustProfile(repo="r", template_id="t", task_type="x")
        assert p.to_dict()["id"] is None

    def test_custom_values_stored(self) -> None:
        p = TrustProfile(
            repo="r",
            template_id="t",
            task_type="x",
            id=42,
            trust_score=0.99,
            last_run_at="2024-06-01T12:00:00+00:00",
        )
        assert p.id == 42
        assert p.trust_score == pytest.approx(0.99)
        assert p.last_run_at == "2024-06-01T12:00:00+00:00"


# ===========================================================================
# TestTrustConfigDataclass
# ===========================================================================


class TestTrustConfigDataclass:
    """Shape, defaults, and to_dict for TrustConfig."""

    def test_default_success_delta(self) -> None:
        c = TrustConfig()
        assert c.success_delta == pytest.approx(0.02)

    def test_default_regression_penalty(self) -> None:
        c = TrustConfig()
        assert c.regression_penalty == pytest.approx(-0.10)

    def test_default_revert_penalty(self) -> None:
        c = TrustConfig()
        assert c.revert_penalty == pytest.approx(-0.15)

    def test_default_bounds(self) -> None:
        c = TrustConfig()
        assert c.min_score == pytest.approx(0.0)
        assert c.max_score == pytest.approx(1.0)

    def test_default_initial_score(self) -> None:
        c = TrustConfig()
        assert c.initial_score == pytest.approx(0.5)

    def test_default_initial_thresholds(self) -> None:
        c = TrustConfig()
        assert c.initial_auto_merge_threshold == pytest.approx(0.85)
        assert c.initial_human_review_threshold == pytest.approx(0.70)

    def test_all_expected_fields_present(self) -> None:
        names = {f.name for f in dc_fields(TrustConfig)}
        expected = {
            "success_delta",
            "regression_penalty",
            "revert_penalty",
            "min_score",
            "max_score",
            "initial_score",
            "initial_auto_merge_threshold",
            "initial_human_review_threshold",
        }
        assert expected == names

    def test_to_dict_keys(self) -> None:
        c = TrustConfig()
        d = c.to_dict()
        expected_keys = {
            "success_delta",
            "regression_penalty",
            "revert_penalty",
            "min_score",
            "max_score",
            "initial_score",
            "initial_auto_merge_threshold",
            "initial_human_review_threshold",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_roundtrip(self) -> None:
        c = TrustConfig(success_delta=0.05, regression_penalty=-0.20)
        d = c.to_dict()
        assert d["success_delta"] == pytest.approx(0.05)
        assert d["regression_penalty"] == pytest.approx(-0.20)

    def test_custom_config_overrides(self) -> None:
        c = TrustConfig(
            success_delta=0.10,
            min_score=0.1,
            max_score=0.9,
            initial_score=0.6,
        )
        assert c.success_delta == pytest.approx(0.10)
        assert c.min_score == pytest.approx(0.1)
        assert c.max_score == pytest.approx(0.9)
        assert c.initial_score == pytest.approx(0.6)


# ===========================================================================
# TestDbTableCreation
# ===========================================================================


class TestDbTableCreation:
    """trust_profiles and trust_adjustments tables created correctly."""

    def test_trust_profiles_table_exists(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trust_profiles'"
            )
            row = cursor.fetchone()
        assert row is not None, "trust_profiles table not found"

    def test_trust_adjustments_table_exists(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trust_adjustments'"
            )
            row = cursor.fetchone()
        assert row is not None, "trust_adjustments table not found"

    def test_trust_profiles_index_exists(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_trust_profiles_repo_template'"
            )
            row = cursor.fetchone()
        assert row is not None, "idx_trust_profiles_repo_template index not found"

    def test_trust_adjustments_index_exists(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_trust_adjustments_profile_id'"
            )
            row = cursor.fetchone()
        assert row is not None, "idx_trust_adjustments_profile_id index not found"

    def test_migration_016_recorded(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM migrations WHERE name='016_add_trust_tables'"
            )
            row = cursor.fetchone()
        assert row is not None, "migration 016 not recorded"

    def test_trust_profiles_columns(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute("PRAGMA table_info(trust_profiles)")
            rows = cursor.fetchall()
        col_names = {row[1] for row in rows}
        expected = {
            "id",
            "repo",
            "template_id",
            "task_type",
            "auto_merge_threshold",
            "human_review_threshold",
            "trust_score",
            "total_runs",
            "successful_merges",
            "regressions",
            "reverted_prs",
            "last_run_at",
            "created_at",
            "updated_at",
        }
        assert expected <= col_names

    def test_trust_adjustments_columns(self) -> None:
        db = _make_db()
        with db._locked():
            conn = db.get_connection()
            cursor = conn.execute("PRAGMA table_info(trust_adjustments)")
            rows = cursor.fetchall()
        col_names = {row[1] for row in rows}
        expected = {
            "id",
            "profile_id",
            "delta",
            "reason",
            "run_id",
            "score_before",
            "score_after",
            "created_at",
        }
        assert expected <= col_names

    def test_unique_constraint_on_trust_profiles(self) -> None:
        """Inserting duplicate (repo, template_id, task_type) should not raise — it upserts."""
        db = _make_db()
        data = _profile_data()
        id1 = db.upsert_trust_profile(data)
        id2 = db.upsert_trust_profile(data)
        # Same row — ids should be identical
        assert id1 == id2


# ===========================================================================
# TestUpsertTrustProfile
# ===========================================================================


class TestUpsertTrustProfile:
    """upsert_trust_profile inserts rows and handles upsert semantics."""

    def test_returns_positive_int(self) -> None:
        db = _make_db()
        row_id = db.upsert_trust_profile(_profile_data())
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_second_upsert_returns_same_id(self) -> None:
        db = _make_db()
        id1 = db.upsert_trust_profile(_profile_data())
        id2 = db.upsert_trust_profile(_profile_data())
        assert id1 == id2

    def test_different_triplets_get_different_ids(self) -> None:
        db = _make_db()
        id1 = db.upsert_trust_profile(_profile_data(repo="owner/repo-a"))
        id2 = db.upsert_trust_profile(_profile_data(repo="owner/repo-b"))
        assert id1 != id2

    def test_upsert_updates_mutable_fields(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(trust_score=0.5))
        db.upsert_trust_profile(_profile_data(trust_score=0.75))
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["trust_score"] == pytest.approx(0.75)

    def test_upsert_updates_counters(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(total_runs=1, successful_merges=1))
        db.upsert_trust_profile(_profile_data(total_runs=5, successful_merges=4, regressions=1))
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["total_runs"] == 5
        assert row["successful_merges"] == 4
        assert row["regressions"] == 1

    def test_upsert_stores_last_run_at(self) -> None:
        db = _make_db()
        ts = "2024-09-01T08:00:00+00:00"
        db.upsert_trust_profile(_profile_data(last_run_at=ts))
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["last_run_at"] == ts

    def test_default_thresholds_stored(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data())
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["auto_merge_threshold"] == pytest.approx(0.85)
        assert row["human_review_threshold"] == pytest.approx(0.70)

    def test_custom_thresholds_stored(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(auto_merge_threshold=0.95,
                                               human_review_threshold=0.80))
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["auto_merge_threshold"] == pytest.approx(0.95)
        assert row["human_review_threshold"] == pytest.approx(0.80)

    def test_multiple_profiles_independent(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(repo="a/b", trust_score=0.3))
        db.upsert_trust_profile(_profile_data(repo="c/d", trust_score=0.9))
        row_ab = db.get_trust_profile("a/b", "coding-pipeline-v1", "bugfix")
        row_cd = db.get_trust_profile("c/d", "coding-pipeline-v1", "bugfix")
        assert row_ab["trust_score"] == pytest.approx(0.3)
        assert row_cd["trust_score"] == pytest.approx(0.9)


# ===========================================================================
# TestGetTrustProfile
# ===========================================================================


class TestGetTrustProfile:
    """get_trust_profile returns the correct row or None."""

    def test_returns_none_when_not_found(self) -> None:
        db = _make_db()
        result = db.get_trust_profile("nonexistent/repo", "pipeline", "bugfix")
        assert result is None

    def test_returns_correct_row(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(repo="owner/repo"))
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert row["repo"] == "owner/repo"

    def test_does_not_return_different_repo(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(repo="owner/repo-a"))
        result = db.get_trust_profile("owner/repo-b", "coding-pipeline-v1", "bugfix")
        assert result is None

    def test_does_not_return_different_template(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(template_id="pipeline-v1"))
        result = db.get_trust_profile("owner/repo", "pipeline-v2", "bugfix")
        assert result is None

    def test_does_not_return_different_task_type(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data(task_type="bugfix"))
        result = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "feature")
        assert result is None

    def test_row_contains_id_field(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data())
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        assert "id" in row
        assert isinstance(row["id"], int)

    def test_row_contains_all_columns(self) -> None:
        db = _make_db()
        db.upsert_trust_profile(_profile_data())
        row = db.get_trust_profile("owner/repo", "coding-pipeline-v1", "bugfix")
        assert row is not None
        expected_keys = {
            "id", "repo", "template_id", "task_type",
            "auto_merge_threshold", "human_review_threshold",
            "trust_score", "total_runs", "successful_merges",
            "regressions", "reverted_prs", "last_run_at",
            "created_at", "updated_at",
        }
        assert expected_keys <= set(row.keys())


# ===========================================================================
# TestInsertTrustAdjustment
# ===========================================================================


class TestInsertTrustAdjustment:
    """insert_trust_adjustment inserts a row and returns the rowid."""

    def _setup_profile(self, db: Database) -> int:
        return db.upsert_trust_profile(_profile_data())

    def test_returns_positive_int_rowid(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        row_id = db.insert_trust_adjustment(_adjustment_data(pid))
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_rowid_increments(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        id1 = db.insert_trust_adjustment(_adjustment_data(pid))
        id2 = db.insert_trust_adjustment(_adjustment_data(pid))
        assert id2 > id1

    def test_fields_stored_correctly(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(
            pid,
            delta=0.02,
            reason="successful_merge",
            score_before=0.50,
            score_after=0.52,
        ))
        rows = db.list_trust_adjustments(pid)
        assert len(rows) == 1
        row = rows[0]
        assert row["profile_id"] == pid
        assert row["delta"] == pytest.approx(0.02)
        assert row["reason"] == "successful_merge"
        assert row["score_before"] == pytest.approx(0.50)
        assert row["score_after"] == pytest.approx(0.52)

    def test_negative_delta_stored(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(
            pid,
            delta=-0.10,
            reason="regression_detected",
            score_before=0.70,
            score_after=0.60,
        ))
        rows = db.list_trust_adjustments(pid)
        assert rows[0]["delta"] == pytest.approx(-0.10)

    def test_optional_run_id_stored(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(pid, run_id="run-abc-123"))
        rows = db.list_trust_adjustments(pid)
        assert rows[0]["run_id"] == "run-abc-123"

    def test_optional_run_id_none_by_default(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid)
        assert rows[0]["run_id"] is None

    def test_created_at_auto_populated(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid)
        assert rows[0]["created_at"] is not None

    def test_custom_created_at_stored(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        ts = "2024-01-15T10:30:00+00:00"
        db.insert_trust_adjustment(_adjustment_data(pid, created_at=ts))
        rows = db.list_trust_adjustments(pid)
        assert rows[0]["created_at"] == ts


# ===========================================================================
# TestListTrustAdjustments
# ===========================================================================


class TestListTrustAdjustments:
    """list_trust_adjustments ordering, pagination, and profile isolation."""

    def _setup_profile(self, db: Database, **kwargs: Any) -> int:
        return db.upsert_trust_profile(_profile_data(**kwargs))

    def test_empty_returns_empty_list(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        assert db.list_trust_adjustments(pid) == []

    def test_returns_all_for_profile(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        for _ in range(3):
            db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid)
        assert len(rows) == 3

    def test_ordered_desc_by_created_at(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        timestamps = [
            "2024-01-01T00:00:00+00:00",
            "2024-06-01T00:00:00+00:00",
            "2024-03-01T00:00:00+00:00",
        ]
        for ts in timestamps:
            db.insert_trust_adjustment(_adjustment_data(pid, created_at=ts))
        rows = db.list_trust_adjustments(pid)
        order = [r["created_at"] for r in rows]
        # Should be sorted newest first
        assert order == sorted(order, reverse=True)

    def test_limit_respected(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        for _ in range(10):
            db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid, limit=3)
        assert len(rows) == 3

    def test_offset_paginates(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        timestamps = [
            "2024-10-0{}T00:00:00+00:00".format(i + 1) for i in range(5)
        ]
        for ts in timestamps:
            db.insert_trust_adjustment(_adjustment_data(pid, created_at=ts))
        page1 = db.list_trust_adjustments(pid, limit=2, offset=0)
        page2 = db.list_trust_adjustments(pid, limit=2, offset=2)
        ids_p1 = {r["id"] for r in page1}
        ids_p2 = {r["id"] for r in page2}
        assert ids_p1.isdisjoint(ids_p2)

    def test_default_limit_is_100(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        for _ in range(120):
            db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid)
        assert len(rows) == 100

    def test_profile_isolation(self) -> None:
        """Adjustments for profile A are not returned when querying profile B."""
        db = _make_db()
        pid_a = self._setup_profile(db, repo="owner/repo-a")
        pid_b = self._setup_profile(db, repo="owner/repo-b")
        for _ in range(3):
            db.insert_trust_adjustment(_adjustment_data(pid_a))
        db.insert_trust_adjustment(_adjustment_data(pid_b, reason="revert"))
        rows_a = db.list_trust_adjustments(pid_a)
        rows_b = db.list_trust_adjustments(pid_b)
        assert len(rows_a) == 3
        assert len(rows_b) == 1
        assert all(r["profile_id"] == pid_a for r in rows_a)
        assert all(r["profile_id"] == pid_b for r in rows_b)

    def test_row_contains_expected_fields(self) -> None:
        db = _make_db()
        pid = self._setup_profile(db)
        db.insert_trust_adjustment(_adjustment_data(pid))
        rows = db.list_trust_adjustments(pid)
        expected_keys = {
            "id", "profile_id", "delta", "reason",
            "run_id", "score_before", "score_after", "created_at",
        }
        assert expected_keys <= set(rows[0].keys())


# ===========================================================================
# TestModuleExports
# ===========================================================================


class TestModuleExports:
    """Verify __init__.py exports TrustProfile and TrustConfig."""

    def test_trust_profile_in_all(self) -> None:
        import orchestration_engine as oe
        assert "TrustProfile" in oe.__all__

    def test_trust_config_in_all(self) -> None:
        import orchestration_engine as oe
        assert "TrustConfig" in oe.__all__

    def test_trust_profile_importable_from_package(self) -> None:
        from orchestration_engine import TrustProfile  # noqa: F401
        assert TrustProfile is not None

    def test_trust_config_importable_from_package(self) -> None:
        from orchestration_engine import TrustConfig  # noqa: F401
        assert TrustConfig is not None

    def test_trust_profile_importable_from_module(self) -> None:
        from orchestration_engine.trust import TrustProfile  # noqa: F401
        assert TrustProfile is not None

    def test_trust_config_importable_from_module(self) -> None:
        from orchestration_engine.trust import TrustConfig  # noqa: F401
        assert TrustConfig is not None
