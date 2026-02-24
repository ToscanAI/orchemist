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


# ---------------------------------------------------------------------------
# Template Selector UI — Feature #80
# ---------------------------------------------------------------------------

class TestTemplateListEnhanced:
    """Verify that GET /api/templates returns enriched fields for the card UI."""

    def test_template_has_category_field(self, client):
        data = client.get("/api/templates").json()
        first = data[0]
        assert "category" in first, "Template listing should include 'category' field"

    def test_template_has_author_field(self, client):
        data = client.get("/api/templates").json()
        first = data[0]
        assert "author" in first, "Template listing should include 'author' field"

    def test_template_has_phases_summary(self, client):
        data = client.get("/api/templates").json()
        first = data[0]
        assert "phases" in first, "Template listing should include 'phases' summary list"
        assert isinstance(first["phases"], list)

    def test_phases_summary_keys(self, client):
        data = client.get("/api/templates").json()
        # Find a template with at least one phase summary
        for t in data:
            if t.get("phases"):
                phase = t["phases"][0]
                for key in ("id", "name", "model_tier"):
                    assert key in phase, f"Phase summary missing key '{key}'"
                break

    def test_content_pipeline_has_known_category(self, client):
        data = client.get("/api/templates").json()
        cp = next((t for t in data if t["id"] == "content-pipeline-v23"), None)
        assert cp is not None
        assert cp["category"], "content-pipeline-v23 should have a non-empty category"

    def test_content_pipeline_has_author(self, client):
        data = client.get("/api/templates").json()
        cp = next((t for t in data if t["id"] == "content-pipeline-v23"), None)
        assert cp is not None
        assert cp["author"], "content-pipeline-v23 should have a non-empty author"

    def test_category_is_string(self, client):
        data = client.get("/api/templates").json()
        for t in data:
            assert isinstance(t["category"], str), (
                f"Template '{t['id']}' category should be a string"
            )

    def test_author_is_string(self, client):
        data = client.get("/api/templates").json()
        for t in data:
            assert isinstance(t["author"], str), (
                f"Template '{t['id']}' author should be a string"
            )


class TestTemplateSelectorHTML:
    """Verify the SPA HTML contains the template selector UI elements."""

    def test_html_contains_search_input(self, client):
        body = client.get("/").text
        assert 'id="template-search"' in body, (
            "HTML should contain a search input with id='template-search'"
        )

    def test_html_contains_category_tabs(self, client):
        body = client.get("/").text
        assert 'category-tabs' in body, (
            "HTML should contain category filter tabs container"
        )

    def test_html_contains_template_card_class(self, client):
        body = client.get("/").text
        assert 'template-card' in body, (
            "HTML should reference 'template-card' CSS class for card layout"
        )

    def test_html_contains_template_grid(self, client):
        body = client.get("/").text
        assert 'template-grid' in body, (
            "HTML should contain a template grid container"
        )

    def test_html_contains_empty_state_orch_new(self, client):
        body = client.get("/").text
        assert 'orch new' in body, (
            "HTML should mention 'orch new' in the empty state message"
        )

    def test_html_has_responsive_meta_tag(self, client):
        body = client.get("/").text
        assert 'name="viewport"' in body, (
            "HTML should include a responsive viewport meta tag"
        )

    def test_html_contains_filter_bar(self, client):
        body = client.get("/").text
        assert 'filter-bar' in body, (
            "HTML should contain a filter bar element"
        )

    def test_html_contains_cat_tab_class(self, client):
        body = client.get("/").text
        assert 'cat-tab' in body, (
            "HTML should define category tab CSS class"
        )

    def test_html_contains_back_navigation(self, client):
        body = client.get("/").text
        assert 'showBrowser' in body, (
            "HTML should contain showBrowser() function for back navigation"
        )


class TestTemplateDetailEnhanced:
    """Verify that GET /api/templates/{name} returns phases for the card detail view."""

    def test_detail_has_phases(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        assert "phases" in data
        assert len(data["phases"]) > 0

    def test_detail_phases_have_full_keys(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        phase = data["phases"][0]
        for key in ("id", "name", "model_tier", "thinking_level", "depends_on", "task_type"):
            assert key in phase, f"Detail phase missing key '{key}'"

    def test_detail_has_author(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        assert "author" in data, "Template detail should include 'author'"
        assert data["author"], "content-pipeline-v23 author should be non-empty"


# ---------------------------------------------------------------------------
# Auto-generated Input Forms — Feature #81
# ---------------------------------------------------------------------------

class TestInputFormsHTML:
    """Verify the SPA HTML contains the auto-generated form infrastructure."""

    def test_html_contains_render_form_function(self, client):
        body = client.get("/").text
        assert "renderForm" in body, (
            "HTML should contain the renderForm() JavaScript function"
        )

    def test_html_contains_form_group_class(self, client):
        body = client.get("/").text
        assert "form-group" in body, (
            "HTML should define .form-group CSS class for field layout"
        )

    def test_html_contains_schema_form_id(self, client):
        body = client.get("/").text
        assert "schema-form" in body, (
            "HTML should reference 'schema-form' container for the generated form"
        )

    def test_html_contains_mode_selector(self, client):
        body = client.get("/").text
        # Mode selector is rendered dynamically via JS; check that the option
        # values are present as string literals in the JS source.
        assert "dry-run" in body, "HTML should include dry-run mode option"
        assert "standalone" in body, "HTML should include standalone mode option"
        assert "openclaw" in body, "HTML should include openclaw mode option"

    def test_html_contains_required_star_style(self, client):
        body = client.get("/").text
        assert "required-star" in body, (
            "HTML should style required field asterisks with .required-star"
        )

    def test_html_contains_toggle_switch(self, client):
        body = client.get("/").text
        assert "toggle-switch" in body, (
            "HTML should contain toggle-switch CSS class for boolean fields"
        )

    def test_html_contains_validate_form_function(self, client):
        body = client.get("/").text
        assert "validateForm" in body, (
            "HTML should contain validateForm() function for client-side validation"
        )

    def test_html_contains_collect_form_values_function(self, client):
        body = client.get("/").text
        assert "collectFormValues" in body, (
            "HTML should contain collectFormValues() function to gather form data"
        )

    def test_html_contains_has_error_class(self, client):
        body = client.get("/").text
        assert "has-error" in body, (
            "HTML should define .has-error class for invalid field highlighting"
        )

    def test_html_contains_field_help_class(self, client):
        body = client.get("/").text
        assert "field-help" in body, (
            "HTML should define .field-help class for field description text"
        )

    def test_html_contains_mode_selector_row_class(self, client):
        body = client.get("/").text
        assert "mode-selector-row" in body, (
            "HTML should contain .mode-selector-row for the mode dropdown"
        )

    def test_html_renders_string_fields_as_text_input(self, client):
        body = client.get("/").text
        # renderForm generates input[type="text"] for string fields
        assert 'type="text"' in body or "type=\\'text\\'" in body or "input type" in body, (
            "HTML should reference text input generation for string fields"
        )

    def test_html_renders_number_fields_as_number_input(self, client):
        body = client.get("/").text
        assert 'type="number"' in body or "type=\\'number\\'" in body, (
            "HTML should reference number input generation for number/integer fields"
        )

    def test_html_renders_boolean_fields_as_checkbox(self, client):
        body = client.get("/").text
        assert 'type="checkbox"' in body or "type=\\'checkbox\\'" in body, (
            "HTML should reference checkbox input generation for boolean fields"
        )

    def test_html_renders_enum_fields_as_select(self, client):
        body = client.get("/").text
        # renderForm builds <select> for enum fields
        assert "enumVals" in body or "<select" in body, (
            "HTML should reference select element generation for enum fields"
        )


class TestInputFormsAPI:
    """Verify that the template detail API exposes config_schema."""

    def test_detail_has_config_schema_key(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        assert "config_schema" in data, (
            "Template detail should include 'config_schema' key"
        )

    def test_config_schema_is_dict(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        assert isinstance(data["config_schema"], dict), (
            "'config_schema' should be a dict"
        )

    def test_content_pipeline_config_schema_has_properties(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        schema = data["config_schema"]
        assert "properties" in schema, (
            "content-pipeline-v23 config_schema should have 'properties'"
        )

    def test_content_pipeline_config_schema_has_topic_field(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        props = data["config_schema"].get("properties", {})
        assert "topic" in props, (
            "content-pipeline-v23 config_schema should have a 'topic' property"
        )

    def test_content_pipeline_config_schema_has_required(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        schema = data["config_schema"]
        assert "required" in schema, (
            "content-pipeline-v23 config_schema should have a 'required' list"
        )

    def test_content_pipeline_topic_is_required(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        required = data["config_schema"].get("required", [])
        assert "topic" in required, (
            "content-pipeline-v23 'topic' field should be in required list"
        )

    def test_content_pipeline_schema_field_has_type(self, client):
        data = client.get("/api/templates/content-pipeline-v23").json()
        props = data["config_schema"].get("properties", {})
        for field_name, field_schema in props.items():
            assert "type" in field_schema, (
                f"Field '{field_name}' in config_schema should have a 'type'"
            )

    def test_config_schema_empty_dict_for_missing_schema(self, client):
        """Templates without a config_schema should return empty dict, not null."""
        data = client.get("/api/templates").json()
        # Find any template, check detail — config_schema must always be dict
        for t in data[:3]:
            detail = client.get(f"/api/templates/{t['id']}").json()
            assert "config_schema" in detail
            assert isinstance(detail["config_schema"], dict), (
                f"Template '{t['id']}' config_schema should be a dict (even if empty)"
            )
