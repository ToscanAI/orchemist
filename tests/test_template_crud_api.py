"""Tests for Template CRUD API endpoints (Issue #259).

Covers:
    POST   /api/v1/templates/validate  — dry-run validation
    POST   /api/v1/templates           — create template
    PUT    /api/v1/templates/{name}    — update template
    DELETE /api/v1/templates/{name}    — delete template

Uses FastAPI's TestClient with an isolated temp user-templates directory so
that no real files are written to ``~/.orch/templates/``.  Bundled templates
are not modified.

All tests are skipped when the optional [web] dependencies are absent.
"""

from pathlib import Path
from typing import Generator, Tuple
from unittest.mock import patch

import pytest

# Skip entire module when FastAPI / starlette is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient


# ---------------------------------------------------------------------------
# Minimal valid template YAML fixture strings
# ---------------------------------------------------------------------------

MINIMAL_TEMPLATE = """\
id: test-crud-template
name: "Test CRUD Template"
version: "1.0.0"
description: "A minimal template used for CRUD API tests."
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
id: test-crud-template
name: "Test CRUD Template (Updated)"
version: "1.1.0"
description: "Updated template."
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

INVALID_YAML = "this: is: not: valid: yaml: [unclosed"

INVALID_TEMPLATE = """\
id: bad-template
name: "Bad Template"
version: "1.0.0"
phases:
  - id: phase-bad
    name: "No Prompt Phase"
    model_tier: haiku
    task_type: generate
"""

# Template with a path-traversal id — must be rejected with 422.
PATH_TRAVERSAL_TEMPLATE = """\
id: ../../evil
name: "Evil Template"
version: "1.0.0"
description: "Malicious path traversal test."
author: "Attacker"
phases:
  - id: phase-one
    name: "Evil Phase"
    description: "Should never be written."
    model_tier: haiku
    task_type: generate
    prompt: "Pwned: {input}"
"""

# Template with an id that starts with a dot (also invalid).
DOT_ID_TEMPLATE = """\
id: .hidden-template
name: "Hidden Template"
version: "1.0.0"
description: "ID starts with dot — must be rejected."
author: "Tester"
phases:
  - id: phase-one
    name: "Phase One"
    description: "Simple test phase."
    model_tier: haiku
    task_type: generate
    prompt: "Process: {input}"
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
        # Always redirect to our temp user dir; keep other args unchanged.
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


@pytest.fixture()
def plain_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Yield an unpatched TestClient (for validate-only tests that don't write)."""
    from orchestration_engine.web.api import create_api_app

    app = create_api_app(db_path=str(tmp_path / "test-engine.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# POST /api/v1/templates/validate
# ---------------------------------------------------------------------------

class TestValidateTemplate:
    """Tests for the dry-run validate endpoint."""

    def test_validate_valid_template_returns_200(self, plain_client):
        res = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": MINIMAL_TEMPLATE, "extended": False},
        )
        assert res.status_code == 200

    def test_validate_valid_template_returns_valid_true(self, plain_client):
        data = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": MINIMAL_TEMPLATE, "extended": False},
        ).json()
        assert data["valid"] is True
        assert data["errors"] == []

    def test_validate_has_warnings_key(self, plain_client):
        data = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": MINIMAL_TEMPLATE, "extended": True},
        ).json()
        assert "warnings" in data

    def test_validate_extended_returns_list(self, plain_client):
        data = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": MINIMAL_TEMPLATE, "extended": True},
        ).json()
        assert isinstance(data["warnings"], list)

    def test_validate_invalid_yaml_returns_422(self, plain_client):
        res = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": INVALID_YAML},
        )
        assert res.status_code == 422

    def test_validate_missing_prompt_returns_valid_false(self, plain_client):
        data = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": INVALID_TEMPLATE, "extended": False},
        ).json()
        assert data["valid"] is False
        assert len(data["errors"]) >= 1

    def test_validate_missing_prompt_has_errors_list(self, plain_client):
        data = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": INVALID_TEMPLATE, "extended": False},
        ).json()
        assert isinstance(data["errors"], list)

    def test_validate_non_mapping_yaml_returns_422(self, plain_client):
        res = plain_client.post(
            "/api/v1/templates/validate",
            json={"content": "- just\n- a\n- list"},
        )
        assert res.status_code == 422

    def test_validate_does_not_write_any_file(self, tmp_path, plain_client):
        before = set(tmp_path.rglob("*.yaml"))
        plain_client.post(
            "/api/v1/templates/validate",
            json={"content": MINIMAL_TEMPLATE},
        )
        after = set(tmp_path.rglob("*.yaml"))
        assert before == after, "validate endpoint must not write any .yaml files"


# ---------------------------------------------------------------------------
# POST /api/v1/templates
# ---------------------------------------------------------------------------

class TestCreateTemplate:
    """Tests for the create endpoint."""

    def test_create_valid_template_returns_201(self, crud_client):
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        assert res.status_code == 201

    def test_create_returns_template_id(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["id"] == "test-crud-template"

    def test_create_returns_template_name(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["name"] == "Test CRUD Template"

    def test_create_returns_template_version(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["version"] == "1.0.0"

    def test_create_returns_source(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["source"] == "user"

    def test_create_returns_phases_count(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["phases_count"] == 1

    def test_create_returns_created_true(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["created"] is True

    def test_create_returns_path_ending_yaml(self, crud_client):
        client, _user_dir = crud_client
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        ).json()
        assert data["path"].endswith(".yaml")

    def test_create_writes_file_to_user_dir(self, crud_client):
        client, user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        written = list(user_dir.glob("*.yaml"))
        assert len(written) == 1
        assert written[0].stem == "test-crud-template"

    def test_create_file_contains_content(self, crud_client):
        client, user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        dest = user_dir / "test-crud-template.yaml"
        assert dest.exists()
        assert "test-crud-template" in dest.read_text(encoding="utf-8")

    def test_create_duplicate_returns_409(self, crud_client):
        client, _user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        res2 = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        assert res2.status_code == 409

    def test_create_duplicate_with_overwrite_returns_201(self, crud_client):
        client, _user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        res2 = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user", "overwrite": True},
        )
        assert res2.status_code == 201

    def test_create_overwrite_returns_created_false(self, crud_client):
        client, _user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user"},
        )
        data = client.post(
            "/api/v1/templates",
            json={"content": MINIMAL_TEMPLATE, "source": "user", "overwrite": True},
        ).json()
        assert data["created"] is False

    def test_create_invalid_yaml_returns_422(self, crud_client):
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": INVALID_YAML},
        )
        assert res.status_code == 422

    def test_create_invalid_template_returns_422(self, crud_client):
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": INVALID_TEMPLATE},
        )
        assert res.status_code == 422

    def test_create_invalid_template_does_not_write_file(self, crud_client):
        client, user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": INVALID_TEMPLATE},
        )
        assert list(user_dir.glob("*.yaml")) == []

    # ------------------------------------------------------------------
    # Security: path traversal prevention
    # ------------------------------------------------------------------

    def test_create_path_traversal_id_rejected(self, crud_client):
        """POST with id='../../evil' must return 422 (path traversal blocked)."""
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": PATH_TRAVERSAL_TEMPLATE, "source": "user"},
        )
        assert res.status_code == 422

    def test_create_path_traversal_does_not_write_file(self, crud_client):
        """A rejected path-traversal request must not write any file."""
        client, user_dir = crud_client
        client.post(
            "/api/v1/templates",
            json={"content": PATH_TRAVERSAL_TEMPLATE, "source": "user"},
        )
        # No .yaml file should exist anywhere under our tmp user dir
        assert list(user_dir.rglob("*.yaml")) == []

    def test_create_dot_id_rejected(self, crud_client):
        """POST with id='.hidden-template' (starts with dot) must return 422."""
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": DOT_ID_TEMPLATE, "source": "user"},
        )
        assert res.status_code == 422

    def test_create_path_traversal_error_message_contains_id(self, crud_client):
        """422 response detail should mention the offending template id."""
        client, _user_dir = crud_client
        res = client.post(
            "/api/v1/templates",
            json={"content": PATH_TRAVERSAL_TEMPLATE, "source": "user"},
        )
        # Detail can be a string or dict — either way, the id must appear
        body = res.text
        assert "../../evil" in body or "Invalid template id" in body


# ---------------------------------------------------------------------------
# PUT /api/v1/templates/{name}
# ---------------------------------------------------------------------------

class TestUpdateTemplate:
    """Tests for the update endpoint."""

    def _write_user_template(self, user_dir: Path) -> Path:
        """Write MINIMAL_TEMPLATE directly to the user templates dir."""
        dest = user_dir / "test-crud-template.yaml"
        dest.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
        return dest

    def test_update_user_template_returns_200(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        res = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": UPDATED_TEMPLATE},
        )
        assert res.status_code == 200

    def test_update_returns_updated_name(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        data = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": UPDATED_TEMPLATE},
        ).json()
        assert data["name"] == "Test CRUD Template (Updated)"

    def test_update_returns_updated_version(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        data = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": UPDATED_TEMPLATE},
        ).json()
        assert data["version"] == "1.1.0"

    def test_update_returns_created_false(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        data = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": UPDATED_TEMPLATE},
        ).json()
        assert data["created"] is False

    def test_update_overwrites_file_contents(self, crud_client):
        client, user_dir = crud_client
        dest = self._write_user_template(user_dir)
        client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": UPDATED_TEMPLATE},
        )
        written = dest.read_text(encoding="utf-8")
        assert "1.1.0" in written

    def test_update_nonexistent_template_returns_404(self, crud_client):
        client, _user_dir = crud_client
        res = client.put(
            "/api/v1/templates/no-such-template",
            json={"content": UPDATED_TEMPLATE},
        )
        assert res.status_code == 404

    def test_update_bundled_template_returns_403(self, crud_client):
        """Project/bundled templates (e.g. coding-pipeline-standard) must be protected."""
        client, _user_dir = crud_client
        res = client.put(
            "/api/v1/templates/coding-pipeline-standard",
            json={"content": UPDATED_TEMPLATE},
        )
        assert res.status_code == 403

    def test_update_invalid_content_returns_422(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        invalid_with_matching_id = INVALID_TEMPLATE.replace(
            "id: bad-template", "id: test-crud-template"
        )
        res = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": invalid_with_matching_id},
        )
        assert res.status_code == 422

    def test_update_invalid_yaml_returns_422(self, crud_client):
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        res = client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": INVALID_YAML},
        )
        assert res.status_code == 422

    def test_update_invalid_content_does_not_overwrite_file(self, crud_client):
        """A failed update must not corrupt the existing file."""
        client, user_dir = crud_client
        dest = self._write_user_template(user_dir)
        original_content = dest.read_text(encoding="utf-8")
        invalid_with_matching_id = INVALID_TEMPLATE.replace(
            "id: bad-template", "id: test-crud-template"
        )
        client.put(
            "/api/v1/templates/test-crud-template",
            json={"content": invalid_with_matching_id},
        )
        assert dest.read_text(encoding="utf-8") == original_content


# ---------------------------------------------------------------------------
# DELETE /api/v1/templates/{name}
# ---------------------------------------------------------------------------

class TestDeleteTemplate:
    """Tests for the delete endpoint."""

    def _write_user_template(self, user_dir: Path) -> Path:
        dest = user_dir / "test-crud-template.yaml"
        dest.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
        return dest

    def test_delete_user_template_returns_204(self, crud_client):
        """Successful delete must return 204 No Content (issue #324 spec)."""
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        res = client.delete("/api/v1/templates/test-crud-template")
        assert res.status_code == 204

    def test_delete_returns_empty_body(self, crud_client):
        """HTTP 204 responses must carry no body (per HTTP spec)."""
        client, user_dir = crud_client
        self._write_user_template(user_dir)
        res = client.delete("/api/v1/templates/test-crud-template")
        assert res.content == b""

    def test_delete_removes_file_from_disk(self, crud_client):
        client, user_dir = crud_client
        dest = self._write_user_template(user_dir)
        client.delete("/api/v1/templates/test-crud-template")
        assert not dest.exists()

    def test_delete_nonexistent_returns_404(self, crud_client):
        client, _user_dir = crud_client
        res = client.delete("/api/v1/templates/no-such-template")
        assert res.status_code == 404

    def test_delete_bundled_template_returns_403(self, crud_client):
        """Project/bundled templates (e.g. coding-pipeline-standard) must be protected."""
        client, _user_dir = crud_client
        res = client.delete("/api/v1/templates/coding-pipeline-standard")
        assert res.status_code == 403

    def test_delete_leaves_bundled_file_intact(self, crud_client):
        """Verify bundled/project template file is not deleted after a rejected request."""
        # Resolve the path directly (bypassing the fixture's monkeypatched engine)
        from orchestration_engine.templates import TemplateEngine
        from pathlib import Path

        # The bundled_dir is always relative to the templates.py source file,
        # so we can compute it directly without an engine instance.
        bundled_dir = Path(__file__).parent.parent .joinpath("templates")
        bundled_path = bundled_dir / "coding-pipeline-standard.yaml"
        assert bundled_path.exists(), "Precondition: coding-pipeline-standard.yaml must exist"

        client, _user_dir = crud_client
        client.delete("/api/v1/templates/coding-pipeline-standard")

        assert bundled_path.exists(), "Template file must not be deleted"
