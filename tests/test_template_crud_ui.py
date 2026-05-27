"""Tests for Template CRUD endpoints added for the Web UI (Issue #770).

Covers:
    POST   /api/v1/templates/{name}/duplicate  — clone a template
    GET    /api/v1/templates/{name}            — yaml_content + source fields
    POST   /api/v1/templates                   — create with source=user
    PUT    /api/v1/templates/{name}            — update user template
    DELETE /api/v1/templates/{name}            — delete user template

Uses FastAPI's TestClient with an isolated temp user-templates directory so
that no real files are written to ``~/.orch/templates/``.  Bundled templates
are not modified.

All tests are skipped when the optional [web] dependencies are absent.
"""

from pathlib import Path
from typing import Generator, Tuple

import pytest

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Minimal valid template YAML fixture strings
# ---------------------------------------------------------------------------

MINIMAL_TEMPLATE = """\
id: test-ui-template
name: "Test UI Template"
version: "1.0.0"
description: "A minimal template used for UI CRUD tests."
author: "Test Suite"
phases:
  - id: phase-one
    name: "Phase One"
    description: "Simple test phase."
    model_tier: haiku
    task_type: generate
    prompt: |
      Process the input: {input}
"""

UPDATED_TEMPLATE = """\
id: test-ui-template
name: "Test UI Template (Updated)"
version: "1.1.0"
description: "Updated UI template."
author: "Test Suite"
phases:
  - id: phase-one
    name: "Phase One Updated"
    description: "Updated test phase."
    model_tier: haiku
    task_type: generate
    prompt: |
      Process updated input: {input}
"""


# ---------------------------------------------------------------------------
# Shared fixture: isolated TestClient + user-templates directory
# ---------------------------------------------------------------------------

@pytest.fixture()
def crud_client(
    tmp_path: Path, monkeypatch
) -> Generator[Tuple[TestClient, Path], None, None]:
    """Yield ``(TestClient, user_templates_dir)`` with full isolation.

    Patches :class:`TemplateEngine.__init__` so that every instance created
    during the request handling uses ``tmp_path/user-templates`` as the user
    directory instead of ``~/.orch/templates/``.
    """
    from orchestration_engine.templates import TemplateEngine
    from orchestration_engine.web.api import create_api_app

    user_dir = tmp_path / "user-templates"
    user_dir.mkdir()

    _original_init = TemplateEngine.__init__

    def _patched_init(
        self,
        templates_dir=None,
        project_dir=None,
        user_dir_arg=None,
        **kwargs,
    ):
        _original_init(
            self,
            templates_dir=templates_dir,
            project_dir=project_dir,
            user_dir=user_dir,
        )

    monkeypatch.setattr(TemplateEngine, "__init__", _patched_init)

    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, user_dir


# ---------------------------------------------------------------------------
# GET /api/v1/templates/{name} — yaml_content + source fields
# ---------------------------------------------------------------------------

class TestTemplateDetailFields:
    """Tests for the yaml_content and source fields on template detail."""

    def test_detail_includes_source_field(self, crud_client):
        client, _user_dir = crud_client
        # Use a known project/bundled template
        res = client.get("/api/v1/templates")
        templates = res.json()
        if not templates:
            pytest.skip("No templates available to test")
        first_id = templates[0]["id"]
        detail = client.get(f"/api/v1/templates/{first_id}")
        assert detail.status_code == 200
        data = detail.json()
        assert "source" in data
        assert isinstance(data["source"], str)
        assert data["source"] in ("project", "user", "bundled", "custom", "unknown")

    def test_detail_includes_yaml_content_field(self, crud_client):
        client, _user_dir = crud_client
        res = client.get("/api/v1/templates")
        templates = res.json()
        if not templates:
            pytest.skip("No templates available to test")
        first_id = templates[0]["id"]
        detail = client.get(f"/api/v1/templates/{first_id}")
        data = detail.json()
        assert "yaml_content" in data
        assert isinstance(data["yaml_content"], str)
        assert len(data["yaml_content"]) > 0

    def test_detail_yaml_content_is_valid_yaml(self, crud_client):
        import yaml as pyyaml
        client, _user_dir = crud_client
        res = client.get("/api/v1/templates")
        templates = res.json()
        if not templates:
            pytest.skip("No templates available to test")
        first_id = templates[0]["id"]
        detail = client.get(f"/api/v1/templates/{first_id}")
        data = detail.json()
        parsed = pyyaml.safe_load(data["yaml_content"])
        assert isinstance(parsed, dict)
        assert parsed.get("id") == first_id

    def test_user_template_source_is_user(self, crud_client):
        """Templates created via POST should report source='user'."""
        client, _user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        detail = client.get("/api/v1/templates/test-ui-template")
        assert detail.status_code == 200
        data = detail.json()
        assert data["source"] == "user"


# ---------------------------------------------------------------------------
# POST /api/v1/templates/{name}/duplicate
# ---------------------------------------------------------------------------

class TestDuplicateTemplate:
    """Tests for the duplicate endpoint."""

    def _create_user_template(self, client) -> None:
        """Create a user template for duplication tests."""
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )

    def test_duplicate_returns_201(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        res = client.post("/api/v1/templates/test-ui-template/duplicate")
        assert res.status_code == 201

    def test_duplicate_returns_new_id_with_copy_suffix(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert data["id"] == "test-ui-template-copy"

    def test_duplicate_returns_name_with_copy(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert "(Copy)" in data["name"]

    def test_duplicate_includes_yaml_content(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert "yaml_content" in data
        assert len(data["yaml_content"]) > 0

    def test_duplicate_includes_source(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert "source" in data

    def test_duplicate_includes_phases(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert "phases" in data
        assert len(data["phases"]) >= 1

    def test_duplicate_writes_file_to_disk(self, crud_client):
        client, user_dir = crud_client
        self._create_user_template(client)
        client.post("/api/v1/templates/test-ui-template/duplicate")
        written = list(user_dir.glob("test-ui-template-copy.yaml"))
        assert len(written) == 1

    def test_duplicate_second_copy_gets_numbered_suffix(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        # First duplicate
        client.post("/api/v1/templates/test-ui-template/duplicate")
        # Second duplicate
        data = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        assert data["id"] == "test-ui-template-copy-2"

    def test_duplicate_nonexistent_returns_404(self, crud_client):
        client, _user_dir = crud_client
        res = client.post("/api/v1/templates/no-such-template/duplicate")
        assert res.status_code == 404

    def test_duplicate_can_be_fetched_by_new_id(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)
        dup = client.post(
            "/api/v1/templates/test-ui-template/duplicate"
        ).json()
        detail = client.get(f"/api/v1/templates/{dup['id']}")
        assert detail.status_code == 200
        assert detail.json()["id"] == "test-ui-template-copy"

    def test_duplicate_can_be_deleted(self, crud_client):
        """Duplicated templates (user-owned) should be deletable."""
        client, _user_dir = crud_client
        self._create_user_template(client)
        client.post("/api/v1/templates/test-ui-template/duplicate")
        res = client.delete("/api/v1/templates/test-ui-template-copy")
        assert res.status_code == 204

    def test_duplicate_bundled_template_returns_201(self, crud_client):
        """Duplicating a project/bundled template should succeed (read → write copy)."""
        client, _user_dir = crud_client
        # Find a project/bundled template
        templates = client.get("/api/v1/templates").json()
        if not templates:
            pytest.skip("No templates available to duplicate")
        bundled_id = templates[0]["id"]
        res = client.post(f"/api/v1/templates/{bundled_id}/duplicate")
        assert res.status_code == 201
        data = res.json()
        assert data["id"] == f"{bundled_id}-copy"


# ---------------------------------------------------------------------------
# Full CRUD round-trip
# ---------------------------------------------------------------------------

class TestCrudRoundTrip:
    """End-to-end round-trip: create → read → update → duplicate → delete."""

    def test_full_crud_lifecycle(self, crud_client):
        client, user_dir = crud_client

        # 1. Create
        create_res = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        assert create_res.status_code == 201
        assert create_res.json()["id"] == "test-ui-template"

        # 2. Read — verify yaml_content and source
        detail = client.get("/api/v1/templates/test-ui-template").json()
        assert detail["source"] == "user"
        assert "yaml_content" in detail
        assert "test-ui-template" in detail["yaml_content"]

        # 3. Update
        update_res = client.put(
            "/api/v1/templates/test-ui-template",
            json={"content": UPDATED_TEMPLATE},
        )
        assert update_res.status_code == 200
        assert update_res.json()["version"] == "1.1.0"

        # 4. Duplicate
        dup_res = client.post("/api/v1/templates/test-ui-template/duplicate")
        assert dup_res.status_code == 201
        assert dup_res.json()["id"] == "test-ui-template-copy"

        # 5. Delete original
        del_res = client.delete("/api/v1/templates/test-ui-template")
        assert del_res.status_code == 204

        # 6. Delete duplicate
        del_dup = client.delete("/api/v1/templates/test-ui-template-copy")
        assert del_dup.status_code == 204

        # 7. Verify both gone
        assert client.get("/api/v1/templates/test-ui-template").status_code == 404
        assert client.get("/api/v1/templates/test-ui-template-copy").status_code == 404


# ---------------------------------------------------------------------------
# Duplicate-of-duplicate naming (#779)
# ---------------------------------------------------------------------------

class TestDuplicateOfDuplicateNaming:
    """Duplicating a copy should increment the (Copy N) suffix, not stack."""

    def _create_user_template(self, client) -> None:
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )

    def test_duplicate_of_duplicate_increments_copy_number(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)

        # First duplicate: name should be "Test UI Template (Copy)"
        dup1 = client.post("/api/v1/templates/test-ui-template/duplicate").json()
        assert dup1["name"] == "Test UI Template (Copy)"

        # Duplicate the duplicate: name should be "Test UI Template (Copy 2)", not "Test UI Template (Copy) (Copy)"
        dup2 = client.post(f"/api/v1/templates/{dup1['id']}/duplicate").json()
        assert dup2["name"] == "Test UI Template (Copy 2)"
        assert "(Copy) (Copy)" not in dup2["name"]


# ---------------------------------------------------------------------------
# Update endpoint ID consistency (#781)
# ---------------------------------------------------------------------------

class TestUpdateIdConsistency:
    """PUT /api/v1/templates/{name} must reject YAML whose id mismatches the URL."""

    def _create_user_template(self, client) -> None:
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )

    def test_update_with_mismatched_id_returns_400(self, crud_client):
        client, _user_dir = crud_client
        self._create_user_template(client)

        mismatched_yaml = UPDATED_TEMPLATE.replace(
            "id: test-ui-template", "id: wrong-id"
        )
        res = client.put(
            "/api/v1/templates/test-ui-template",
            json={"content": mismatched_yaml},
        )
        assert res.status_code == 400
        assert "does not match" in res.json()["detail"]

    def test_update_with_missing_id_returns_400(self, crud_client):
        import yaml as pyyaml
        client, _user_dir = crud_client
        self._create_user_template(client)

        parsed = pyyaml.safe_load(UPDATED_TEMPLATE)
        del parsed["id"]
        no_id_yaml = pyyaml.dump(parsed)
        res = client.put(
            "/api/v1/templates/test-ui-template",
            json={"content": no_id_yaml},
        )
        assert res.status_code == 400
        assert "id" in res.json()["detail"].lower()


class TestPathTraversalRejection:
    """Issue #777: _resolve_template must reject paths outside template dirs."""

    def test_path_traversal_etc_passwd(self, crud_client):
        client, _user_dir = crud_client
        resp = client.get("/api/v1/templates//etc/passwd.yaml")
        assert resp.status_code in (403, 404)

    def test_path_traversal_dotdot(self, crud_client):
        client, _user_dir = crud_client
        resp = client.get("/api/v1/templates/../../etc/passwd.yaml")
        assert resp.status_code in (403, 404)
