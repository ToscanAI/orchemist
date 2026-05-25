"""Tests for #838 — admin_audit_log table + endpoint.

Every PUT to /api/v1/admin/feature-flags must append an audit row
recording the before/after values, the keys that actually changed,
and the OS pid that served the request. The GET endpoint returns the
rows newest-first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_admin(tmp_path, monkeypatch):
    """Point both the runtime reader (feature_flags) AND the admin API
    write handler at a tmp admin.json. Yields (client, admin_path)."""
    from orchestration_engine import feature_flags as ff
    from orchestration_engine.web.api import create_api_app

    admin_path = tmp_path / "admin.json"
    monkeypatch.setenv("ORCH_ADMIN_PATH", str(admin_path))
    # Also point engine.db at tmp so the audit log doesn't pollute
    # ~/.orchestration-engine/engine.db
    db_path = tmp_path / "engine.db"
    monkeypatch.setenv("ORCH_DB_PATH", str(db_path))
    ff.reset_cache()
    yield TestClient(create_api_app(db_path=str(db_path))), admin_path
    ff.reset_cache()


class TestAuditLogTableExists:
    def test_table_present_after_db_init(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        with db.transaction() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='admin_audit_log'"
            )
            assert cur.fetchone() is not None, (
                "admin_audit_log table missing from fresh DB — "
                "_create_table_admin_audit_log not called by _init_db"
            )

    def test_index_present(self, tmp_path):
        """The created_at index makes 'recent N rows' queries fast."""
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        with db.transaction() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_admin_audit_log_created_at'"
            )
            assert cur.fetchone() is not None


class TestAppendAdminAudit:
    def test_append_and_read_round_trip(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        row_id = db.append_admin_audit(
            action="update_feature_flags",
            target="phase0_hard_gate",
            before={"phase0_hard_gate": False},
            after={"phase0_hard_gate": True},
            source_pid=12345,
        )
        assert row_id > 0
        rows = db.list_admin_audit(limit=10)
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "update_feature_flags"
        assert r["target"] == "phase0_hard_gate"
        assert r["before"] == {"phase0_hard_gate": False}
        assert r["after"] == {"phase0_hard_gate": True}
        assert r["source_pid"] == 12345

    def test_source_pid_defaults_to_current_process(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        db.append_admin_audit(action="x", target="y")
        rows = db.list_admin_audit(limit=1)
        assert rows[0]["source_pid"] == os.getpid()

    def test_before_after_can_be_none(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        db.append_admin_audit(action="init", target="bootstrap")
        rows = db.list_admin_audit(limit=1)
        assert rows[0]["before"] is None
        assert rows[0]["after"] is None

    def test_rows_ordered_newest_first(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        ids = []
        for n in range(5):
            ids.append(db.append_admin_audit(
                action=f"action_{n}", target=f"target_{n}"
            ))
        rows = db.list_admin_audit(limit=10)
        # newest first → reverse of insertion order
        assert [r["id"] for r in rows] == list(reversed(ids))

    def test_limit_and_offset(self, tmp_path):
        from orchestration_engine.db import Database
        db = Database(tmp_path / "engine.db")
        for n in range(10):
            db.append_admin_audit(action="x", target=f"t{n}")
        page1 = db.list_admin_audit(limit=3, offset=0)
        page2 = db.list_admin_audit(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


class TestPutFeatureFlagsWritesAudit:
    def test_single_flag_change_appends_one_row(self, isolated_admin):
        client, admin_path = isolated_admin
        resp = client.put(
            "/api/v1/admin/feature-flags",
            json={"phase0_hard_gate": True},
        )
        assert resp.status_code == 200, resp.text
        # Audit row should be visible immediately
        rows = client.get("/api/v1/admin/audit-log?limit=10").json()["rows"]
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "update_feature_flags"
        assert r["target"] == "phase0_hard_gate"
        assert r["before"] == {"phase0_hard_gate": False}  # was default
        assert r["after"] == {"phase0_hard_gate": True}

    def test_multi_flag_change_records_changed_keys(self, isolated_admin):
        client, _ = isolated_admin
        resp = client.put(
            "/api/v1/admin/feature-flags",
            json={
                "phase0_hard_gate": True,
                "dialogue_phase": True,
                "extend_verdict": True,  # ALREADY True by default → NOT changed
            },
        )
        assert resp.status_code == 200
        rows = client.get("/api/v1/admin/audit-log").json()["rows"]
        assert len(rows) == 1
        r = rows[0]
        # Only the keys that ACTUALLY changed value are recorded
        target_keys = set(r["target"].split(","))
        assert target_keys == {"phase0_hard_gate", "dialogue_phase"}
        assert r["before"] == {
            "phase0_hard_gate": False, "dialogue_phase": False
        }
        assert r["after"] == {
            "phase0_hard_gate": True, "dialogue_phase": True
        }

    def test_noop_put_does_not_append_audit(self, isolated_admin):
        """A PUT that doesn't change any value should leave the log alone —
        otherwise repeated UI saves spam the table with empty diffs."""
        client, _ = isolated_admin
        # First write — establishes the baseline
        client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        # Second write with the SAME value
        client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        rows = client.get("/api/v1/admin/audit-log").json()["rows"]
        assert len(rows) == 1  # only the first write logged

    def test_audit_endpoint_query_validation(self, isolated_admin):
        client, _ = isolated_admin
        # limit out of range
        assert client.get("/api/v1/admin/audit-log?limit=0").status_code == 400
        assert client.get("/api/v1/admin/audit-log?limit=2000").status_code == 400
        # negative offset
        assert client.get("/api/v1/admin/audit-log?offset=-1").status_code == 400

    def test_audit_log_resilient_when_db_write_fails(
        self, isolated_admin, monkeypatch, caplog
    ):
        """If the audit append fails, the admin PUT must still succeed
        — audit is best-effort, not gating. A failed audit logs a warning."""
        import logging
        from orchestration_engine.db import Database
        client, _ = isolated_admin

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated DB outage")
        monkeypatch.setattr(Database, "append_admin_audit", _raise)

        with caplog.at_level(logging.WARNING):
            resp = client.put(
                "/api/v1/admin/feature-flags",
                json={"phase0_hard_gate": True},
            )
        assert resp.status_code == 200  # PUT still succeeded
        assert any("audit-log append failed" in r.message for r in caplog.records)


class TestConfigSurfaceDocExists:
    """The audit log is useless without operator documentation telling
    them where to find it. CONFIG-SURFACE.md is the contract."""

    def test_config_surface_md_present(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "docs" / "CONFIG-SURFACE.md"
        )
        assert path.exists(), "docs/CONFIG-SURFACE.md is missing — #838 deliverable"
        body = path.read_text()
        # Must document admin.json AND the new audit-log table
        assert "admin.json" in body
        assert "admin_audit_log" in body
        # Must tell operators how to query the log
        assert "GET /api/v1/admin/audit-log" in body or "/api/v1/admin/audit-log" in body
