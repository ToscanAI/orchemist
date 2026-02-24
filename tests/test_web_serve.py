"""Tests for the ``orch serve`` web UI server (Feature #79).

All tests use FastAPI's TestClient — no real server process is started.
Tests are skipped if the optional [web] dependencies are not installed.
"""

import json
import time
import pytest

# Skip the entire module when FastAPI is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from orchestration_engine.web.app import create_app  # noqa: E402  (after importorskip)
from orchestration_engine import __version__          # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the FastAPI app (module-scoped for speed)."""
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200

    def test_returns_version(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"
        assert data["version"] == __version__


# ---------------------------------------------------------------------------
# Template listing
# ---------------------------------------------------------------------------

class TestTemplateList:
    def test_returns_200(self, client):
        res = client.get("/api/templates")
        assert res.status_code == 200

    def test_returns_list(self, client):
        data = client.get("/api/templates").json()
        assert isinstance(data, list)
        assert len(data) >= 1, "Should find at least one bundled template"

    def test_includes_content_pipeline(self, client):
        data = client.get("/api/templates").json()
        ids = [t["id"] for t in data]
        assert "content-pipeline-v23" in ids

    def test_template_has_required_keys(self, client):
        data = client.get("/api/templates").json()
        first = data[0]
        for key in ("id", "name", "version", "phases_count", "source"):
            assert key in first, f"Missing key '{key}' in template listing"


# ---------------------------------------------------------------------------
# Template detail
# ---------------------------------------------------------------------------

class TestTemplateDetail:
    def test_returns_200_for_existing(self, client):
        res = client.get("/api/templates/content-pipeline-v23")
        assert res.status_code == 200

    def test_content_pipeline_has_phases(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        assert "phases" in data
        assert len(data["phases"]) > 0

    def test_phases_have_required_keys(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        phase = data["phases"][0]
        for key in ("id", "name", "model_tier"):
            assert key in phase, f"Phase missing key '{key}'"

    def test_returns_404_for_nonexistent(self, client):
        res = client.get("/api/templates/nonexistent-template-xyz")
        assert res.status_code == 404

    def test_detail_has_top_level_fields(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        for key in ("id", "name", "version", "description", "phases"):
            assert key in data, f"Missing key '{key}' in template detail"


# ---------------------------------------------------------------------------
# Run endpoint
# ---------------------------------------------------------------------------

class TestRunEndpoint:
    def test_dry_run_returns_run_id(self, client):
        res = client.post(
            "/api/run",
            json={"template": "content-pipeline-v23", "mode": "dry-run", "input": {}},
        )
        assert res.status_code == 200
        data = res.json()
        assert "run_id" in data
        assert len(data["run_id"]) > 0

    def test_nonexistent_template_returns_404(self, client):
        res = client.post(
            "/api/run",
            json={"template": "does-not-exist-xyz", "mode": "dry-run", "input": {}},
        )
        assert res.status_code == 404

    def test_run_id_is_unique(self, client):
        ids = set()
        for _ in range(3):
            res = client.post(
                "/api/run",
                json={"template": "content-pipeline-v23", "mode": "dry-run", "input": {}},
            )
            assert res.status_code == 200
            ids.add(res.json()["run_id"])
        assert len(ids) == 3, "Each run should get a unique run_id"


# ---------------------------------------------------------------------------
# SPA root
# ---------------------------------------------------------------------------

class TestSPA:
    def test_root_returns_200(self, client):
        res = client.get("/")
        assert res.status_code == 200

    def test_root_returns_html(self, client):
        res = client.get("/")
        assert "text/html" in res.headers.get("content-type", "")

    def test_html_contains_title(self, client):
        body = client.get("/").text
        assert "Orchestration Engine" in body

    def test_html_contains_htmx_or_fetch(self, client):
        body = client.get("/").text
        # Either htmx CDN or native fetch-based JS
        assert "htmx" in body or "fetch(" in body


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------

class TestCORS:
    def test_cors_header_present_on_api(self, client):
        res = client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3000"},
        )
        # FastAPI CORS middleware should echo back the allow-origin header
        assert (
            res.headers.get("access-control-allow-origin") == "*"
            or res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        )


# ---------------------------------------------------------------------------
# CLI command registration
# ---------------------------------------------------------------------------

class TestCLIServeCommand:
    def test_serve_command_exists(self):
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output.lower() or "web" in result.output.lower()

    def test_serve_help_mentions_port(self):
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert "port" in result.output.lower()

    def test_serve_help_mentions_host(self):
        from click.testing import CliRunner
        from orchestration_engine.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert "host" in result.output.lower()


# ---------------------------------------------------------------------------
# SSE streaming (Fix 6)
# ---------------------------------------------------------------------------

class TestSSEStreaming:
    """Verify that the /api/run/{id}/status SSE endpoint delivers events."""

    def test_sse_delivers_phase_and_complete_events(self, client):
        """POST a dry-run, then stream SSE and assert we get at least one
        phase event followed by a 'complete' event."""
        # 1. Start a run
        res = client.post(
            "/api/run",
            json={"template": "content-pipeline-v23", "mode": "dry-run", "input": {}},
        )
        assert res.status_code == 200
        run_id = res.json()["run_id"]

        # 2. Stream SSE — TestClient supports iter_lines() on a streaming response.
        received_events = []
        deadline = time.time() + 30  # generous timeout for CI

        with client.stream("GET", f"/api/run/{run_id}/status") as stream:
            for raw_line in stream.iter_lines():
                if time.time() > deadline:
                    break
                # SSE lines look like: "data: {json}" or empty keep-alive lines.
                if raw_line.startswith("data:"):
                    payload_str = raw_line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        event = json.loads(payload_str)
                        received_events.append(event)
                    except json.JSONDecodeError:
                        continue
                    # Stop after we see the terminal event.
                    if event.get("type") in ("complete", "aborted", "error"):
                        break

        event_types = [e.get("type") for e in received_events]

        # Must have at least one phase-level event (phase_complete or start).
        assert any(
            t in event_types for t in ("phase_complete", "phase_failed", "start")
        ), f"Expected at least one phase event, got: {event_types}"

        # Must end with 'complete' (dry-run never aborts or errors).
        assert "complete" in event_types, (
            f"Expected a 'complete' event in SSE stream, got: {event_types}"
        )

    def test_sse_unknown_run_returns_404(self, client):
        res = client.get("/api/run/nonexistent-run-id/status")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Invalid mode validation (Fix 7)
# ---------------------------------------------------------------------------

class TestRunRequestValidation:
    """Verify that unknown mode values are rejected with 422."""

    def test_invalid_mode_returns_422(self, client):
        res = client.post(
            "/api/run",
            json={"template": "content-pipeline-v23", "mode": "bogus", "input": {}},
        )
        assert res.status_code == 422, (
            f"Expected 422 Unprocessable Entity for invalid mode, got {res.status_code}"
        )

    def test_valid_modes_are_accepted(self, client):
        for mode in ("dry-run", "standalone", "openclaw"):
            res = client.post(
                "/api/run",
                json={"template": "content-pipeline-v23", "mode": mode, "input": {}},
            )
            # 200 OK or 404 (if template doesn't exist in CI) — but NOT 422.
            assert res.status_code != 422, (
                f"Mode '{mode}' should be valid but got 422"
            )
