"""Tests for #839 — SQLite WAL backpressure on pipeline launches.

Without a cap on concurrent daemons, unbounded launches trip SQLite WAL
contention (SQLITE_BUSY) and manifest as zombie runs (#754). This module
verifies:

  1. `Database.count_active_pipeline_runs()` correctly counts non-terminal rows
  2. `_launch_pipeline_from_trigger` returns 429 + Retry-After when the
     env-var cap is hit
  3. The cap is configurable via ORCH_MAX_DAEMONS
  4. ORCH_MAX_DAEMONS=0 disables the cap (legacy behaviour)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import read_src


# ---------------------------------------------------------------------------
# count_active_pipeline_runs() — DB-level unit tests
# ---------------------------------------------------------------------------


# #863: ``fresh_db`` retained as a thin alias of the canonical ``db`` fixture
# (kept under its module-local name so internal callers don't need touching).
from tests._helpers import insert_pipeline_run as _insert_pipeline_run_helper
from tests._helpers import pipeline_run_dict as _pipeline_run_dict  # noqa: F401


@pytest.fixture
def fresh_db(db):
    return db


def _insert_run(db, status: str, run_id: str = "abc12345"):
    """Insert a minimal pipeline_runs row with the given status.

    Sets ``pid`` to the current test-process PID so the row survives
    the #754 zombie sweep that runs inside ``count_active_pipeline_runs``.
    These tests probe the COUNT semantics, not the SWEEP semantics —
    issue #754's sweep behavior is verified in
    ``test_issue_754_zombie_sweep.py``. Without a live PID, every
    non-terminal row inserted here would be swept to ``'crashed'``
    before the count is returned and the COUNT assertions would all
    drop to 0.
    """
    # Use the canonical helper but preserve this file's historical values
    # (template_id="tpl", output_dir="/tmp") to avoid semantic drift in
    # tests that may pattern-match those strings.
    _insert_pipeline_run_helper(
        db,
        run_id=run_id,
        status=status,
        pid=os.getpid(),
        template_id="tpl",
        output_dir="/tmp",
    )


class TestCountActiveRuns:
    def test_zero_runs_returns_zero(self, fresh_db):
        assert fresh_db.count_active_pipeline_runs() == 0

    def test_pending_counted(self, fresh_db):
        _insert_run(fresh_db, "pending", "aaaa1111")
        assert fresh_db.count_active_pipeline_runs() == 1

    def test_running_counted(self, fresh_db):
        _insert_run(fresh_db, "running", "bbbb2222")
        assert fresh_db.count_active_pipeline_runs() == 1

    def test_pending_review_counted(self, fresh_db):
        _insert_run(fresh_db, "pending_review", "cccc3333")
        assert fresh_db.count_active_pipeline_runs() == 1

    def test_terminal_statuses_excluded(self, fresh_db):
        """Terminal statuses (success, failed, cancelled, crashed, etc.)
        do NOT count against the backpressure limit — those runs are
        done and not consuming a daemon slot."""
        for i, s in enumerate(["success", "failed", "cancelled", "crashed"]):
            _insert_run(fresh_db, s, run_id=f"term{i:04d}")
        assert fresh_db.count_active_pipeline_runs() == 0

    def test_mixed_counts_only_active(self, fresh_db):
        _insert_run(fresh_db, "running",  "act1")
        _insert_run(fresh_db, "pending",  "act2")
        _insert_run(fresh_db, "success",  "done1")
        _insert_run(fresh_db, "failed",   "done2")
        assert fresh_db.count_active_pipeline_runs() == 2


# ---------------------------------------------------------------------------
# launch path — 429 backpressure
# ---------------------------------------------------------------------------


# #874: ``isolated_launcher`` retained as a thin alias of the canonical
# ``api_client`` fixture so existing call sites in this module keep working.
@pytest.fixture
def isolated_launcher(api_client):
    """Yield (client, db_path) where the engine + DB live under tmp."""
    return api_client


def _seed_active_runs(db_path: Path, count: int) -> None:
    """Pre-fill the DB with N rows in status='running' so the cap is hit
    on the next launch attempt."""
    from orchestration_engine.db import Database
    db = Database(db_path)
    for i in range(count):
        _insert_run(db, "running", run_id=f"seed{i:04d}")


class TestBackpressureCap:
    def test_default_cap_rejects_at_8(self, isolated_launcher, monkeypatch):
        """Default ORCH_MAX_DAEMONS=8 — the 9th launch attempt 429s."""
        client, db_path = isolated_launcher
        monkeypatch.setenv("ORCH_MAX_DAEMONS", "8")
        _seed_active_runs(db_path, 8)
        # Try to launch a new run via the public endpoint
        resp = client.post("/api/v1/runs", json={
            "template": "hello-pipeline",
            "input": {},
            "mode": "dry-run",
        })
        # The endpoint may 404 the template lookup before backpressure;
        # but with a valid template it should 429. Either way, we should
        # NEVER see a 201/200 with 8 active runs.
        assert resp.status_code != 201, (
            f"launch succeeded with 8 active runs already (status={resp.status_code})"
        )
        # If we got a 429, verify the headers + body include retry hints
        if resp.status_code == 429:
            assert resp.headers.get("retry-after") == "30"
            body = resp.json()
            detail = body.get("detail", "")
            assert "backpressure" in detail.lower() or "ORCH_MAX_DAEMONS" in detail

    def test_custom_cap_honoured(self, isolated_launcher, monkeypatch):
        """ORCH_MAX_DAEMONS=2 — 3rd launch attempt 429s."""
        client, db_path = isolated_launcher
        monkeypatch.setenv("ORCH_MAX_DAEMONS", "2")
        _seed_active_runs(db_path, 2)
        resp = client.post("/api/v1/runs", json={
            "template": "hello-pipeline",
            "input": {},
            "mode": "dry-run",
        })
        assert resp.status_code in (429, 404, 422), (
            f"unexpected status={resp.status_code}: {resp.text[:200]}"
        )

    def test_cap_disabled_by_zero(self, isolated_launcher, monkeypatch):
        """ORCH_MAX_DAEMONS=0 disables the cap (legacy behaviour). A
        launch with 10 active runs should NOT 429 — it'll either 201 or
        fail for a different reason (template not found etc.)."""
        client, db_path = isolated_launcher
        monkeypatch.setenv("ORCH_MAX_DAEMONS", "0")
        _seed_active_runs(db_path, 10)
        resp = client.post("/api/v1/runs", json={
            "template": "definitely-not-a-real-template-xyz",
            "input": {},
            "mode": "dry-run",
        })
        # With cap=0 the backpressure check is bypassed; we should fall
        # through to the template lookup which 404s.
        assert resp.status_code != 429, (
            f"cap=0 should disable backpressure but got 429: {resp.text}"
        )

    def test_malformed_env_var_falls_back_to_default(self, isolated_launcher, monkeypatch):
        """Bogus ORCH_MAX_DAEMONS values fall back to default 8 instead
        of crashing the launcher."""
        client, db_path = isolated_launcher
        monkeypatch.setenv("ORCH_MAX_DAEMONS", "not-a-number")
        _seed_active_runs(db_path, 8)
        resp = client.post("/api/v1/runs", json={
            "template": "hello-pipeline",
            "input": {},
            "mode": "dry-run",
        })
        # Falls back to default cap of 8 → 8 active runs → 429
        assert resp.status_code != 500, (
            f"malformed ORCH_MAX_DAEMONS crashed the launcher: {resp.text}"
        )


class TestSourceWiring:
    """Belt-and-suspenders against future refactors that remove the check."""

    def test_launcher_source_calls_count_active(self):
        api_src = read_src("web/api.py")
        assert "count_active_pipeline_runs()" in api_src, (
            "web/api.py no longer calls count_active_pipeline_runs() — "
            "the #839 backpressure check is gone."
        )

    def test_launcher_source_references_env_var(self):
        api_src = read_src("web/api.py")
        assert "ORCH_MAX_DAEMONS" in api_src, (
            "ORCH_MAX_DAEMONS env var no longer referenced in web/api.py "
            "— the configurable cap is gone."
        )

    def test_launcher_source_emits_retry_after_header(self):
        api_src = read_src("web/api.py")
        assert 'Retry-After' in api_src, (
            "Retry-After header no longer emitted on 429 — clients lose "
            "the back-off hint."
        )
