"""tests/test_cost_api.py — Integration tests for Issue #5.2.3: Cost API Endpoints.

Covers:
- GET /api/v1/costs/summary  — aggregated cost query with group_by, date filtering,
  pagination, and input validation.
- GET /api/v1/costs/run/{run_id} — per-phase cost breakdown for a specific run.

Test classes:
    TestCostSummaryAPI          — GET /api/v1/costs/summary
    TestCostRunBreakdownAPI     — GET /api/v1/costs/run/{run_id}
    TestCostDbMethods           — Unit tests for db.get_cost_summary / get_run_costs
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database

# Skip entire module when FastAPI / starlette is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by an isolated file-backed DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-cost-api.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _make_db(tmp_path: Path) -> Database:
    """Return a fresh file-backed Database with all migrations applied."""
    return Database(str(tmp_path / "test-cost-db.db"))


# #862: route through the canonical helper so a future schema column is
# automatically picked up. ``INSERT OR IGNORE`` is preserved by guarding the
# call against pre-existing rows (callers of this helper insert each run_id
# at most once per test, but the original used OR IGNORE defensively).
def _insert_run(db: Database, run_id: str, template_id: str = "coding-pipeline-v1") -> None:
    """Insert a minimal pipeline_runs row so FK constraints pass."""
    from tests._helpers import insert_pipeline_run as _impl
    if db.get_pipeline_run(run_id) is not None:
        return  # mimic INSERT OR IGNORE
    _impl(
        db,
        run_id=run_id,
        template_id=template_id,
        template_path="templates/test.yaml",
        mode="local",
        output_dir="/tmp/out",
        status="completed",
    )


def _insert_cost(
    db: Database,
    run_id: str,
    phase_id: str = "spec",
    model: str = "anthropic/claude-sonnet-4",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cost_usd: float = 0.01,
    created_at: str | None = None,
) -> None:
    """Insert a cost_tracking row directly (bypasses CostTracker)."""
    ts = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, ts),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    """Isolated TestClient per test (separate DB file)."""
    with _make_client(tmp_path) as c:
        yield c


@pytest.fixture()
def populated_client(tmp_path):
    """TestClient with pre-seeded cost data covering two runs and two models."""
    db_file = str(tmp_path / "test-cost-populated.db")
    db = Database(db_file)

    # Two runs
    _insert_run(db, "run-aaa", template_id="coding-pipeline-v1")
    _insert_run(db, "run-bbb", template_id="content-pipeline-v1")

    # run-aaa: 2 phases on 2026-01-10
    _insert_cost(db, "run-aaa", phase_id="spec",   model="anthropic/claude-sonnet-4", cost_usd=0.01, created_at="2026-01-10 08:00:00")
    _insert_cost(db, "run-aaa", phase_id="build",  model="anthropic/claude-haiku-4",  cost_usd=0.005, created_at="2026-01-10 09:00:00")

    # run-bbb: 1 phase on 2026-01-11
    _insert_cost(db, "run-bbb", phase_id="write",  model="anthropic/claude-sonnet-4", cost_usd=0.02, created_at="2026-01-11 10:00:00")

    from orchestration_engine.web.api import create_api_app
    app = create_api_app(db_path=db_file)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, db


# ---------------------------------------------------------------------------
# TestCostSummaryAPI — GET /api/v1/costs/summary
# ---------------------------------------------------------------------------


class TestCostSummaryAPI:
    """Tests for GET /api/v1/costs/summary."""

    def test_empty_database_returns_empty_items(self, client):
        """Empty DB returns 200 with empty items list."""
        resp = client.get("/api/v1/costs/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["limit"] == 20
        assert data["offset"] == 0

    def test_default_group_by_day(self, populated_client):
        """Default group_by=day returns one row per date, ordered DESC."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2  # two distinct dates
        days = [item["day"] for item in data["items"]]
        assert days == sorted(days, reverse=True)  # DESC order

    def test_group_by_template(self, populated_client):
        """group_by=template returns one row per template_id."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?group_by=template")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        template_ids = {item["template_id"] for item in data["items"]}
        assert "coding-pipeline-v1" in template_ids
        assert "content-pipeline-v1" in template_ids

    def test_group_by_model(self, populated_client):
        """group_by=model returns one row per model string."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?group_by=model")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        models = {item["model"] for item in data["items"]}
        assert "anthropic/claude-sonnet-4" in models
        assert "anthropic/claude-haiku-4" in models

    def test_start_date_filter(self, populated_client):
        """start_date filters out earlier dates."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?start_date=2026-01-11")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["day"] == "2026-01-11"

    def test_end_date_filter(self, populated_client):
        """end_date filters out later dates."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?end_date=2026-01-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["day"] == "2026-01-10"

    def test_date_range_filter(self, populated_client):
        """start_date + end_date combines into inclusive range."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?start_date=2026-01-10&end_date=2026-01-11")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_date_range_no_match(self, populated_client):
        """Date range outside all data returns empty items."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?start_date=2025-01-01&end_date=2025-01-31")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_pagination_limit(self, populated_client):
        """limit parameter caps the number of returned items."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["total"] == 2   # total unchanged
        assert data["limit"] == 1

    def test_pagination_offset(self, populated_client):
        """offset skips rows without affecting total."""
        client, _ = populated_client
        resp_p1 = client.get("/api/v1/costs/summary?limit=1&offset=0")
        resp_p2 = client.get("/api/v1/costs/summary?limit=1&offset=1")
        assert resp_p1.status_code == 200
        assert resp_p2.status_code == 200
        item_p1 = resp_p1.json()["items"][0]
        item_p2 = resp_p2.json()["items"][0]
        assert item_p1["day"] != item_p2["day"]

    def test_limit_clamped_to_max(self, client):
        """limit > 100 is silently clamped to 100."""
        resp = client.get("/api/v1/costs/summary?limit=9999")
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

    def test_limit_clamped_to_min(self, client):
        """limit <= 0 is silently clamped to 1."""
        resp = client.get("/api/v1/costs/summary?limit=0")
        assert resp.status_code == 200
        assert resp.json()["limit"] == 1

    def test_offset_negative_clamped(self, client):
        """Negative offset is silently clamped to 0."""
        resp = client.get("/api/v1/costs/summary?offset=-5")
        assert resp.status_code == 200
        assert resp.json()["offset"] == 0

    def test_invalid_group_by_returns_400(self, client):
        """Unknown group_by value returns 400."""
        resp = client.get("/api/v1/costs/summary?group_by=phase")
        assert resp.status_code == 400
        assert "group_by" in resp.json()["detail"].lower()

    def test_invalid_start_date_format_returns_400(self, client):
        """Malformed start_date returns 400."""
        resp = client.get("/api/v1/costs/summary?start_date=01-01-2026")
        assert resp.status_code == 400
        assert "start_date" in resp.json()["detail"].lower()

    def test_invalid_end_date_format_returns_400(self, client):
        """Malformed end_date returns 400."""
        resp = client.get("/api/v1/costs/summary?end_date=not-a-date")
        assert resp.status_code == 400
        assert "end_date" in resp.json()["detail"].lower()

    def test_aggregated_totals_correct(self, populated_client):
        """Day-level totals correctly sum cost_usd and token counts."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary?start_date=2026-01-10&end_date=2026-01-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["day"] == "2026-01-10"
        assert abs(item["total_cost"] - 0.015) < 1e-9
        assert item["phase_count"] == 2

    def test_response_schema_keys(self, populated_client):
        """Response contains required top-level keys."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/summary")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("items", "total", "limit", "offset"):
            assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# TestCostRunBreakdownAPI — GET /api/v1/costs/run/{run_id}
# ---------------------------------------------------------------------------


class TestCostRunBreakdownAPI:
    """Tests for GET /api/v1/costs/run/{run_id}."""

    def test_valid_run_returns_breakdown(self, populated_client):
        """Existing run with cost records returns 200 with phase items."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/run/run-aaa")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-aaa"
        assert len(data["items"]) == 2
        phase_ids = {item["phase_id"] for item in data["items"]}
        assert "spec" in phase_ids
        assert "build" in phase_ids

    def test_run_totals_computed_correctly(self, populated_client):
        """Total cost and token counts are summed across phases."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/run/run-aaa")
        assert resp.status_code == 200
        data = resp.json()
        assert abs(data["total_cost"] - 0.015) < 1e-9
        # 1000 + 1000 input, 500 + 500 output (defaults from _insert_cost)
        assert data["total_input_tokens"] == 2000
        assert data["total_output_tokens"] == 1000

    def test_items_ordered_by_created_at(self, populated_client):
        """Phase records are returned in chronological order."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/run/run-aaa")
        assert resp.status_code == 200
        items = resp.json()["items"]
        timestamps = [item["created_at"] for item in items]
        assert timestamps == sorted(timestamps)

    def test_unknown_run_returns_404(self, client):
        """Non-existent run_id returns 404."""
        resp = client.get("/api/v1/costs/run/does-not-exist")
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]

    def test_response_schema_keys(self, populated_client):
        """Response contains required top-level keys."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/run/run-bbb")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("run_id", "items", "total_cost", "total_input_tokens", "total_output_tokens"):
            assert key in data, f"Missing key: {key}"

    def test_single_phase_run(self, populated_client):
        """Run with a single phase returns one item."""
        client, _ = populated_client
        resp = client.get("/api/v1/costs/run/run-bbb")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["phase_id"] == "write"


# ---------------------------------------------------------------------------
# TestCostDbMethods — unit tests for Database methods
# ---------------------------------------------------------------------------


class TestCostDbMethods:
    """Unit tests for db.get_cost_summary, count_cost_summary, get_run_costs."""

    def test_get_cost_summary_empty(self, tmp_path):
        """Empty database returns empty list."""
        db = _make_db(tmp_path)
        result = db.get_cost_summary()
        assert result == []

    def test_count_cost_summary_empty(self, tmp_path):
        """Empty database returns count 0."""
        db = _make_db(tmp_path)
        assert db.count_cost_summary() == 0

    def test_get_run_costs_empty(self, tmp_path):
        """Unknown run_id returns empty list."""
        db = _make_db(tmp_path)
        assert db.get_run_costs("nonexistent-run") == []

    def test_get_cost_summary_group_by_day(self, tmp_path):
        """get_cost_summary groups correctly by day."""
        db = _make_db(tmp_path)
        _insert_run(db, "run-x")
        _insert_cost(db, "run-x", cost_usd=0.1, created_at="2026-03-01 10:00:00")
        _insert_cost(db, "run-x", cost_usd=0.2, created_at="2026-03-01 11:00:00")
        _insert_cost(db, "run-x", cost_usd=0.3, created_at="2026-03-02 10:00:00")

        result = db.get_cost_summary(group_by="day")
        assert len(result) == 2
        assert db.count_cost_summary(group_by="day") == 2

        # First item should be 2026-03-02 (DESC)
        assert result[0]["day"] == "2026-03-02"
        assert abs(result[0]["total_cost"] - 0.3) < 1e-9

    def test_get_cost_summary_date_filter(self, tmp_path):
        """start_date / end_date filters work at DB level."""
        db = _make_db(tmp_path)
        _insert_run(db, "run-y")
        _insert_cost(db, "run-y", cost_usd=0.1, created_at="2026-01-01 00:00:00")
        _insert_cost(db, "run-y", cost_usd=0.2, created_at="2026-06-15 00:00:00")

        result_jan = db.get_cost_summary(start_date="2026-01-01", end_date="2026-01-31")
        assert len(result_jan) == 1
        assert result_jan[0]["day"] == "2026-01-01"

    def test_get_cost_summary_group_by_model(self, tmp_path):
        """get_cost_summary groups correctly by model."""
        db = _make_db(tmp_path)
        _insert_run(db, "run-z")
        _insert_cost(db, "run-z", model="model-a", cost_usd=0.1)
        _insert_cost(db, "run-z", model="model-a", cost_usd=0.2)
        _insert_cost(db, "run-z", model="model-b", cost_usd=0.5)

        result = db.get_cost_summary(group_by="model")
        assert len(result) == 2
        # Ordered by total_cost DESC: model-b first
        assert result[0]["model"] == "model-b"
        assert abs(result[0]["total_cost"] - 0.5) < 1e-9
        assert result[1]["model"] == "model-a"
        assert abs(result[1]["total_cost"] - 0.3) < 1e-9

    def test_get_run_costs_returns_all_phases(self, tmp_path):
        """get_run_costs returns all phases for a run in order."""
        db = _make_db(tmp_path)
        _insert_run(db, "run-multi")
        _insert_cost(db, "run-multi", phase_id="spec",   created_at="2026-03-01 08:00:00")
        _insert_cost(db, "run-multi", phase_id="build",  created_at="2026-03-01 09:00:00")
        _insert_cost(db, "run-multi", phase_id="review", created_at="2026-03-01 10:00:00")

        costs = db.get_run_costs("run-multi")
        assert len(costs) == 3
        assert [r["phase_id"] for r in costs] == ["spec", "build", "review"]
