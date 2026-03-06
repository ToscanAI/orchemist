"""Tests for Trigger CRUD REST API endpoints (Issue #329.4).

Covers:
  - Group A: POST /api/v1/triggers — create trigger
  - Group B: GET /api/v1/triggers — list triggers (with ?enabled= filter)
  - Group C: GET /api/v1/triggers/{id} — get trigger by id
  - Group D: PUT /api/v1/triggers/{id} — update trigger
  - Group E: DELETE /api/v1/triggers/{id} — delete trigger
  - Group F: Secret write-only enforcement (never returned in responses)
  - Group G: SlidingWindowRateLimiter unit tests
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Skip entire module when FastAPI / starlette is not installed.
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from orchestration_engine.db import Database  # noqa: E402
from orchestration_engine.web.api import SlidingWindowRateLimiter  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A real bundled template ID for tests that need template resolution.
_TEMPLATE_ID = "coding-pipeline-v1"

# Valid trigger IDs matching the required pattern.
_TRIGGER_ID = "trig-testcrud0001"
_TRIGGER_ID_2 = "trig-testcrud0002"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by an isolated file-based DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _insert_trigger(
    db_path: str,
    trigger_id: str = _TRIGGER_ID,
    template_id: str = _TEMPLATE_ID,
    mode: str = "async",
    secret: str = None,
    rate_limit: int = 0,
    input_map: Dict[str, Any] = None,
    filters: list = None,
    enabled: bool = True,
) -> None:
    """Insert a trigger row directly into the test database."""
    db = Database(Path(db_path))
    db.create_trigger(
        {
            "id": trigger_id,
            "template_id": template_id,
            "mode": mode,
            "secret": secret,
            "rate_limit": rate_limit,
            "input_map": input_map or {},
            "filters": filters or [],
            "enabled": enabled,
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return path to an isolated test database file."""
    return str(tmp_path / "test-engine.db")


@pytest.fixture()
def client(tmp_path: Path):
    """Isolated TestClient per test (separate DB file in tmp_path)."""
    with _make_client(tmp_path) as c:
        yield c


@pytest.fixture()
def client_with_db(tmp_path: Path):
    """Yield (TestClient, db_path) tuple for tests that need direct DB access."""
    db_file = str(tmp_path / "test-engine.db")
    from orchestration_engine.web.api import create_api_app
    app = create_api_app(db_path=db_file)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, db_file


# ---------------------------------------------------------------------------
# Group A: POST /api/v1/triggers — create
# ---------------------------------------------------------------------------

class TestCreateTrigger:
    """POST /api/v1/triggers — create a new webhook trigger."""

    def test_create_returns_201(self, client):
        """A valid create request returns 201."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 201

    def test_create_response_has_required_fields(self, client):
        """Response body must include all TriggerResponse fields."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID, "id": _TRIGGER_ID},
        )
        assert res.status_code == 201
        data = res.json()
        for field in ("id", "template_id", "mode", "secret", "rate_limit",
                      "input_map", "filters", "enabled", "created_at"):
            assert field in data, f"Missing field: {field}"

    def test_create_explicit_id(self, client):
        """When an explicit id is provided, it is used."""
        res = client.post(
            "/api/v1/triggers",
            json={"id": _TRIGGER_ID, "template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 201
        assert res.json()["id"] == _TRIGGER_ID

    def test_create_auto_generates_id_when_omitted(self, client):
        """When id is omitted, an auto-generated id is returned."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 201
        generated_id = res.json()["id"]
        assert generated_id.startswith("trig-")
        assert len(generated_id) > 5

    def test_create_default_values(self, client):
        """Defaults: mode=async, rate_limit=0, enabled=True."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID},
        )
        data = res.json()
        assert data["mode"] == "async"
        assert data["rate_limit"] == 0
        assert data["enabled"] is True
        assert data["input_map"] == {}
        assert data["filters"] == []

    def test_create_with_all_fields(self, client):
        """All optional fields can be set on creation."""
        res = client.post(
            "/api/v1/triggers",
            json={
                "id": _TRIGGER_ID,
                "template_id": _TEMPLATE_ID,
                "mode": "sync",
                "secret": "s3cr3t",
                "rate_limit": 10,
                "input_map": {"repo": "$.repository.name"},
                "filters": [{"branch": "main"}],
                "enabled": False,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["mode"] == "sync"
        assert data["rate_limit"] == 10
        assert data["enabled"] is False
        assert data["input_map"] == {"repo": "$.repository.name"}
        assert data["filters"] == [{"branch": "main"}]

    def test_create_duplicate_id_returns_409(self, client):
        """Creating a trigger with a duplicate id returns 409."""
        client.post(
            "/api/v1/triggers",
            json={"id": _TRIGGER_ID, "template_id": _TEMPLATE_ID},
        )
        res = client.post(
            "/api/v1/triggers",
            json={"id": _TRIGGER_ID, "template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 409

    def test_create_invalid_mode_returns_400(self, client):
        """An invalid mode value returns 400."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID, "id": _TRIGGER_ID, "mode": "turbo"},
        )
        assert res.status_code == 400

    def test_create_missing_template_id_returns_422(self, client):
        """Missing required template_id field returns 422."""
        res = client.post(
            "/api/v1/triggers",
            json={"id": _TRIGGER_ID},
        )
        assert res.status_code == 422

    def test_create_invalid_id_format_returns_400(self, client):
        """A trigger id that fails pattern validation returns 400."""
        res = client.post(
            "/api/v1/triggers",
            json={"id": "!invalid!", "template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 400

    def test_create_negative_rate_limit_returns_400(self, client):
        """Negative rate_limit returns 400."""
        res = client.post(
            "/api/v1/triggers",
            json={"template_id": _TEMPLATE_ID, "id": _TRIGGER_ID, "rate_limit": -1},
        )
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# Group B: GET /api/v1/triggers — list
# ---------------------------------------------------------------------------

class TestListTriggers:
    """GET /api/v1/triggers — list webhook triggers."""

    def test_list_empty_returns_200_with_empty_items(self, client):
        """When no triggers exist, returns 200 with empty items list."""
        res = client.get("/api/v1/triggers")
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert data["items"] == []

    def test_list_returns_all_triggers(self, client_with_db):
        """All existing triggers are returned when no filters applied."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, template_id=_TEMPLATE_ID)
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2, template_id=_TEMPLATE_ID)

        res = client.get("/api/v1/triggers")
        assert res.status_code == 200
        data = res.json()
        ids = [item["id"] for item in data["items"]]
        assert _TRIGGER_ID in ids
        assert _TRIGGER_ID_2 in ids

    def test_list_filter_enabled_true(self, client_with_db):
        """?enabled=true returns only enabled triggers."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, enabled=True)
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2, enabled=False)

        res = client.get("/api/v1/triggers?enabled=true")
        assert res.status_code == 200
        items = res.json()["items"]
        assert all(item["enabled"] is True for item in items)
        ids = [item["id"] for item in items]
        assert _TRIGGER_ID in ids
        assert _TRIGGER_ID_2 not in ids

    def test_list_filter_enabled_false(self, client_with_db):
        """?enabled=false returns only disabled triggers."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, enabled=True)
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2, enabled=False)

        res = client.get("/api/v1/triggers?enabled=false")
        assert res.status_code == 200
        items = res.json()["items"]
        assert all(item["enabled"] is False for item in items)
        ids = [item["id"] for item in items]
        assert _TRIGGER_ID_2 in ids
        assert _TRIGGER_ID not in ids

    def test_list_filter_enabled_invalid_returns_400(self, client):
        """?enabled=maybe returns 400."""
        res = client.get("/api/v1/triggers?enabled=maybe")
        assert res.status_code == 400

    def test_list_filter_by_template_id(self, client_with_db):
        """?template_id= filters to only matching triggers."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, template_id="template-a-01")
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2, template_id="template-b-01")

        res = client.get("/api/v1/triggers?template_id=template-a-01")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == _TRIGGER_ID

    def test_list_response_items_have_required_fields(self, client_with_db):
        """Each item in the list has all TriggerResponse fields."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.get("/api/v1/triggers")
        items = res.json()["items"]
        assert len(items) == 1
        for field in ("id", "template_id", "mode", "secret", "rate_limit",
                      "input_map", "filters", "enabled", "created_at"):
            assert field in items[0], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Group C: GET /api/v1/triggers/{id} — get by id
# ---------------------------------------------------------------------------

class TestGetTrigger:
    """GET /api/v1/triggers/{id} — get a single trigger."""

    def test_get_existing_returns_200(self, client_with_db):
        """An existing trigger returns 200 with trigger data."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID}")
        assert res.status_code == 200
        assert res.json()["id"] == _TRIGGER_ID

    def test_get_nonexistent_returns_404(self, client):
        """A nonexistent trigger id returns 404."""
        res = client.get("/api/v1/triggers/nonexistent-xx-01")
        assert res.status_code == 404

    def test_get_response_has_correct_fields(self, client_with_db):
        """Response has all expected fields with correct values."""
        client, db_file = client_with_db
        _insert_trigger(
            db_file,
            trigger_id=_TRIGGER_ID,
            template_id=_TEMPLATE_ID,
            mode="sync",
            rate_limit=5,
            input_map={"key": "val"},
            filters=[{"branch": "main"}],
            enabled=True,
        )

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID}")
        data = res.json()
        assert data["id"] == _TRIGGER_ID
        assert data["template_id"] == _TEMPLATE_ID
        assert data["mode"] == "sync"
        assert data["rate_limit"] == 5
        assert data["input_map"] == {"key": "val"}
        assert data["filters"] == [{"branch": "main"}]
        assert data["enabled"] is True


# ---------------------------------------------------------------------------
# Group D: PUT /api/v1/triggers/{id} — update
# ---------------------------------------------------------------------------

class TestUpdateTrigger:
    """PUT /api/v1/triggers/{id} — update an existing trigger."""

    def test_update_returns_200(self, client_with_db):
        """A valid update returns 200 with updated data."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"mode": "sync"},
        )
        assert res.status_code == 200

    def test_update_mode(self, client_with_db):
        """Mode field is updated correctly."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, mode="async")

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"mode": "sync"},
        )
        assert res.json()["mode"] == "sync"

    def test_update_rate_limit(self, client_with_db):
        """rate_limit field is updated correctly."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, rate_limit=0)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"rate_limit": 30},
        )
        assert res.json()["rate_limit"] == 30

    def test_update_enabled_flag(self, client_with_db):
        """enabled flag is updated correctly."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, enabled=True)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"enabled": False},
        )
        assert res.json()["enabled"] is False

    def test_update_input_map(self, client_with_db):
        """input_map field is updated correctly."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        new_map = {"repo": "$.repository.full_name"}
        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"input_map": new_map},
        )
        assert res.json()["input_map"] == new_map

    def test_update_filters(self, client_with_db):
        """filters field is updated correctly."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        new_filters = [{"branch": "develop"}, {"action": "opened"}]
        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"filters": new_filters},
        )
        assert res.json()["filters"] == new_filters

    def test_update_partial_leaves_other_fields_unchanged(self, client_with_db):
        """Updating one field does not affect other fields."""
        client, db_file = client_with_db
        _insert_trigger(
            db_file,
            trigger_id=_TRIGGER_ID,
            mode="fire_and_forget",
            rate_limit=5,
        )

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"enabled": False},
        )
        data = res.json()
        # Other fields unchanged
        assert data["mode"] == "fire_and_forget"
        assert data["rate_limit"] == 5
        assert data["enabled"] is False

    def test_update_nonexistent_returns_404(self, client):
        """Updating a nonexistent trigger returns 404."""
        res = client.put(
            "/api/v1/triggers/nonexistent-xx-01",
            json={"mode": "sync"},
        )
        assert res.status_code == 404

    def test_update_invalid_mode_returns_400(self, client_with_db):
        """Updating to an invalid mode returns 400."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"mode": "ultra-fast"},
        )
        assert res.status_code == 400

    def test_update_empty_body_returns_200_no_change(self, client_with_db):
        """An empty body (no fields) is a no-op and returns 200."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, mode="async", rate_limit=3)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["mode"] == "async"
        assert data["rate_limit"] == 3

    def test_update_secret_is_redacted_in_response(self, client_with_db):
        """After updating the secret, the response still redacts it."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.put(
            f"/api/v1/triggers/{_TRIGGER_ID}",
            json={"secret": "new_secret_value"},
        )
        assert res.status_code == 200
        assert res.json()["secret"] == "***"


# ---------------------------------------------------------------------------
# Group E: DELETE /api/v1/triggers/{id} — delete
# ---------------------------------------------------------------------------

class TestDeleteTrigger:
    """DELETE /api/v1/triggers/{id} — delete a webhook trigger."""

    def test_delete_existing_returns_204(self, client_with_db):
        """Deleting an existing trigger returns 204 (no content)."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        res = client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")
        assert res.status_code == 204

    def test_delete_removes_trigger_from_list(self, client_with_db):
        """After deletion the trigger no longer appears in list."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")

        res = client.get("/api/v1/triggers")
        ids = [item["id"] for item in res.json()["items"]]
        assert _TRIGGER_ID not in ids

    def test_delete_nonexistent_returns_404(self, client):
        """Deleting a nonexistent trigger returns 404."""
        res = client.delete("/api/v1/triggers/nonexistent-xx-01")
        assert res.status_code == 404

    def test_delete_then_get_returns_404(self, client_with_db):
        """After deletion, GET on the same id returns 404."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)

        client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID}")
        assert res.status_code == 404

    def test_delete_does_not_affect_other_triggers(self, client_with_db):
        """Deleting one trigger does not remove other triggers."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID)
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2)

        client.delete(f"/api/v1/triggers/{_TRIGGER_ID}")

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID_2}")
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Group F: Secret write-only enforcement
# ---------------------------------------------------------------------------

class TestSecretRedaction:
    """Secret field must never be exposed in API responses."""

    def test_create_with_secret_response_redacts_it(self, client):
        """Creating a trigger with a secret: response shows '***'."""
        res = client.post(
            "/api/v1/triggers",
            json={
                "id": _TRIGGER_ID,
                "template_id": _TEMPLATE_ID,
                "secret": "my-super-secret",
            },
        )
        assert res.status_code == 201
        assert res.json()["secret"] == "***"

    def test_create_without_secret_response_shows_none(self, client):
        """Creating a trigger without secret: response shows null/None."""
        res = client.post(
            "/api/v1/triggers",
            json={"id": _TRIGGER_ID, "template_id": _TEMPLATE_ID},
        )
        assert res.status_code == 201
        assert res.json()["secret"] is None

    def test_get_with_secret_response_redacts_it(self, client_with_db):
        """GET /api/v1/triggers/{id} with stored secret returns '***'."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, secret="my-super-secret")

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID}")
        assert res.status_code == 200
        assert res.json()["secret"] == "***"

    def test_get_without_secret_response_shows_none(self, client_with_db):
        """GET /api/v1/triggers/{id} without stored secret returns null."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, secret=None)

        res = client.get(f"/api/v1/triggers/{_TRIGGER_ID}")
        assert res.status_code == 200
        assert res.json()["secret"] is None

    def test_list_with_secrets_all_redacted(self, client_with_db):
        """All secrets are redacted in list responses."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, secret="secret1")
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID_2, secret="secret2")

        res = client.get("/api/v1/triggers")
        items = res.json()["items"]
        for item in items:
            assert item["secret"] == "***", (
                f"Expected '***' but got {item['secret']!r} for trigger {item['id']}"
            )

    def test_list_without_secrets_shows_none(self, client_with_db):
        """Triggers without secrets show null in list responses."""
        client, db_file = client_with_db
        _insert_trigger(db_file, trigger_id=_TRIGGER_ID, secret=None)

        res = client.get("/api/v1/triggers")
        items = res.json()["items"]
        assert items[0]["secret"] is None

    def test_secret_raw_value_not_in_response_body(self, client):
        """The actual secret value must not appear anywhere in the response body."""
        secret_value = "ultra-secret-value-12345"
        res = client.post(
            "/api/v1/triggers",
            json={
                "id": _TRIGGER_ID,
                "template_id": _TEMPLATE_ID,
                "secret": secret_value,
            },
        )
        assert res.status_code == 201
        assert secret_value not in res.text


# ---------------------------------------------------------------------------
# Group G: SlidingWindowRateLimiter unit tests
# ---------------------------------------------------------------------------

class TestSlidingWindowRateLimiter:
    """SlidingWindowRateLimiter — unit tests for the named rate limiter class."""

    def _make_db(self, tmp_path: Path) -> Database:
        """Create an isolated DB instance for rate limiter tests."""
        db_file = str(tmp_path / "rl-test.db")
        return Database(Path(db_file))

    def test_zero_rate_limit_always_allows(self, tmp_path: Path):
        """rate_limit=0 means unlimited — check always returns False."""
        db = self._make_db(tmp_path)
        limiter = SlidingWindowRateLimiter(db)
        assert limiter.check(_TRIGGER_ID, 0) is False

    def test_under_limit_allows(self, tmp_path: Path):
        """When invocation count is below rate_limit, check returns False."""
        db = self._make_db(tmp_path)
        # Insert trigger so invocation recording works
        db.create_trigger({
            "id": _TRIGGER_ID,
            "template_id": _TEMPLATE_ID,
            "mode": "async",
            "rate_limit": 5,
            "input_map": {},
            "filters": [],
            "enabled": True,
        })
        # Record 2 invocations; limit is 5
        db.record_webhook_invocation(_TRIGGER_ID)
        db.record_webhook_invocation(_TRIGGER_ID)

        limiter = SlidingWindowRateLimiter(db)
        assert limiter.check(_TRIGGER_ID, 5) is False

    def test_at_limit_blocks(self, tmp_path: Path):
        """When invocation count equals rate_limit, check returns True."""
        db = self._make_db(tmp_path)
        db.create_trigger({
            "id": _TRIGGER_ID,
            "template_id": _TEMPLATE_ID,
            "mode": "async",
            "rate_limit": 3,
            "input_map": {},
            "filters": [],
            "enabled": True,
        })
        # Record exactly 3 invocations
        for _ in range(3):
            db.record_webhook_invocation(_TRIGGER_ID)

        limiter = SlidingWindowRateLimiter(db)
        assert limiter.check(_TRIGGER_ID, 3) is True

    def test_over_limit_blocks(self, tmp_path: Path):
        """When invocation count exceeds rate_limit, check returns True."""
        db = self._make_db(tmp_path)
        db.create_trigger({
            "id": _TRIGGER_ID,
            "template_id": _TEMPLATE_ID,
            "mode": "async",
            "rate_limit": 2,
            "input_map": {},
            "filters": [],
            "enabled": True,
        })
        for _ in range(5):
            db.record_webhook_invocation(_TRIGGER_ID)

        limiter = SlidingWindowRateLimiter(db)
        assert limiter.check(_TRIGGER_ID, 2) is True

    def test_different_triggers_are_isolated(self, tmp_path: Path):
        """Rate limit checks are per trigger_id — other triggers don't interfere."""
        db = self._make_db(tmp_path)
        for tid in (_TRIGGER_ID, _TRIGGER_ID_2):
            db.create_trigger({
                "id": tid,
                "template_id": _TEMPLATE_ID,
                "mode": "async",
                "rate_limit": 2,
                "input_map": {},
                "filters": [],
                "enabled": True,
            })
        # Saturate only TRIGGER_ID
        for _ in range(3):
            db.record_webhook_invocation(_TRIGGER_ID)

        limiter = SlidingWindowRateLimiter(db)
        # TRIGGER_ID should be blocked
        assert limiter.check(_TRIGGER_ID, 2) is True
        # TRIGGER_ID_2 should be allowed (no invocations)
        assert limiter.check(_TRIGGER_ID_2, 2) is False

    def test_limiter_is_instantiable_with_db(self, tmp_path: Path):
        """SlidingWindowRateLimiter can be instantiated with a DB instance."""
        db = self._make_db(tmp_path)
        limiter = SlidingWindowRateLimiter(db)
        assert limiter is not None
        assert hasattr(limiter, "check")
