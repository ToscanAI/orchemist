"""Tests for the Orchestration Engine REST API (Issue #257).

Uses FastAPI's TestClient — no real server process or daemon subprocess is
started.  Daemon spawning is patched out with unittest.mock so tests are
fast, deterministic, and offline-safe.

All tests are skipped when the optional [web] dependencies are absent.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Skip entire module when FastAPI / starlette is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from orchestration_engine import __version__  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by an isolated in-memory DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _fake_popen(*args, **kwargs) -> MagicMock:
    """Return a mock Popen object with a predictable PID."""
    mock = MagicMock()
    mock.pid = 99999
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    """Isolated TestClient per test (separate DB file)."""
    with _make_client(tmp_path) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        res = client.get("/api/v1/health")
        assert res.status_code == 200

    def test_returns_ok_status(self, client):
        data = client.get("/api/v1/health").json()
        assert data["status"] == "ok"

    def test_returns_version(self, client):
        data = client.get("/api/v1/health").json()
        assert data["version"] == __version__


# ---------------------------------------------------------------------------
# 2. Template listing  GET /api/v1/templates
# ---------------------------------------------------------------------------

class TestTemplateList:
    def test_returns_200(self, client):
        res = client.get("/api/v1/templates")
        assert res.status_code == 200

    def test_returns_list(self, client):
        data = client.get("/api/v1/templates").json()
        assert isinstance(data, list)

    def test_finds_at_least_one_template(self, client):
        data = client.get("/api/v1/templates").json()
        assert len(data) >= 1, "Should find at least one bundled template"

    def test_template_has_required_keys(self, client):
        data = client.get("/api/v1/templates").json()
        first = data[0]
        for key in ("id", "name", "version", "phases_count", "source", "phases"):
            assert key in first, f"Missing key '{key}' in template listing"

    def test_includes_content_pipeline(self, client):
        data = client.get("/api/v1/templates").json()
        ids = [t["id"] for t in data]
        assert "content-pipeline" in ids


# ---------------------------------------------------------------------------
# 3. Template detail  GET /api/v1/templates/{name}
# ---------------------------------------------------------------------------

class TestTemplateDetail:
    def test_returns_200_for_existing(self, client):
        res = client.get("/api/v1/templates/content-pipeline")
        assert res.status_code == 200

    def test_returns_404_for_nonexistent(self, client):
        res = client.get("/api/v1/templates/no-such-template-xyz")
        assert res.status_code == 404

    def test_detail_has_phases(self, client):
        data = client.get("/api/v1/templates/content-pipeline").json()
        assert "phases" in data
        assert len(data["phases"]) > 0

    def test_phase_has_required_keys(self, client):
        data = client.get("/api/v1/templates/content-pipeline").json()
        phase = data["phases"][0]
        for key in ("id", "name", "model_tier", "thinking_level", "depends_on"):
            assert key in phase, f"Phase missing key '{key}'"

    def test_top_level_fields(self, client):
        data = client.get("/api/v1/templates/content-pipeline").json()
        for key in ("id", "name", "version", "description", "phases", "config_schema"):
            assert key in data, f"Missing key '{key}' in template detail"


# ---------------------------------------------------------------------------
# 4. Launch a run  POST /api/v1/runs
# ---------------------------------------------------------------------------

class TestLaunchRun:
    """Tests for POST /api/v1/runs."""

    def test_returns_201(self, client, tmp_path):
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            res = client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "input": {"brief": "AI safety"},
                    "output_dir": str(tmp_path / "run-output"),
                },
            )
        assert res.status_code == 201, res.text

    def test_response_has_run_id(self, client, tmp_path):
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            data = client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / "run-output2"),
                },
            ).json()
        assert "run_id" in data
        assert len(data["run_id"]) == 8

    def test_response_has_expected_status(self, client, tmp_path):
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            data = client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / "run-output3"),
                },
            ).json()
        # Daemon is mocked — status starts as 'pending'
        assert data["status"] in ("pending", "running")

    def test_response_pid_set(self, client, tmp_path):
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            data = client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / "run-output4"),
                },
            ).json()
        assert data["pid"] == 99999

    def test_nonexistent_template_returns_404(self, client):
        res = client.post(
            "/api/v1/runs",
            json={"template": "no-such-template-xyz", "mode": "dry-run"},
        )
        assert res.status_code == 404

    def test_invalid_mode_returns_422(self, client):
        res = client.post(
            "/api/v1/runs",
            json={"template": "content-pipeline", "mode": "invalid-mode"},
        )
        assert res.status_code == 422

    def test_template_id_in_response(self, client, tmp_path):
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            data = client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / "run-output5"),
                },
            ).json()
        assert data["template_id"] == "content-pipeline"


# ---------------------------------------------------------------------------
# 5. Get run status  GET /api/v1/runs/{run_id}
# ---------------------------------------------------------------------------

class TestGetRun:
    def _launch(self, client, tmp_path) -> Dict[str, Any]:
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            return client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / "run-out"),
                },
            ).json()

    def test_returns_200_for_existing_run(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        res = client.get(f"/api/v1/runs/{run['run_id']}")
        assert res.status_code == 200

    def test_returns_404_for_nonexistent_run(self, client):
        res = client.get("/api/v1/runs/nonexistent00")
        assert res.status_code == 404

    def test_run_has_required_fields(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        data = client.get(f"/api/v1/runs/{run['run_id']}").json()
        for key in (
            "run_id", "status", "template_id", "mode", "output_dir",
            "completed_phases", "pid",
        ):
            assert key in data, f"Run response missing key '{key}'"

    def test_completed_phases_is_list(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        data = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert isinstance(data["completed_phases"], list)


# ---------------------------------------------------------------------------
# 6. List runs  GET /api/v1/runs
# ---------------------------------------------------------------------------

class TestListRuns:
    def _launch(self, client, tmp_path, suffix="") -> Dict[str, Any]:
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            return client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / f"run-out{suffix}"),
                },
            ).json()

    def test_returns_200(self, client):
        res = client.get("/api/v1/runs")
        assert res.status_code == 200

    def test_returns_dict_with_items_and_total(self, client):
        data = client.get("/api/v1/runs").json()
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)
        assert isinstance(data["total"], int)

    def test_empty_on_fresh_db(self, client):
        data = client.get("/api/v1/runs").json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_shows_launched_run(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        data = client.get("/api/v1/runs").json()
        ids = [r["run_id"] for r in data["items"]]
        assert run["run_id"] in ids

    def test_status_filter(self, client, tmp_path):
        self._launch(client, tmp_path, suffix="a")
        data = client.get("/api/v1/runs?status=pending").json()
        # All returned items must have status=pending
        for item in data["items"]:
            assert item["status"] == "pending"

    def test_template_id_filter(self, client, tmp_path):
        self._launch(client, tmp_path, suffix="b")
        data = client.get("/api/v1/runs?template_id=content-pipeline").json()
        for item in data["items"]:
            assert item["template_id"] == "content-pipeline"

    def test_pagination_limit(self, client, tmp_path):
        # Launch 3 runs
        for i in range(3):
            self._launch(client, tmp_path, suffix=str(i))
        data = client.get("/api/v1/runs?limit=2").json()
        assert len(data["items"]) <= 2
        assert data["limit"] == 2

    def test_pagination_offset(self, client, tmp_path):
        # Launch 3 runs
        for i in range(3):
            self._launch(client, tmp_path, suffix=f"offset{i}")
        all_data = client.get("/api/v1/runs?limit=10&offset=0").json()
        offset_data = client.get("/api/v1/runs?limit=10&offset=1").json()
        assert len(offset_data["items"]) == len(all_data["items"]) - 1

    def test_limit_capped_at_100(self, client):
        data = client.get("/api/v1/runs?limit=9999").json()
        assert data["limit"] == 100


# ---------------------------------------------------------------------------
# 7. Logs  GET /api/v1/runs/{run_id}/logs
# ---------------------------------------------------------------------------

class TestRunLogs:
    def _launch_with_log(self, client, tmp_path) -> Dict[str, Any]:
        out_dir = tmp_path / "run-with-log"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create a log file so the endpoint can serve it
        (out_dir / ".orch-daemon.log").write_text("test log entry\n")

        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            return client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(out_dir),
                },
            ).json()

    def test_returns_200_when_log_exists(self, client, tmp_path):
        run = self._launch_with_log(client, tmp_path)
        res = client.get(f"/api/v1/runs/{run['run_id']}/logs")
        assert res.status_code == 200

    def test_response_contains_log_key(self, client, tmp_path):
        run = self._launch_with_log(client, tmp_path)
        data = client.get(f"/api/v1/runs/{run['run_id']}/logs").json()
        assert "log" in data
        assert "run_id" in data

    def test_returns_404_for_nonexistent_run(self, client):
        res = client.get("/api/v1/runs/nosuchrun00/logs")
        assert res.status_code == 404

    def test_returns_404_when_log_missing(self, client, tmp_path):
        """Run exists in DB but log file is absent (e.g. output_dir was deleted)."""
        import uuid as _uuid
        from orchestration_engine.db import Database

        # Directly insert a run record pointing to a nonexistent output_dir
        # so the endpoint has no log file to serve.
        run_id = str(_uuid.uuid4())[:8]
        db = Database(tmp_path / "test-engine.db")
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": "/tmp/fake.yaml",
                "template_id": "fake-template",
                "input_json": "{}",
                "mode": "dry-run",
                "output_dir": str(tmp_path / "nonexistent-dir"),
                "status": "pending",
            }
        )

        res = client.get(f"/api/v1/runs/{run_id}/logs")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# 8. Cancel run  DELETE /api/v1/runs/{run_id}
# ---------------------------------------------------------------------------

class TestCancelRun:
    def _launch(self, client, tmp_path, suffix="") -> Dict[str, Any]:
        with patch("orchestration_engine.web.api.subprocess.Popen", side_effect=_fake_popen):
            return client.post(
                "/api/v1/runs",
                json={
                    "template": "content-pipeline",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / f"cancel-out{suffix}"),
                },
            ).json()

    def test_returns_200_for_pending_run(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        res = client.delete(f"/api/v1/runs/{run['run_id']}")
        assert res.status_code == 200

    def test_response_has_cancelled_true(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        data = client.delete(f"/api/v1/runs/{run['run_id']}").json()
        assert data["cancelled"] is True
        assert data["run_id"] == run["run_id"]

    def test_run_status_is_cancelled_after_delete(self, client, tmp_path):
        run = self._launch(client, tmp_path)
        client.delete(f"/api/v1/runs/{run['run_id']}")
        status_data = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert status_data["status"] == "cancelled"

    def test_returns_404_for_nonexistent_run(self, client):
        res = client.delete("/api/v1/runs/nosuchrun00")
        assert res.status_code == 404

    def test_returns_409_for_already_terminal_run(self, client, tmp_path):
        """Cancelling a run that finished (success) returns 409 Conflict."""
        from orchestration_engine.db import Database

        # Launch and manually update status to success
        run = self._launch(client, tmp_path, suffix="terminal")
        db = Database(tmp_path / "test-engine.db")
        db.update_pipeline_run(run["run_id"], status="success")

        res = client.delete(f"/api/v1/runs/{run['run_id']}")
        assert res.status_code == 409


# ---------------------------------------------------------------------------
# 9. DB methods: list_pipeline_runs_filtered / count / cancel_pipeline_run
# ---------------------------------------------------------------------------

class TestDbMethods:
    """Direct unit tests for the new DB methods added in Issue #257."""

    def test_list_filtered_by_status(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test.db")
        run_id = str(uuid.uuid4())[:8]
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": "/tmp/fake.yaml",
                "template_id": "test-template",
                "input_json": "{}",
                "mode": "dry-run",
                "output_dir": str(tmp_path),
                "status": "running",
            }
        )
        results = db.list_pipeline_runs_filtered(status="running")
        assert any(r["run_id"] == run_id for r in results)

        results_pending = db.list_pipeline_runs_filtered(status="pending")
        assert not any(r["run_id"] == run_id for r in results_pending)

    def test_list_filtered_by_template_id(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test2.db")
        run_id = str(uuid.uuid4())[:8]
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": "/tmp/fake.yaml",
                "template_id": "specific-template",
                "input_json": "{}",
                "mode": "dry-run",
                "output_dir": str(tmp_path),
            }
        )
        results = db.list_pipeline_runs_filtered(template_id="specific-template")
        assert any(r["run_id"] == run_id for r in results)

        results_other = db.list_pipeline_runs_filtered(template_id="other-template")
        assert not any(r["run_id"] == run_id for r in results_other)

    def test_list_filtered_pagination(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test3.db")
        for i in range(5):
            db.insert_pipeline_run(
                {
                    "run_id": str(uuid.uuid4())[:8],
                    "template_path": "/tmp/fake.yaml",
                    "template_id": "page-template",
                    "input_json": "{}",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / f"out{i}"),
                }
            )
        page1 = db.list_pipeline_runs_filtered(template_id="page-template", limit=2, offset=0)
        page2 = db.list_pipeline_runs_filtered(template_id="page-template", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # Pages must be disjoint
        ids1 = {r["run_id"] for r in page1}
        ids2 = {r["run_id"] for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_count_pipeline_runs(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test4.db")
        assert db.count_pipeline_runs() == 0
        for i in range(3):
            db.insert_pipeline_run(
                {
                    "run_id": str(uuid.uuid4())[:8],
                    "template_path": "/tmp/fake.yaml",
                    "template_id": "count-template",
                    "input_json": "{}",
                    "mode": "dry-run",
                    "output_dir": str(tmp_path / f"c{i}"),
                }
            )
        assert db.count_pipeline_runs() == 3
        assert db.count_pipeline_runs(template_id="count-template") == 3
        assert db.count_pipeline_runs(template_id="other") == 0

    def test_cancel_pipeline_run(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test5.db")
        run_id = str(uuid.uuid4())[:8]
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": "/tmp/fake.yaml",
                "template_id": "cancel-template",
                "input_json": "{}",
                "mode": "dry-run",
                "output_dir": str(tmp_path),
            }
        )
        result = db.cancel_pipeline_run(run_id)
        assert result is True

        run = db.get_pipeline_run(run_id)
        assert run["status"] == "cancelled"

    def test_cancel_terminal_run_returns_false(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test6.db")
        run_id = str(uuid.uuid4())[:8]
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": "/tmp/fake.yaml",
                "template_id": "done-template",
                "input_json": "{}",
                "mode": "dry-run",
                "output_dir": str(tmp_path),
                "status": "success",
            }
        )
        result = db.cancel_pipeline_run(run_id)
        assert result is False

        # Status must remain success
        run = db.get_pipeline_run(run_id)
        assert run["status"] == "success"

    def test_cancel_nonexistent_run_returns_false(self, tmp_path):
        from orchestration_engine.db import Database

        db = Database(tmp_path / "db-test7.db")
        result = db.cancel_pipeline_run("nonexistent")
        assert result is False
