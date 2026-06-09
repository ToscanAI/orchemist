"""Tests for POST /api/v1/issues/launch (Issue #642).

Regression coverage for the launch endpoint introduced in PR #634:

  * Bug 1 — the handler ``launch_issue_pipeline`` called ``generate_pipeline_input``
    without importing it, so any well-formed launch request raised
    ``NameError`` → HTTP 500. The fix adds a module-level import; a valid launch
    must now return 201.
  * The endpoint must keep returning 422 for a missing required field and 400
    for an unresolvable default template (existing behavior — pinned here so the
    500 crash path cannot silently reopen).

Uses FastAPI's TestClient with an isolated temp user-templates directory and a
seeded default template, so the endpoint resolves a template without any
network access. ``ORCH_DEFAULT_TEMPLATE`` is set via monkeypatch and read by the
handler at call time.

All tests rely on the engine's [web] extra (fastapi), which CI installs.
"""

from pathlib import Path
from typing import Generator, Tuple

import pytest

# fastapi + starlette.testclient are guaranteed by the engine's [web] extra,
# which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient


# A fully valid template: passes both basic and extended validation (has
# description, author, semver version, and one simple phase).
VALID_DEFAULT_TEMPLATE = """\
id: launch-default-tpl
name: "Launch Default Template"
version: "1.0.0"
description: "Default template used for /issues/launch tests."
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


def _make_client(user_templates_dir: Path, db_path: Path) -> TestClient:
    """Build a TestClient for the REST API with isolated templates + DB."""
    from orchestration_engine.web.api import create_api_app

    app = create_api_app(
        db_path=str(db_path),
        user_templates_dir=user_templates_dir,
    )
    # raise_server_exceptions=False so a 500 surfaces as a response (lets us
    # assert status==201 instead of crashing the test on the old NameError).
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def launch_client(
    tmp_path: Path, monkeypatch
) -> Generator[TestClient, None, None]:
    """Yield a TestClient with a seeded, resolvable default template."""
    user_dir = tmp_path / "user-templates"
    user_dir.mkdir()
    (user_dir / "launch-default-tpl.yaml").write_text(
        VALID_DEFAULT_TEMPLATE, encoding="utf-8"
    )
    monkeypatch.setenv("ORCH_DEFAULT_TEMPLATE", "launch-default-tpl")
    db_path = tmp_path / "engine.db"
    yield _make_client(user_dir, db_path)


class TestLaunchIssuePipeline:
    """Tests for POST /api/v1/issues/launch."""

    def test_valid_launch_returns_201(self, launch_client):
        """A well-formed launch request returns 201 (no NameError/500) — #642 Bug 1."""
        res = launch_client.post(
            "/api/v1/issues/launch",
            json={"issue_number": 642, "repo": "owner/repo", "title": "Fix the bug"},
        )
        assert res.status_code == 201, (
            f"Expected 201, got {res.status_code}; a 500 here means the "
            f"NameError regression is back. Body: {res.text}"
        )

    def test_valid_launch_no_500(self, launch_client):
        """The valid launch path must never surface a 500 — #642 Bug 1."""
        res = launch_client.post(
            "/api/v1/issues/launch",
            json={"issue_number": 1, "repo": "owner/repo"},
        )
        assert res.status_code != 500

    def test_valid_launch_body_shape(self, launch_client):
        """201 body carries status='accepted', a feat/<n>- branch_name, and template_id."""
        res = launch_client.post(
            "/api/v1/issues/launch",
            json={"issue_number": 642, "repo": "owner/repo", "title": "Fix the bug"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["status"] == "accepted"
        assert body["branch_name"].startswith("feat/642-")
        assert body["template_id"] == "launch-default-tpl"

    def test_missing_issue_number_returns_422(self, launch_client):
        """Omitting the required issue_number → pydantic 422 (not 500)."""
        res = launch_client.post(
            "/api/v1/issues/launch",
            json={"repo": "owner/repo"},
        )
        assert res.status_code == 422

    def test_bogus_default_template_returns_400(self, tmp_path, monkeypatch):
        """An unresolvable ORCH_DEFAULT_TEMPLATE → 400 (not 500)."""
        user_dir = tmp_path / "user-templates"
        user_dir.mkdir()
        monkeypatch.setenv("ORCH_DEFAULT_TEMPLATE", "no-such-template-xyz")
        client = _make_client(user_dir, tmp_path / "engine.db")
        res = client.post(
            "/api/v1/issues/launch",
            json={"issue_number": 99, "repo": "owner/repo"},
        )
        assert res.status_code == 400
        assert "no-such-template-xyz" in res.json()["detail"]
