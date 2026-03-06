"""Tests for webhook trigger configuration: TriggerConfig dataclass and DB CRUD.

Covers:
  - Group A: TriggerConfig dataclass construction and validation
  - Group B: Database CRUD operations (create/get/list/update/delete)
  - Group C: Edge cases and error conditions
  - Group D: Integration — TriggerConfig ↔ Database round-trip lifecycle
"""

import sqlite3
import time
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.webhooks import (
    VALID_MODES,
    TriggerConfig,
    TriggerValidationError,
    _ID_RE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Database:
    """Fresh in-memory Database for each test."""
    return Database(":memory:")


@pytest.fixture
def valid_trigger_data() -> Dict[str, Any]:
    """A fully-populated, valid trigger data dict."""
    return {
        "id": "trig-abc123def456",
        "template_id": "coding-pipeline-v1",
        "mode": "async",
        "secret": "my-secret",
        "rate_limit": 60,
        "input_map": {"repo": "$.repository.full_name"},
        "filters": [{"field": "event", "eq": "push"}],
    }


# ---------------------------------------------------------------------------
# Group A: TriggerConfig Dataclass + Validation
# ---------------------------------------------------------------------------


class TestTriggerConfigConstruction:
    """Happy-path construction and default values."""

    def test_trigger_config_valid_construction(self, valid_trigger_data):
        """Full construction with all fields should not raise."""
        tc = TriggerConfig(**{k: v for k, v in valid_trigger_data.items()
                               if k in TriggerConfig.__dataclass_fields__})
        assert tc.id == valid_trigger_data["id"]
        assert tc.template_id == valid_trigger_data["template_id"]
        assert tc.mode == "async"
        assert tc.rate_limit == 60

    def test_trigger_config_defaults(self):
        """Minimal construction (id + template_id) should apply safe defaults."""
        tc = TriggerConfig(id="trig-ab123456", template_id="my-template")
        assert tc.mode == "async"
        assert tc.rate_limit == 0
        assert tc.input_map == {}
        assert tc.filters == []
        assert tc.secret is None
        assert tc.created_at is None

    def test_trigger_config_all_valid_modes(self):
        """Each member of VALID_MODES must be accepted without error."""
        for mode in VALID_MODES:
            tc = TriggerConfig(id="trig-ab123456", template_id="tmpl", mode=mode)
            assert tc.mode == mode


class TestTriggerConfigValidation:
    """Validation failures must raise TriggerValidationError."""

    def test_trigger_config_invalid_id_too_short(self):
        """id of length 2 ('ab') is below the 3-char minimum."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="ab", template_id="tmpl")

    def test_trigger_config_invalid_id_starts_with_hyphen(self):
        """id starting with a hyphen must be rejected."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="-abc123def456", template_id="tmpl")

    def test_trigger_config_invalid_id_with_spaces(self):
        """id containing a space must be rejected."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig abc", template_id="tmpl")

    def test_trigger_config_empty_template_id(self):
        """Empty template_id must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="")

    def test_trigger_config_whitespace_template_id(self):
        """Whitespace-only template_id must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="   ")

    def test_trigger_config_invalid_mode(self):
        """An unrecognised mode value must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="tmpl", mode="webhook")

    def test_trigger_config_negative_rate_limit(self):
        """Negative rate_limit must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="tmpl", rate_limit=-1)

    def test_trigger_config_zero_rate_limit(self):
        """rate_limit=0 means unlimited and must be accepted."""
        tc = TriggerConfig(id="trig-ab123456", template_id="tmpl", rate_limit=0)
        assert tc.rate_limit == 0

    def test_trigger_config_large_rate_limit(self):
        """Very large rate_limit values must be accepted."""
        tc = TriggerConfig(id="trig-ab123456", template_id="tmpl", rate_limit=100_000)
        assert tc.rate_limit == 100_000

    def test_trigger_config_invalid_input_map_type(self):
        """input_map that is not a dict must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="tmpl",
                          input_map="not-a-dict")  # type: ignore[arg-type]

    def test_trigger_config_invalid_filters_type(self):
        """filters that is not a list must raise TriggerValidationError."""
        with pytest.raises(TriggerValidationError):
            TriggerConfig(id="trig-ab123456", template_id="tmpl",
                          filters={"not": "a-list"})  # type: ignore[arg-type]


class TestTriggerConfigSerialization:
    """to_dict / from_dict round-trips and generate_id."""

    def test_trigger_config_to_dict_round_trip(self, valid_trigger_data):
        """TriggerConfig.from_dict(tc.to_dict()) should produce an equal object."""
        tc = TriggerConfig(**{k: v for k, v in valid_trigger_data.items()
                               if k in TriggerConfig.__dataclass_fields__})
        reconstructed = TriggerConfig.from_dict(tc.to_dict())
        assert reconstructed.id == tc.id
        assert reconstructed.template_id == tc.template_id
        assert reconstructed.mode == tc.mode
        assert reconstructed.secret == tc.secret
        assert reconstructed.rate_limit == tc.rate_limit
        assert reconstructed.input_map == tc.input_map
        assert reconstructed.filters == tc.filters

    def test_trigger_config_generate_id(self):
        """generate_id() must return a string starting with 'trig-' that passes _ID_RE."""
        trigger_id = TriggerConfig.generate_id()
        assert trigger_id.startswith("trig-")
        assert _ID_RE.match(trigger_id), f"Generated id {trigger_id!r} did not match pattern"

    def test_from_dict_missing_optional_fields(self):
        """from_dict with only id + template_id must apply defaults for optional fields."""
        tc = TriggerConfig.from_dict({"id": "trig-ab123456", "template_id": "tmpl"})
        assert tc.mode == "async"
        assert tc.rate_limit == 0
        assert tc.input_map == {}
        assert tc.filters == []
        assert tc.secret is None

    def test_trigger_config_id_with_underscores(self):
        """Trigger ids containing underscores must be valid."""
        tc = TriggerConfig(id="trig_my_hook", template_id="tmpl")
        assert tc.id == "trig_my_hook"


# ---------------------------------------------------------------------------
# Group B: DB CRUD Operations
# ---------------------------------------------------------------------------


class TestCreateTrigger:
    """Tests for Database.create_trigger."""

    def test_create_trigger_returns_id(self, db, valid_trigger_data):
        """create_trigger must return the trigger id."""
        returned_id = db.create_trigger(valid_trigger_data)
        assert returned_id == valid_trigger_data["id"]

    def test_create_and_get_trigger(self, db, valid_trigger_data):
        """Created trigger must be retrievable and match the input data."""
        db.create_trigger(valid_trigger_data)
        row = db.get_trigger(valid_trigger_data["id"])
        assert row is not None
        assert row["id"] == valid_trigger_data["id"]
        assert row["template_id"] == valid_trigger_data["template_id"]
        assert row["mode"] == valid_trigger_data["mode"]
        assert row["secret"] == valid_trigger_data["secret"]
        assert row["rate_limit"] == valid_trigger_data["rate_limit"]
        assert row["input_map"] == valid_trigger_data["input_map"]
        assert row["filters"] == valid_trigger_data["filters"]

    def test_create_trigger_from_config_object(self, db, valid_trigger_data):
        """End-to-end: TriggerConfig → to_dict() → create_trigger → DB row."""
        tc = TriggerConfig(**{k: v for k, v in valid_trigger_data.items()
                               if k in TriggerConfig.__dataclass_fields__})
        db.create_trigger(tc.to_dict())
        row = db.get_trigger(tc.id)
        assert row is not None
        assert row["input_map"] == tc.input_map
        assert row["filters"] == tc.filters


class TestGetTrigger:
    """Tests for Database.get_trigger."""

    def test_get_trigger_not_found(self, db):
        """get_trigger for an unknown id must return None."""
        result = db.get_trigger("nonexistent-id-xyz")
        assert result is None


class TestListTriggers:
    """Tests for Database.list_triggers."""

    def test_list_triggers_empty(self, db):
        """list_triggers on a fresh DB must return an empty list."""
        assert db.list_triggers() == []

    def test_list_triggers_multiple(self, db):
        """list_triggers must return all created triggers ordered by created_at DESC."""
        ids = ["trig-aaa0000000aa", "trig-bbb1111111bb", "trig-ccc2222222cc"]
        for i, tid in enumerate(ids):
            db.create_trigger({
                "id": tid,
                "template_id": "tmpl",
                "mode": "async",
            })
            # Small sleep to ensure distinct created_at timestamps
            time.sleep(0.01)
        results = db.list_triggers()
        assert len(results) == 3
        # Ordered DESC — last inserted first
        assert results[0]["id"] == ids[2]
        assert results[2]["id"] == ids[0]

    def test_list_triggers_filter_by_template_id(self, db):
        """list_triggers with template_id filter returns only matching rows."""
        db.create_trigger({"id": "trig-aaabbbccc000", "template_id": "tmpl-a", "mode": "async"})
        db.create_trigger({"id": "trig-dddeeefff111", "template_id": "tmpl-b", "mode": "async"})
        db.create_trigger({"id": "trig-ggghhh000222", "template_id": "tmpl-a", "mode": "sync"})
        results = db.list_triggers(template_id="tmpl-a")
        assert len(results) == 2
        assert all(r["template_id"] == "tmpl-a" for r in results)

    def test_list_triggers_filter_by_mode(self, db):
        """list_triggers with mode filter returns only matching rows."""
        db.create_trigger({"id": "trig-aaabbbccc000", "template_id": "tmpl", "mode": "sync"})
        db.create_trigger({"id": "trig-dddeeefff111", "template_id": "tmpl", "mode": "async"})
        db.create_trigger({"id": "trig-ggghhh000222", "template_id": "tmpl", "mode": "async"})
        results = db.list_triggers(mode="sync")
        assert len(results) == 1
        assert results[0]["mode"] == "sync"

    def test_list_triggers_pagination(self, db):
        """Pagination (limit + offset) must return the correct slice."""
        ids = [f"trig-pg{i:02d}00000000" for i in range(5)]
        for i, tid in enumerate(ids):
            db.create_trigger({"id": tid, "template_id": "tmpl", "mode": "async"})
            time.sleep(0.01)
        # All 5, newest first → ids[4], ids[3], ids[2], ids[1], ids[0]
        page = db.list_triggers(limit=2, offset=2)
        assert len(page) == 2
        # Row 2 and 3 in DESC order → ids[2] and ids[1]
        assert page[0]["id"] == ids[2]
        assert page[1]["id"] == ids[1]

    def test_list_triggers_combined_filters(self, db):
        """template_id + mode combined filter returns intersection."""
        db.create_trigger({"id": "trig-aaabbbccc000", "template_id": "tmpl-a", "mode": "sync"})
        db.create_trigger({"id": "trig-dddeeefff111", "template_id": "tmpl-a", "mode": "async"})
        db.create_trigger({"id": "trig-ggghhh000222", "template_id": "tmpl-b", "mode": "sync"})
        results = db.list_triggers(template_id="tmpl-a", mode="sync")
        assert len(results) == 1
        assert results[0]["id"] == "trig-aaabbbccc000"


class TestUpdateTrigger:
    """Tests for Database.update_trigger."""

    def test_update_trigger_mode(self, db, valid_trigger_data):
        """update_trigger must persist mode changes."""
        db.create_trigger(valid_trigger_data)
        db.update_trigger(valid_trigger_data["id"], mode="sync")
        row = db.get_trigger(valid_trigger_data["id"])
        assert row["mode"] == "sync"

    def test_update_trigger_rate_limit(self, db, valid_trigger_data):
        """update_trigger must persist rate_limit changes."""
        db.create_trigger(valid_trigger_data)
        db.update_trigger(valid_trigger_data["id"], rate_limit=120)
        row = db.get_trigger(valid_trigger_data["id"])
        assert row["rate_limit"] == 120

    def test_update_trigger_input_map(self, db, valid_trigger_data):
        """update_trigger must JSON-serialise and correctly deserialise input_map."""
        db.create_trigger(valid_trigger_data)
        new_map = {"branch": "$.ref", "sha": "$.after"}
        db.update_trigger(valid_trigger_data["id"], input_map=new_map)
        row = db.get_trigger(valid_trigger_data["id"])
        assert row["input_map"] == new_map

    def test_update_trigger_filters(self, db, valid_trigger_data):
        """update_trigger must JSON-serialise and correctly deserialise filters."""
        db.create_trigger(valid_trigger_data)
        new_filters = [{"field": "ref", "startswith": "refs/heads/"}]
        db.update_trigger(valid_trigger_data["id"], filters=new_filters)
        row = db.get_trigger(valid_trigger_data["id"])
        assert row["filters"] == new_filters

    def test_update_trigger_secret(self, db, valid_trigger_data):
        """update_trigger must persist secret changes."""
        db.create_trigger(valid_trigger_data)
        db.update_trigger(valid_trigger_data["id"], secret="new-secret-value")
        row = db.get_trigger(valid_trigger_data["id"])
        assert row["secret"] == "new-secret-value"

    def test_update_trigger_not_found(self, db):
        """update_trigger for a non-existent id must return False."""
        result = db.update_trigger("nonexistent-xyz", mode="sync")
        assert result is False

    def test_update_trigger_no_valid_kwargs(self, db, valid_trigger_data):
        """update_trigger with no valid kwargs must return False without touching DB."""
        db.create_trigger(valid_trigger_data)
        result = db.update_trigger(valid_trigger_data["id"])
        assert result is False

    def test_update_trigger_unknown_field_ignored(self, db, valid_trigger_data):
        """update_trigger with only unknown kwargs must return False (no valid update)."""
        db.create_trigger(valid_trigger_data)
        result = db.update_trigger(valid_trigger_data["id"], foo="bar", baz=123)
        assert result is False

    def test_update_trigger_updated_at_changes(self, db, valid_trigger_data):
        """updated_at must advance after a successful update."""
        db.create_trigger(valid_trigger_data)
        row_before = db.get_trigger(valid_trigger_data["id"])
        time.sleep(0.05)  # Ensure timestamp difference
        db.update_trigger(valid_trigger_data["id"], mode="sync")
        row_after = db.get_trigger(valid_trigger_data["id"])
        # updated_at should be different (later) after the update
        assert row_after["updated_at"] != row_before["updated_at"]


class TestDeleteTrigger:
    """Tests for Database.delete_trigger."""

    def test_delete_trigger(self, db, valid_trigger_data):
        """Deleted trigger must no longer be retrievable."""
        db.create_trigger(valid_trigger_data)
        db.delete_trigger(valid_trigger_data["id"])
        assert db.get_trigger(valid_trigger_data["id"]) is None

    def test_delete_trigger_not_found(self, db):
        """delete_trigger for a non-existent id must return False."""
        result = db.delete_trigger("nonexistent-xyz")
        assert result is False

    def test_delete_trigger_returns_true(self, db, valid_trigger_data):
        """delete_trigger for an existing trigger must return True."""
        db.create_trigger(valid_trigger_data)
        result = db.delete_trigger(valid_trigger_data["id"])
        assert result is True


# ---------------------------------------------------------------------------
# Group C: Edge Cases and Error Conditions
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases, error conditions, and boundary values."""

    def test_duplicate_trigger_id_raises(self, db, valid_trigger_data):
        """Creating two triggers with the same id must raise IntegrityError."""
        db.create_trigger(valid_trigger_data)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_trigger(valid_trigger_data)

    def test_trigger_id_max_length(self, db):
        """64-character id must be accepted and stored correctly."""
        # 64 chars: 'a' + 62 'x' chars + 'a'
        long_id = "a" + "x" * 62 + "a"
        assert len(long_id) == 64
        db.create_trigger({"id": long_id, "template_id": "tmpl", "mode": "async"})
        row = db.get_trigger(long_id)
        assert row is not None
        assert row["id"] == long_id

    def test_trigger_id_min_length(self, db):
        """3-character id (valid format) must be accepted."""
        short_id = "a1a"
        db.create_trigger({"id": short_id, "template_id": "tmpl", "mode": "async"})
        row = db.get_trigger(short_id)
        assert row is not None
        assert row["id"] == short_id

    def test_trigger_secret_none(self, db, valid_trigger_data):
        """secret=None must be stored as NULL and returned as None."""
        data = {**valid_trigger_data, "secret": None}
        db.create_trigger(data)
        row = db.get_trigger(data["id"])
        assert row["secret"] is None

    def test_trigger_empty_input_map(self, db, valid_trigger_data):
        """input_map={} must be stored and returned as an empty dict."""
        data = {**valid_trigger_data, "input_map": {}}
        db.create_trigger(data)
        row = db.get_trigger(data["id"])
        assert row["input_map"] == {}

    def test_trigger_empty_filters(self, db, valid_trigger_data):
        """filters=[] must be stored and returned as an empty list."""
        data = {**valid_trigger_data, "filters": []}
        db.create_trigger(data)
        row = db.get_trigger(data["id"])
        assert row["filters"] == []

    def test_trigger_complex_filters(self, db, valid_trigger_data):
        """Complex nested filter structures must survive a JSON round-trip."""
        complex_filters = [
            {"field": "event", "eq": "push"},
            {"field": "ref", "startswith": "refs/heads/main"},
            {"nested": {"deeply": {"nested": True, "list": [1, 2, 3]}}},
        ]
        data = {**valid_trigger_data, "filters": complex_filters}
        db.create_trigger(data)
        row = db.get_trigger(data["id"])
        assert row["filters"] == complex_filters

    def test_json_fields_round_trip(self, db, valid_trigger_data):
        """input_map and filters must survive storage and retrieval as Python objects."""
        complex_input_map = {
            "repo": "$.repository.full_name",
            "ref": "$.ref",
            "sha": "$.after",
            "nested": {"key": "value"},
        }
        data = {**valid_trigger_data, "input_map": complex_input_map}
        db.create_trigger(data)
        row = db.get_trigger(data["id"])
        assert isinstance(row["input_map"], dict)
        assert row["input_map"] == complex_input_map
        assert isinstance(row["filters"], list)


# ---------------------------------------------------------------------------
# Group D: Integration — TriggerConfig ↔ Database round-trip
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Full lifecycle tests: create → get → update → list → delete."""

    def test_full_lifecycle(self, db, valid_trigger_data):
        """Complete CRUD lifecycle: create → get → update → list → delete."""
        tc = TriggerConfig(**{k: v for k, v in valid_trigger_data.items()
                               if k in TriggerConfig.__dataclass_fields__})

        # Create
        returned_id = db.create_trigger(tc.to_dict())
        assert returned_id == tc.id

        # Get
        row = db.get_trigger(tc.id)
        assert row is not None
        assert row["template_id"] == tc.template_id
        assert row["mode"] == tc.mode
        assert row["input_map"] == tc.input_map
        assert row["filters"] == tc.filters

        # Update
        db.update_trigger(tc.id, mode="sync", rate_limit=30)
        updated = db.get_trigger(tc.id)
        assert updated["mode"] == "sync"
        assert updated["rate_limit"] == 30

        # List
        results = db.list_triggers()
        assert any(r["id"] == tc.id for r in results)

        # Delete
        assert db.delete_trigger(tc.id) is True
        assert db.get_trigger(tc.id) is None

    def test_create_trigger_from_generate_id(self, db):
        """TriggerConfig.generate_id() must produce ids that work end-to-end with DB."""
        trigger_id = TriggerConfig.generate_id()
        tc = TriggerConfig(id=trigger_id, template_id="my-template")
        db.create_trigger(tc.to_dict())
        row = db.get_trigger(trigger_id)
        assert row is not None
        assert row["id"] == trigger_id

    def test_multiple_triggers_isolation(self, db):
        """Multiple triggers must be stored and retrieved independently."""
        tc1 = TriggerConfig(id="trig-aaa1111111bb", template_id="tmpl-a", mode="sync")
        tc2 = TriggerConfig(id="trig-bbb2222222cc", template_id="tmpl-b", mode="async",
                             input_map={"key": "value"})
        db.create_trigger(tc1.to_dict())
        db.create_trigger(tc2.to_dict())

        row1 = db.get_trigger(tc1.id)
        row2 = db.get_trigger(tc2.id)

        assert row1["mode"] == "sync"
        assert row1["input_map"] == {}
        assert row2["mode"] == "async"
        assert row2["input_map"] == {"key": "value"}
