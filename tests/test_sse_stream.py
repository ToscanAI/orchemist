"""Tests for the SSE live-progress streaming endpoint (Issue #258).

Tests cover:
- GET /api/v1/runs/{run_id}/stream returns 200 with text/event-stream
- 404 (SSE error event) for unknown run_id
- Events are emitted in order for seeded pipeline_run_events rows
- Terminal status_changed event is emitted and stream ends
- Backward compatibility: existing polling endpoint unchanged

Uses FastAPI's TestClient with streaming support (stream=True).
No real daemon subprocess is started.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient
sse_starlette = pytest.importorskip("sse_starlette")

from orchestration_engine.db import Database  # noqa: E402
from orchestration_engine.web.api import create_api_app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> TestClient:
    """Return a TestClient backed by an isolated on-disk DB."""
    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _seed_run(db: Database, run_id: str, status: str = "running") -> None:
    """Insert a minimal pipeline_runs row for testing."""
    import tempfile
    from tests._helpers import pipeline_run_dict
    out_dir = tempfile.mkdtemp()
    db.insert_pipeline_run(pipeline_run_dict(
        run_id,
        template_path="/fake/template.yaml",
        template_id="fake-template",
        output_dir=out_dir,
        status=status,
    ))


def _parse_sse_events(raw_text: str) -> list[dict]:
    """Parse raw SSE text into a list of event dicts.

    Each dict has ``event`` and ``data`` keys (and optionally ``id``).
    """
    events = []
    current: dict = {}
    for line in raw_text.splitlines():
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
        elif line.startswith("id:"):
            current["id"] = line[len("id:"):].strip()
        elif line == "" and current:
            events.append(current)
            current = {}
    if current:
        events.append(current)
    return events


# ---------------------------------------------------------------------------
# 1. DB — insert_pipeline_run_event / list_pipeline_run_events
# ---------------------------------------------------------------------------

class TestDatabaseEvents:
    def test_insert_returns_autoincrement_id(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "run1")
        id1 = db.insert_pipeline_run_event("run1", "phase_started", phase_id="research")
        id2 = db.insert_pipeline_run_event("run1", "phase_completed", phase_id="research",
                                            tokens_consumed=100, cost_usd=0.01, state="success")
        assert isinstance(id1, int)
        assert id2 > id1

    def test_list_returns_all_events(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "run2")
        db.insert_pipeline_run_event("run2", "phase_started", phase_id="spec")
        db.insert_pipeline_run_event("run2", "phase_completed", phase_id="spec",
                                     tokens_consumed=50, cost_usd=0.005, state="success")
        events = db.list_pipeline_run_events("run2")
        assert len(events) == 2
        assert events[0]["event_type"] == "phase_started"
        assert events[1]["event_type"] == "phase_completed"

    def test_list_after_id_filters_correctly(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "run3")
        id1 = db.insert_pipeline_run_event("run3", "phase_started", phase_id="a")
        _     = db.insert_pipeline_run_event("run3", "phase_completed", phase_id="a")
        events = db.list_pipeline_run_events("run3", after_id=id1)
        assert len(events) == 1
        assert events[0]["event_type"] == "phase_completed"

    def test_list_returns_empty_for_unknown_run(self, tmp_path):
        db = Database(tmp_path / "db.db")
        events = db.list_pipeline_run_events("no-such-run")
        assert events == []

    def test_event_fields_populated(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "run4")
        db.insert_pipeline_run_event(
            "run4", "phase_completed", phase_id="write",
            tokens_consumed=200, cost_usd=0.02, state="success",
            metadata={"word_count": 150},
        )
        events = db.list_pipeline_run_events("run4")
        e = events[0]
        assert e["run_id"] == "run4"
        assert e["phase_id"] == "write"
        assert e["tokens_consumed"] == 200
        assert e["cost_usd"] == pytest.approx(0.02)
        assert e["state"] == "success"

    def test_list_respects_limit(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "run5")
        for i in range(10):
            db.insert_pipeline_run_event("run5", "phase_started", phase_id=f"p{i}")
        events = db.list_pipeline_run_events("run5", limit=3)
        assert len(events) == 3

    def test_events_isolated_by_run_id(self, tmp_path):
        db = Database(tmp_path / "db.db")
        _seed_run(db, "runA")
        _seed_run(db, "runB")
        db.insert_pipeline_run_event("runA", "phase_started", phase_id="x")
        db.insert_pipeline_run_event("runB", "phase_started", phase_id="y")
        assert len(db.list_pipeline_run_events("runA")) == 1
        assert len(db.list_pipeline_run_events("runB")) == 1


# ---------------------------------------------------------------------------
# 2. SSE endpoint — /api/v1/runs/{run_id}/stream
# ---------------------------------------------------------------------------

def _get_sse_text(client: TestClient, url: str) -> str:
    """Fetch the full SSE response text using httpx's stream() context manager.

    For terminal runs the generator exits after emitting all buffered events,
    so the stream completes quickly.  Uses ``read()`` to consume the full body
    before accessing ``res.text`` (required by httpx's streaming API).
    """
    with client.stream("GET", url) as res:
        res.read()
        return res.text


class TestSSEStream:
    def test_unknown_run_returns_error_event(self, tmp_path):
        with _make_client(tmp_path) as client:
            text = _get_sse_text(client, "/api/v1/runs/nonexistent/stream")
        events = _parse_sse_events(text)
        assert any(e.get("event") == "error" for e in events), (
            f"Expected an 'error' SSE event, got: {events}"
        )
        error_event = next(e for e in events if e.get("event") == "error")
        data = json.loads(error_event["data"])
        assert "not found" in data.get("error", "").lower()

    def test_stream_content_type(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "run-ct", status="success")

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.stream("GET", "/api/v1/runs/run-ct/stream") as res:
                content_type = res.headers.get("content-type", "")
                res.read()  # consume to allow clean close
        assert "text/event-stream" in content_type

    def test_stream_emits_phase_events_in_order(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "run-order", status="success")
        db.insert_pipeline_run_event("run-order", "phase_started", phase_id="spec")
        db.insert_pipeline_run_event("run-order", "phase_completed", phase_id="spec",
                                     tokens_consumed=10, cost_usd=0.001, state="success")

        with TestClient(app, raise_server_exceptions=False) as client:
            text = _get_sse_text(client, "/api/v1/runs/run-order/stream")

        events = _parse_sse_events(text)
        event_types = [e.get("event") for e in events]
        assert "phase_started" in event_types
        assert "phase_completed" in event_types
        # Order preserved
        started_idx = event_types.index("phase_started")
        completed_idx = event_types.index("phase_completed")
        assert started_idx < completed_idx

    def test_stream_emits_status_changed_for_terminal_run(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "run-terminal", status="success")

        with TestClient(app, raise_server_exceptions=False) as client:
            text = _get_sse_text(client, "/api/v1/runs/run-terminal/stream")

        events = _parse_sse_events(text)
        event_types = [e.get("event") for e in events]
        assert "status_changed" in event_types, (
            f"Expected 'status_changed' event for terminal run, got: {event_types}"
        )
        status_evt = next(e for e in events if e.get("event") == "status_changed")
        data = json.loads(status_evt["data"])
        assert data["status"] == "success"

    def test_stream_phase_completed_carries_metrics(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "run-metrics", status="success")
        db.insert_pipeline_run_event(
            "run-metrics", "phase_completed", phase_id="write",
            tokens_consumed=500, cost_usd=0.05, state="success",
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            text = _get_sse_text(client, "/api/v1/runs/run-metrics/stream")

        events = _parse_sse_events(text)
        completed_evt = next(
            (e for e in events if e.get("event") == "phase_completed"), None
        )
        assert completed_evt is not None, "Expected a phase_completed event"
        data = json.loads(completed_evt["data"])
        assert data["tokens_consumed"] == 500
        assert data["cost_usd"] == pytest.approx(0.05)
        assert data["state"] == "success"
        assert data["phase_id"] == "write"

    def test_stream_event_has_id_field(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "run-id-field", status="success")
        db.insert_pipeline_run_event("run-id-field", "phase_started", phase_id="p1")

        with TestClient(app, raise_server_exceptions=False) as client:
            text = _get_sse_text(client, "/api/v1/runs/run-id-field/stream")

        events = _parse_sse_events(text)
        phase_events = [e for e in events if e.get("event") == "phase_started"]
        assert phase_events, "Expected at least one phase_started event"
        assert "id" in phase_events[0], "SSE event should include an 'id' field"


# ---------------------------------------------------------------------------
# 3. Backward compatibility — polling endpoint unchanged
# ---------------------------------------------------------------------------

class TestPollingEndpointUnchanged:
    def test_get_run_still_works(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "compat-run", status="success")

        with TestClient(app, raise_server_exceptions=False) as client:
            res = client.get("/api/v1/runs/compat-run")
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"] == "compat-run"
        assert data["status"] == "success"

    def test_get_run_returns_json(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        app = create_api_app(db_path=db_file)
        db = Database(Path(db_file))
        _seed_run(db, "compat-run2", status="running")

        with TestClient(app, raise_server_exceptions=False) as client:
            res = client.get("/api/v1/runs/compat-run2")
        assert "application/json" in res.headers.get("content-type", "")

    def test_get_run_404_for_unknown(self, tmp_path):
        with _make_client(tmp_path) as client:
            res = client.get("/api/v1/runs/no-such-run")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# 4. Daemon _write_phase_event helper
# ---------------------------------------------------------------------------

class TestDaemonWritePhaseEvent:
    def test_writes_phase_started_event(self, tmp_path):
        from orchestration_engine.daemon import _write_phase_event

        db = Database(tmp_path / "db.db")
        _seed_run(db, "daemon-run")
        _write_phase_event(db, "daemon-run", "spec", "phase_started")

        events = db.list_pipeline_run_events("daemon-run")
        assert len(events) == 1
        assert events[0]["event_type"] == "phase_started"
        assert events[0]["phase_id"] == "spec"

    def test_writes_phase_completed_event_with_metrics(self, tmp_path):
        from orchestration_engine.daemon import _write_phase_event

        db = Database(tmp_path / "db.db")
        _seed_run(db, "daemon-run2")
        phase_result = {
            "state": "success",
            "tokens_consumed": 300,
            "cost_usd": 0.03,
            "result": {"output": "hello world"},
        }
        _write_phase_event(
            db, "daemon-run2", "write", "phase_completed",
            phase_result=phase_result,
            tokens_consumed=300,
            cost_usd=0.03,
            state="success",
        )
        events = db.list_pipeline_run_events("daemon-run2")
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "phase_completed"
        assert e["tokens_consumed"] == 300
        assert e["cost_usd"] == pytest.approx(0.03)
        assert e["state"] == "success"

    def test_swallows_db_error_silently(self, tmp_path):
        """_write_phase_event must not raise even when the DB call fails."""
        from orchestration_engine.daemon import _write_phase_event

        db = Database(tmp_path / "db.db")
        # Don't seed the run — FK constraint will fail on real DB, but
        # sqlite has FK enforcement only when turned on; in any case the
        # function must not propagate exceptions.
        try:
            _write_phase_event(db, "ghost-run", "p1", "phase_started")
        except Exception as exc:
            pytest.fail(f"_write_phase_event raised unexpectedly: {exc}")
