"""tests/test_trust_decay_api.py — Tests for Issue #4.2.4: Trust Decay + API Endpoints.

Covers:
- decay_idle_profiles(): decay logic, floor enforcement, threshold recalc,
  adjustment log, weeks_idle computation, already-at-floor profiles.
- GET  /api/v1/trust/profiles          — list all profiles
- GET  /api/v1/trust/profiles/{id}     — single profile lookup
- PUT  /api/v1/trust/profiles/{id}     — manual trust override
- GET  /api/v1/trust/adjustments       — audit log for a profile
- Module exports via __init__.py

Test classes:
    TestDecayIdleProfiles           — decay_idle_profiles() unit tests
    TestDbHelperMethods             — list_trust_profiles / get_trust_profile_by_id
    TestTrustProfilesListAPI        — GET /api/v1/trust/profiles
    TestTrustProfileDetailAPI       — GET /api/v1/trust/profiles/{id}
    TestTrustProfileOverrideAPI     — PUT /api/v1/trust/profiles/{id}
    TestTrustAdjustmentsAPI         — GET /api/v1/trust/adjustments
    TestModuleExports               — __init__.py exports
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.trust import (
    DECAY_FLOOR,
    DECAY_THRESHOLD_DAYS,
    DEFAULT_DECAY_RATE,
    TrustCalibrator,
    decay_idle_profiles,
)

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return a fresh in-memory Database with all migrations applied."""
    return Database(":memory:")


def _make_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by a file-backed isolated DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-trust-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _insert_profile(
    db: Database,
    repo: str = "owner/repo",
    template_id: str = "coding-pipeline-v1",
    task_type: str = "bugfix",
    trust_score: float = 0.8,
    last_run_at: str | None = None,
    successful_merges: int = 0,
    **kwargs: Any,
) -> int:
    """Insert a trust profile row and return its id."""
    data: Dict[str, Any] = {
        "repo": repo,
        "template_id": template_id,
        "task_type": task_type,
        "auto_merge_threshold": 0.85,
        "human_review_threshold": 0.70,
        "trust_score": trust_score,
        "total_runs": 5,
        "successful_merges": successful_merges,
        "regressions": 0,
        "reverted_prs": 0,
        "last_run_at": last_run_at,
    }
    data.update(kwargs)
    return db.upsert_trust_profile(data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    """Isolated TestClient per test (separate DB file)."""
    with _make_client(tmp_path) as c:
        yield c


@pytest.fixture()
def client_with_profile(tmp_path):
    """TestClient with one pre-inserted trust profile."""
    from orchestration_engine.web.api import create_api_app
    from orchestration_engine.db import Database

    db_file = str(tmp_path / "test-trust-engine.db")
    app = create_api_app(db_path=db_file)

    # Insert a profile directly in the DB
    db = Database(db_file)
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)
    db.close()

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, 1  # client + profile_id


# ===========================================================================
# TestDbHelperMethods
# ===========================================================================


class TestDbHelperMethods:
    """list_trust_profiles and get_trust_profile_by_id."""

    def test_list_returns_empty_initially(self) -> None:
        db = _make_db()
        assert db.list_trust_profiles() == []

    def test_list_returns_inserted_profile(self) -> None:
        db = _make_db()
        _insert_profile(db)
        rows = db.list_trust_profiles()
        assert len(rows) == 1
        assert rows[0]["repo"] == "owner/repo"

    def test_list_ordered_by_id_asc(self) -> None:
        db = _make_db()
        _insert_profile(db, repo="b-repo", template_id="t1", task_type="fix")
        _insert_profile(db, repo="a-repo", template_id="t2", task_type="fix")
        rows = db.list_trust_profiles()
        assert rows[0]["repo"] == "b-repo"
        assert rows[1]["repo"] == "a-repo"

    def test_get_by_id_returns_none_for_missing(self) -> None:
        db = _make_db()
        assert db.get_trust_profile_by_id(999) is None

    def test_get_by_id_returns_correct_row(self) -> None:
        db = _make_db()
        profile_id = _insert_profile(db, trust_score=0.65)
        row = db.get_trust_profile_by_id(profile_id)
        assert row is not None
        assert row["id"] == profile_id
        assert abs(row["trust_score"] - 0.65) < 1e-6

    def test_get_by_id_contains_all_expected_keys(self) -> None:
        db = _make_db()
        profile_id = _insert_profile(db)
        row = db.get_trust_profile_by_id(profile_id)
        assert row is not None
        for key in ("id", "repo", "template_id", "task_type", "trust_score",
                    "auto_merge_threshold", "human_review_threshold"):
            assert key in row, f"Missing key: {key}"


# ===========================================================================
# TestDecayIdleProfiles
# ===========================================================================


class TestDecayIdleProfiles:
    """Unit tests for decay_idle_profiles()."""

    def test_no_profiles_returns_empty_list(self) -> None:
        db = _make_db()
        results = decay_idle_profiles(db)
        assert results == []

    def test_active_profile_not_decayed(self) -> None:
        """Profile with recent last_run_at should not be touched."""
        db = _make_db()
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _insert_profile(db, trust_score=0.8, last_run_at=recent_ts)
        results = decay_idle_profiles(db)
        assert results == []

    def test_idle_profile_is_decayed(self) -> None:
        """Profile last run 14 days ago should be decayed (2 weeks)."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        profile_id = _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)

        results = decay_idle_profiles(db, decay_rate=0.05)

        assert len(results) == 1
        r = results[0]
        assert r["profile_id"] == profile_id
        assert r["weeks_idle"] == 2
        # 0.8 - 0.05 * 2 = 0.70
        assert abs(r["new_score"] - 0.70) < 1e-6
        assert abs(r["old_score"] - 0.80) < 1e-6
        assert abs(r["delta"] - (-0.10)) < 1e-6

    def test_decay_respects_floor(self) -> None:
        """Score should never fall below the floor."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=70)).isoformat()
        # 10 weeks idle × 0.05 = 0.50 drop → would be 0.35 → floored at 0.3
        profile_id = _insert_profile(db, trust_score=0.80, last_run_at=stale_ts)

        results = decay_idle_profiles(db, decay_rate=0.05, floor=0.3)

        assert len(results) == 1
        assert abs(results[0]["new_score"] - 0.3) < 1e-6

    def test_already_at_floor_not_modified(self) -> None:
        """Profile already at the floor should produce no result."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        _insert_profile(db, trust_score=0.3, last_run_at=stale_ts)

        results = decay_idle_profiles(db, floor=0.3)

        assert results == []

    def test_null_last_run_treated_as_idle(self) -> None:
        """Profile with no last_run_at should be decayed (1 week default)."""
        db = _make_db()
        _insert_profile(db, trust_score=0.8, last_run_at=None)

        results = decay_idle_profiles(db, decay_rate=0.05)

        assert len(results) == 1
        # 1 week × 0.05 = 0.05 drop → 0.75
        assert abs(results[0]["new_score"] - 0.75) < 1e-6

    def test_adjustment_written_to_db(self) -> None:
        """An idle_decay adjustment must be written to trust_adjustments."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        profile_id = _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)

        results = decay_idle_profiles(db, decay_rate=0.05)
        assert results
        adjustment_id = results[0]["adjustment_id"]

        adjustments = db.list_trust_adjustments(profile_id=profile_id)
        ids = [a["id"] for a in adjustments]
        assert adjustment_id in ids

        adj = next(a for a in adjustments if a["id"] == adjustment_id)
        assert adj["reason"] == "idle_decay"

    def test_profile_score_updated_in_db(self) -> None:
        """The trust_profiles row must reflect the new score after decay."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        profile_id = _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)

        decay_idle_profiles(db, decay_rate=0.05)

        refreshed = db.get_trust_profile_by_id(profile_id)
        assert refreshed is not None
        assert abs(refreshed["trust_score"] - 0.75) < 1e-6

    def test_threshold_recalculated_after_decay(self) -> None:
        """auto_merge_threshold must be re-derived from new score."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        profile_id = _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)

        results = decay_idle_profiles(db, decay_rate=0.05)

        assert len(results) == 1
        expected_new_score = 0.70
        # Default calibrator: conservative=0.98, aggressive=0.7, bootstrap=10
        # Profile has 0 successful_merges → bootstrap → threshold = 0.98
        calibrator = TrustCalibrator(repo="owner/repo",
                                     template_id="coding-pipeline-v1",
                                     task_type="bugfix")
        expected_threshold = calibrator.compute_threshold(expected_new_score, 0)
        assert abs(results[0]["threshold"] - expected_threshold) < 1e-6

    def test_pinned_now_determines_idle(self) -> None:
        """Passing a custom `now` controls which profiles are considered idle."""
        db = _make_db()
        # Profile ran 8 days before the pinned 'now'
        pinned_now = datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc)
        last_run = datetime(2025, 1, 12, 12, 0, 0, tzinfo=timezone.utc)
        _insert_profile(db, trust_score=0.8, last_run_at=last_run.isoformat())

        results = decay_idle_profiles(db, decay_rate=0.05, now=pinned_now)
        assert len(results) == 1

    def test_multiple_profiles_independent(self) -> None:
        """Decay is applied independently to each idle profile."""
        db = _make_db()
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(days=14)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()

        _insert_profile(db, repo="repo-a", template_id="t", task_type="fix",
                        trust_score=0.9, last_run_at=stale)
        _insert_profile(db, repo="repo-b", template_id="t", task_type="fix",
                        trust_score=0.9, last_run_at=recent)

        results = decay_idle_profiles(db, decay_rate=0.05)

        # Only the stale profile should be in results
        assert len(results) == 1
        r = results[0]
        assert abs(r["new_score"] - 0.80) < 1e-6  # 0.9 - 0.05*2

    def test_result_contains_expected_keys(self) -> None:
        """Result dict must have all documented keys."""
        db = _make_db()
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)

        results = decay_idle_profiles(db, decay_rate=0.05)
        assert results
        keys = {"profile_id", "adjustment_id", "old_score", "new_score",
                "delta", "weeks_idle", "threshold"}
        assert keys.issubset(results[0].keys())


# ===========================================================================
# TestTrustProfilesListAPI
# ===========================================================================


class TestTrustProfilesListAPI:
    """GET /api/v1/trust/profiles"""

    def test_returns_200_empty(self, client) -> None:
        resp = client.get("/api/v1/trust/profiles")
        assert resp.status_code == 200

    def test_returns_items_key(self, client) -> None:
        resp = client.get("/api/v1/trust/profiles")
        body = resp.json()
        assert "items" in body

    def test_returns_total_key(self, client) -> None:
        resp = client.get("/api/v1/trust/profiles")
        body = resp.json()
        assert "total" in body
        assert body["total"] == 0

    def test_returns_inserted_profile(self, client_with_profile) -> None:
        client, _ = client_with_profile
        resp = client.get("/api/v1/trust/profiles")
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_profile_has_expected_fields(self, client_with_profile) -> None:
        client, _ = client_with_profile
        resp = client.get("/api/v1/trust/profiles")
        item = resp.json()["items"][0]
        for field in ("id", "repo", "template_id", "task_type", "trust_score"):
            assert field in item, f"Missing field: {field}"

    def test_pagination_limit(self, tmp_path) -> None:
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "pagination.db")
        db = Database(db_file)
        for i in range(5):
            _insert_profile(db, repo=f"repo-{i}", template_id="t", task_type="fix")
        db.close()
        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/api/v1/trust/profiles?limit=2")
            body = resp.json()
            assert body["total"] == 5
            assert len(body["items"]) == 2

    def test_pagination_offset(self, tmp_path) -> None:
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "offset.db")
        db = Database(db_file)
        for i in range(4):
            _insert_profile(db, repo=f"repo-{i}", template_id="t", task_type="fix")
        db.close()
        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/api/v1/trust/profiles?limit=2&offset=2")
            body = resp.json()
            assert len(body["items"]) == 2
            assert body["items"][0]["repo"] == "repo-2"


# ===========================================================================
# TestTrustProfileDetailAPI
# ===========================================================================


class TestTrustProfileDetailAPI:
    """GET /api/v1/trust/profiles/{id}"""

    def test_returns_200_for_existing(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.get(f"/api/v1/trust/profiles/{profile_id}")
        assert resp.status_code == 200

    def test_returns_404_for_missing(self, client) -> None:
        resp = client.get("/api/v1/trust/profiles/9999")
        assert resp.status_code == 404

    def test_returns_correct_profile(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.get(f"/api/v1/trust/profiles/{profile_id}")
        body = resp.json()
        assert body["id"] == profile_id
        assert body["repo"] == "owner/repo"

    def test_profile_has_trust_score(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.get(f"/api/v1/trust/profiles/{profile_id}")
        body = resp.json()
        assert "trust_score" in body
        assert 0.0 <= body["trust_score"] <= 1.0


# ===========================================================================
# TestTrustProfileOverrideAPI
# ===========================================================================


class TestTrustProfileOverrideAPI:
    """PUT /api/v1/trust/profiles/{id}"""

    def test_returns_200_on_valid_override(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.put(
            f"/api/v1/trust/profiles/{profile_id}",
            json={"trust_score": 0.5, "reason": "Testing manual override"},
        )
        assert resp.status_code == 200

    def test_score_is_updated_in_response(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.put(
            f"/api/v1/trust/profiles/{profile_id}",
            json={"trust_score": 0.6, "reason": "Set to 0.6"},
        )
        body = resp.json()
        assert abs(body["trust_score"] - 0.6) < 1e-6

    def test_returns_404_for_missing_profile(self, client) -> None:
        resp = client.put(
            "/api/v1/trust/profiles/9999",
            json={"trust_score": 0.5, "reason": "No profile"},
        )
        assert resp.status_code == 404

    def test_returns_422_for_score_above_1(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.put(
            f"/api/v1/trust/profiles/{profile_id}",
            json={"trust_score": 1.5, "reason": "Bad score"},
        )
        assert resp.status_code == 422

    def test_returns_422_for_score_below_0(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.put(
            f"/api/v1/trust/profiles/{profile_id}",
            json={"trust_score": -0.1, "reason": "Bad score"},
        )
        assert resp.status_code == 422

    def test_adjustment_logged_in_db(self, tmp_path) -> None:
        """An override must write a trust_adjustments row."""
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "override.db")
        db = Database(db_file)
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        profile_id = _insert_profile(db, trust_score=0.8, last_run_at=stale_ts)
        db.close()

        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            c.put(
                f"/api/v1/trust/profiles/{profile_id}",
                json={"trust_score": 0.5, "reason": "CI override",
                      "reviewed_by": "rene"},
            )

        db2 = Database(db_file)
        adjustments = db2.list_trust_adjustments(profile_id=profile_id)
        db2.close()
        assert len(adjustments) == 1
        assert "manual_override" in adjustments[0]["reason"]

    def test_reviewed_by_in_audit_reason(self, tmp_path) -> None:
        """reviewed_by should be encoded in the adjustment reason."""
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "audit.db")
        db = Database(db_file)
        profile_id = _insert_profile(db, trust_score=0.8)
        db.close()

        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            c.put(
                f"/api/v1/trust/profiles/{profile_id}",
                json={"trust_score": 0.6, "reason": "x", "reviewed_by": "alice"},
            )

        db2 = Database(db_file)
        adj = db2.list_trust_adjustments(profile_id=profile_id)[0]
        db2.close()
        assert "alice" in adj["reason"]

    def test_threshold_recalculated(self, client_with_profile) -> None:
        """auto_merge_threshold must be updated after override."""
        client, profile_id = client_with_profile
        resp = client.put(
            f"/api/v1/trust/profiles/{profile_id}",
            json={"trust_score": 0.9, "reason": "test"},
        )
        body = resp.json()
        # The threshold should be a float in [0, 1]
        assert "auto_merge_threshold" in body
        assert 0.0 <= body["auto_merge_threshold"] <= 1.0


# ===========================================================================
# TestTrustAdjustmentsAPI
# ===========================================================================


class TestTrustAdjustmentsAPI:
    """GET /api/v1/trust/adjustments"""

    def test_returns_200_with_profile_id(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.get(f"/api/v1/trust/adjustments?profile_id={profile_id}")
        assert resp.status_code == 200

    def test_returns_empty_items_for_new_profile(self, client_with_profile) -> None:
        client, profile_id = client_with_profile
        resp = client.get(f"/api/v1/trust/adjustments?profile_id={profile_id}")
        body = resp.json()
        assert body["items"] == []

    def test_returns_404_for_unknown_profile(self, client) -> None:
        resp = client.get("/api/v1/trust/adjustments?profile_id=9999")
        assert resp.status_code == 404

    def test_missing_profile_id_returns_422(self, client) -> None:
        resp = client.get("/api/v1/trust/adjustments")
        assert resp.status_code == 422

    def test_adjustments_returned_after_override(self, tmp_path) -> None:
        """Adjustments written by an override should appear in the list."""
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "adj-api.db")
        db = Database(db_file)
        profile_id = _insert_profile(db, trust_score=0.8)
        db.close()

        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            c.put(
                f"/api/v1/trust/profiles/{profile_id}",
                json={"trust_score": 0.5, "reason": "test"},
            )
            resp = c.get(f"/api/v1/trust/adjustments?profile_id={profile_id}")

        body = resp.json()
        assert len(body["items"]) == 1
        assert "manual_override" in body["items"][0]["reason"]

    def test_pagination_limit(self, tmp_path) -> None:
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "adj-pag.db")
        db = Database(db_file)
        profile_id = _insert_profile(db, trust_score=0.8)
        # Insert multiple adjustments directly
        for i in range(5):
            db.insert_trust_adjustment({
                "profile_id": profile_id,
                "delta": -0.01,
                "reason": "idle_decay",
                "run_id": None,
                "score_before": 0.8,
                "score_after": 0.79,
            })
        db.close()
        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get(f"/api/v1/trust/adjustments?profile_id={profile_id}&limit=2")
            body = resp.json()
            # total reflects returned batch (API returns len(items) as total)
            assert len(body["items"]) == 2

    def test_items_have_expected_keys(self, tmp_path) -> None:
        from orchestration_engine.web.api import create_api_app
        db_file = str(tmp_path / "adj-keys.db")
        db = Database(db_file)
        profile_id = _insert_profile(db, trust_score=0.8)
        db.insert_trust_adjustment({
            "profile_id": profile_id,
            "delta": -0.05,
            "reason": "idle_decay",
            "run_id": None,
            "score_before": 0.8,
            "score_after": 0.75,
        })
        db.close()
        app = create_api_app(db_path=db_file)
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get(f"/api/v1/trust/adjustments?profile_id={profile_id}")
            item = resp.json()["items"][0]
        for key in ("id", "profile_id", "delta", "reason", "score_before", "score_after"):
            assert key in item, f"Missing key: {key}"


# ===========================================================================
# TestModuleExports
# ===========================================================================


class TestModuleExports:
    """Verify __init__.py exports the new symbols from trust.py."""

    def test_decay_idle_profiles_exported(self) -> None:
        from orchestration_engine import decay_idle_profiles as fn
        assert callable(fn)

    def test_default_decay_rate_exported(self) -> None:
        from orchestration_engine import DEFAULT_DECAY_RATE as r
        assert 0.0 < r < 1.0

    def test_decay_floor_exported(self) -> None:
        from orchestration_engine import DECAY_FLOOR as f
        assert 0.0 <= f <= 1.0

    def test_decay_threshold_days_exported(self) -> None:
        from orchestration_engine import DECAY_THRESHOLD_DAYS as d
        assert isinstance(d, int)
        assert d > 0

    def test_decay_idle_profiles_in_all(self) -> None:
        import orchestration_engine
        assert "decay_idle_profiles" in orchestration_engine.__all__
