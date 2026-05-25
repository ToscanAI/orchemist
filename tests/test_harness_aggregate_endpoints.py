"""Tests for the harness aggregate endpoints (items 4, 6, 7 from the 2026-05-25 audit).

Endpoints under test:
    GET /api/v1/regressions
    GET /api/v1/stale-findings
    GET /api/v1/trust-profiles
    GET /api/v1/decisions
    GET /api/v1/admin/state
    PUT /api/v1/admin/feature-flags
"""

from __future__ import annotations

import json
import uuid as _uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestration_engine.db import Database
from orchestration_engine.web.api import create_api_app


@pytest.fixture
def client_and_db(tmp_path: Path, monkeypatch):
    # Sandbox the admin.json location so the test doesn't touch the user's
    # real ~/.orchestration-engine/admin.json.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    db_file = tmp_path / "engine.db"
    app = create_api_app(db_path=db_file)
    client = TestClient(app)
    return client, db_file, fake_home


# ── /api/v1/regressions ──────────────────────────────────────────────────────


class TestListRegressions:
    def test_empty_db_returns_empty_list(self, client_and_db):
        client, _, _ = client_and_db
        res = client.get("/api/v1/regressions")
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["limit"] == 50
        assert body["offset"] == 0

    def _insert(self, db, regression_id: str, status: str = "detected"):
        """Insert a regression row matching the actual `regressions` schema."""
        with db._locked():
            conn = db.get_connection()
            conn.execute(
                """INSERT INTO regressions
                   (id, commit_sha, ci_run_url, failure_type, affected_files, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (regression_id, "deadbeef" + regression_id, "https://ci/run/1",
                 "AssertionError in test_foo", '["src/foo.py"]', status),
            )
            conn.commit()

    def test_returns_inserted_regression(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        self._insert(db, "r1")
        res = client.get("/api/v1/regressions").json()
        assert res["total"] == 1
        item = res["items"][0]
        assert item["id"] == "r1"
        assert item["failure_type"] == "AssertionError in test_foo"
        # affected_files is deserialised by list_regressions
        assert item["affected_files"] == ["src/foo.py"]

    def test_status_filter(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        for status in ("detected", "fixing", "resolved"):
            self._insert(db, f"reg-{status}", status=status)
        assert client.get("/api/v1/regressions").json()["total"] == 3
        assert client.get("/api/v1/regressions?status=detected").json()["total"] == 1
        assert client.get("/api/v1/regressions?status=resolved").json()["total"] == 1
        assert client.get("/api/v1/regressions?status=zzzzz").json()["total"] == 0

    def test_clamps_limit(self, client_and_db):
        client, _, _ = client_and_db
        # Asking for limit=999 should clamp to 200
        body = client.get("/api/v1/regressions?limit=999").json()
        assert body["limit"] == 200
        # Limit=-5 should clamp to 1
        body = client.get("/api/v1/regressions?limit=-5").json()
        assert body["limit"] == 1


# ── /api/v1/stale-findings ───────────────────────────────────────────────────


class TestStaleFindings:
    def test_returns_empty_with_status_marker(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/stale-findings").json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["scan_status"] == "no_scanner_yet"


# ── /api/v1/trust-profiles ───────────────────────────────────────────────────


class TestTrustProfiles:
    def test_empty_db_returns_empty(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/trust-profiles").json()
        assert body == {"items": [], "total": 0}

    def test_returns_inserted_profile(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        # Trust profile insert uses get_or_create — exercise via that path.
        from orchestration_engine.trust import TrustConfig
        cfg = TrustConfig()
        # Use the low-level db method to keep this test focused on the endpoint.
        # We just need *a* trust_profiles row.
        with db._locked():
            conn = db.get_connection()
            conn.execute(
                """INSERT INTO trust_profiles
                   (repo, template_id, task_type, auto_merge_threshold,
                    human_review_threshold, trust_score, total_runs,
                    successful_merges, regressions, reverted_prs, last_run_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("ToscanAI/orchemist", "coding-pipeline-standard", "feature",
                 0.90, 0.70, 0.91, 42, 38, 0, 0, "2026-05-25T11:00:00Z"),
            )
            conn.commit()
        body = client.get("/api/v1/trust-profiles").json()
        assert body["total"] == 1
        p = body["items"][0]
        assert p["repo"] == "ToscanAI/orchemist"
        assert p["trust_score"] == 0.91
        assert p["auto_merge_threshold"] == 0.90


# ── /api/v1/decisions ────────────────────────────────────────────────────────


class TestDecisions:
    def test_empty_db_returns_empty_items(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/decisions").json()
        assert body["items"] == []

    def test_clamps_limit(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/decisions?limit=500").json()
        assert body["limit"] == 100


# ── /api/v1/admin/state + PUT /admin/feature-flags ──────────────────────────


class TestAdminState:
    def test_returns_defaults_when_no_file(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/admin/state").json()
        assert body["source"] == "default"
        assert body["autonomy_level"] == "4.3"
        assert body["feature_flags"]["phase0_hard_gate"] is False
        assert body["modes"]["openrouter"] is True

    def test_round_trip_flag_update(self, client_and_db):
        client, _, fake_home = client_and_db

        # Update a flag
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        assert res.status_code == 200
        assert res.json()["feature_flags"]["phase0_hard_gate"] is True

        # File should now exist
        admin_path = fake_home / ".orchestration-engine" / "admin.json"
        assert admin_path.exists()
        on_disk = json.loads(admin_path.read_text())
        assert on_disk["feature_flags"]["phase0_hard_gate"] is True

        # GET should now report source=file with the new value
        state = client.get("/api/v1/admin/state").json()
        assert state["source"] == "file"
        assert state["feature_flags"]["phase0_hard_gate"] is True

    def test_rejects_unknown_flag(self, client_and_db):
        client, _, _ = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json={"made_up_flag": True})
        assert res.status_code == 400
        assert "Unknown" in res.json()["detail"]

    def test_rejects_non_dict_body(self, client_and_db):
        client, _, _ = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json=["not", "a", "dict"])
        assert res.status_code == 400

    def test_coerces_truthy_to_bool(self, client_and_db):
        client, _, _ = client_and_db
        # Passing 1 / 0 should normalise to True / False
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": 1, "extend_verdict": 0})
        body = res.json()
        assert body["feature_flags"]["phase0_hard_gate"] is True
        assert body["feature_flags"]["extend_verdict"] is False

    def test_partial_file_falls_back_to_defaults(self, client_and_db):
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        # Partial file — only autonomy_level set, no flags
        (admin_dir / "admin.json").write_text(json.dumps({"autonomy_level": "5"}))
        body = client.get("/api/v1/admin/state").json()
        assert body["autonomy_level"] == "5"
        # Flags fall back to defaults
        assert body["feature_flags"]["phase0_hard_gate"] is False

    def test_unreadable_file_falls_back_with_default_source(self, client_and_db):
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text("not valid json {{{")
        body = client.get("/api/v1/admin/state").json()
        assert body["source"] == "default"
        assert body["autonomy_level"] == "4.3"
