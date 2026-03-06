"""Tests for Issue #330.1 — Chain Config Schema, Template Parsing, and DB Migration.

Covers:
- AC-01: Parsing valid on_complete blocks (success + failed lists)
- AC-02: Rejecting entries missing the 'template' key
- AC-03: max_chain_depth defaults to 5 when not specified
- AC-04: Custom max_chain_depth is parsed correctly
- AC-05: DB migration 006 — parent_run_id column exists after migration
- AC-06: Run creation with parent_run_id and chain_depth persists correctly
- AC-07: validate_template() catches invalid on_complete structure
- AC-08: PipelineTemplate.on_complete is None when block is absent
- AC-09: OnCompleteEntry.input_map defaults to empty dict
- AC-10: on_complete block with only success list (no failed) is valid
"""

import json
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.templates import (
    OnCompleteConfig,
    OnCompleteEntry,
    PipelineTemplate,
    TemplateEngine,
    _parse_on_complete_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_db():
    """Return an in-memory Database instance with all migrations applied."""
    return Database(":memory:")


@pytest.fixture
def sample_run() -> Dict[str, Any]:
    """Return a minimal pipeline_run record dict."""
    return {
        "run_id": "chain-test-001",
        "template_path": "/tmp/template.yaml",
        "template_id": "test-pipeline",
        "input_json": json.dumps({"topic": "chaining"}),
        "mode": "dry-run",
        "output_dir": "/tmp/output/chain-test",
        "status": "pending",
        "gateway_url": None,
        "skip_scoring": 0,
    }


@pytest.fixture
def engine():
    """Return a TemplateEngine with no custom search paths."""
    return TemplateEngine()


def _minimal_template_yaml(
    template_id: str = "chain-tpl",
    name: str = "Chain Test Template",
    *,
    extra: str = "",
) -> str:
    """Return a minimal valid template YAML with optional extra top-level fields."""
    return textwrap.dedent(f"""\
        id: {template_id}
        name: "{name}"
        version: "1.0.0"
        description: "A minimal template for chain config tests."
        author: "Test Author"
        {extra}
        phases:
          - id: phase_a
            name: Phase A
            model_tier: haiku
            thinking_level: off
            depends_on: []
            prompt_template: "Hello {{input}}"
    """)


# ---------------------------------------------------------------------------
# AC-01: Parsing valid on_complete blocks
# ---------------------------------------------------------------------------


class TestParseOnCompleteConfig:
    """Unit tests for _parse_on_complete_config()."""

    def test_returns_none_when_raw_is_none(self):
        assert _parse_on_complete_config(None) is None

    def test_returns_none_when_raw_is_not_dict(self):
        assert _parse_on_complete_config("not-a-dict") is None
        assert _parse_on_complete_config(42) is None
        assert _parse_on_complete_config([]) is None

    def test_parse_valid_success_and_failed(self):
        raw = {
            "success": [
                {"template": "notify-pipeline"},
            ],
            "failed": [
                {"template": "cleanup-pipeline", "input_map": {"reason": "failed"}},
            ],
        }
        config = _parse_on_complete_config(raw)
        assert config is not None
        assert isinstance(config, OnCompleteConfig)
        assert len(config.success) == 1
        assert config.success[0].template == "notify-pipeline"
        assert config.success[0].input_map == {}
        assert len(config.failed) == 1
        assert config.failed[0].template == "cleanup-pipeline"
        assert config.failed[0].input_map == {"reason": "failed"}

    def test_parse_only_success_no_failed(self):
        raw = {
            "success": [{"template": "downstream-pipeline"}],
        }
        config = _parse_on_complete_config(raw)
        assert config is not None
        assert len(config.success) == 1
        assert config.failed == []

    def test_parse_only_failed_no_success(self):
        raw = {
            "failed": [{"template": "alert-pipeline"}],
        }
        config = _parse_on_complete_config(raw)
        assert config is not None
        assert config.success == []
        assert len(config.failed) == 1
        assert config.failed[0].template == "alert-pipeline"

    def test_parse_empty_dict_returns_config_with_defaults(self):
        """An empty dict still produces an OnCompleteConfig with default values."""
        config = _parse_on_complete_config({})
        assert config is not None
        assert config.success == []
        assert config.failed == []
        assert config.max_chain_depth == 5

    def test_multiple_entries_in_success(self):
        raw = {
            "success": [
                {"template": "pipeline-a"},
                {"template": "pipeline-b", "input_map": {"key": "value"}},
            ],
        }
        config = _parse_on_complete_config(raw)
        assert config is not None
        assert len(config.success) == 2
        assert config.success[0].template == "pipeline-a"
        assert config.success[1].template == "pipeline-b"
        assert config.success[1].input_map == {"key": "value"}


# ---------------------------------------------------------------------------
# AC-02: Rejecting entries missing 'template' key
# ---------------------------------------------------------------------------


class TestOnCompleteEntryValidation:
    """Tests for OnCompleteEntry validation and _parse_on_complete_config rejection."""

    def test_missing_template_key_raises_value_error(self):
        """An entry without 'template' must raise ValueError during parsing."""
        raw = {
            "success": [
                {"input_map": {"key": "value"}},  # missing 'template'
            ],
        }
        with pytest.raises(ValueError, match="template"):
            _parse_on_complete_config(raw)

    def test_on_complete_entry_empty_template_raises(self):
        """OnCompleteEntry with empty template string must raise ValueError."""
        with pytest.raises(ValueError, match="non-empty string"):
            OnCompleteEntry(template="")

    def test_on_complete_entry_non_string_template_raises(self):
        """OnCompleteEntry with non-string template must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            OnCompleteEntry(template=None)  # type: ignore[arg-type]

    def test_on_complete_entry_valid(self):
        entry = OnCompleteEntry(template="my-pipeline")
        assert entry.template == "my-pipeline"
        assert entry.input_map == {}

    def test_on_complete_entry_with_input_map(self):
        entry = OnCompleteEntry(template="my-pipeline", input_map={"k": "v"})
        assert entry.input_map == {"k": "v"}


# ---------------------------------------------------------------------------
# AC-03 & AC-04: max_chain_depth default and custom values
# ---------------------------------------------------------------------------


class TestMaxChainDepth:
    def test_default_max_chain_depth_is_5(self):
        config = _parse_on_complete_config({})
        assert config is not None
        assert config.max_chain_depth == 5

    def test_custom_max_chain_depth(self):
        config = _parse_on_complete_config({"max_chain_depth": 3})
        assert config is not None
        assert config.max_chain_depth == 3

    def test_max_chain_depth_clamped_to_1_minimum(self):
        """max_chain_depth < 1 should be clamped to 1 by OnCompleteConfig.__post_init__."""
        config = OnCompleteConfig(max_chain_depth=0)
        assert config.max_chain_depth == 1

    def test_on_complete_config_direct_instantiation(self):
        config = OnCompleteConfig(
            success=[OnCompleteEntry(template="p1")],
            failed=[],
            max_chain_depth=10,
        )
        assert config.max_chain_depth == 10
        assert len(config.success) == 1
        assert config.success[0].template == "p1"


# ---------------------------------------------------------------------------
# AC-05: DB migration — parent_run_id column exists
# ---------------------------------------------------------------------------


class TestDBMigrationChainColumns:
    def test_parent_run_id_column_exists(self, in_memory_db):
        """After DB init, pipeline_runs must have a parent_run_id column."""
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
            cursor = conn.execute("PRAGMA table_info(pipeline_runs)")
            columns = {row[1] for row in cursor.fetchall()}
        assert "parent_run_id" in columns, (
            f"parent_run_id column missing from pipeline_runs. Columns: {columns}"
        )

    def test_chain_depth_column_exists(self, in_memory_db):
        """After DB init, pipeline_runs must have a chain_depth column."""
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
            cursor = conn.execute("PRAGMA table_info(pipeline_runs)")
            columns = {row[1] for row in cursor.fetchall()}
        assert "chain_depth" in columns, (
            f"chain_depth column missing from pipeline_runs. Columns: {columns}"
        )

    def test_migration_006_is_recorded(self, in_memory_db):
        """Migration 006 should be in the migrations table after DB init."""
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
            cursor = conn.execute(
                "SELECT name FROM migrations WHERE name = '006_add_chain_columns'"
            )
            row = cursor.fetchone()
        assert row is not None, "Migration '006_add_chain_columns' not recorded"

    def test_migration_idempotent_on_existing_db(self, in_memory_db):
        """Running migration 006 again must not raise (idempotent)."""
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
        # Should not raise even when columns already exist.
        in_memory_db._migration_006_add_chain_columns(conn)


# ---------------------------------------------------------------------------
# AC-06: Run creation with parent_run_id and chain_depth
# ---------------------------------------------------------------------------


class TestInsertPipelineRunWithChainFields:
    def test_insert_with_parent_run_id(self, in_memory_db, sample_run):
        """A run with parent_run_id should persist the value correctly."""
        sample_run["parent_run_id"] = "parent-run-999"
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run is not None
        assert run["parent_run_id"] == "parent-run-999"

    def test_insert_with_chain_depth(self, in_memory_db, sample_run):
        """A run with chain_depth should persist the value correctly."""
        sample_run["chain_depth"] = 2
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run is not None
        assert run["chain_depth"] == 2

    def test_insert_without_chain_fields_defaults(self, in_memory_db, sample_run):
        """When chain fields are absent, defaults should be None/0."""
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run is not None
        assert run["parent_run_id"] is None
        assert run["chain_depth"] == 0

    def test_insert_parent_run_id_none_is_accepted(self, in_memory_db, sample_run):
        """Explicitly passing parent_run_id=None should not fail."""
        sample_run["parent_run_id"] = None
        in_memory_db.insert_pipeline_run(sample_run)
        run = in_memory_db.get_pipeline_run(sample_run["run_id"])
        assert run is not None
        assert run["parent_run_id"] is None


# ---------------------------------------------------------------------------
# AC-07: validate_template() catches invalid on_complete
# ---------------------------------------------------------------------------


class TestValidateTemplateOnComplete:
    def _write_yaml(self, tmp_path: Path, name: str, content: str) -> Path:
        """Write YAML content to a temp file and return the path."""
        p = tmp_path / name
        p.write_text(textwrap.dedent(content))
        return p

    def test_valid_on_complete_produces_no_errors(self, engine, tmp_path):
        tpl_path = self._write_yaml(tmp_path, "chain-tpl.yaml", """\
            id: chain-tpl
            name: "Chain Test Template"
            version: "1.0.0"
            description: "Test"
            author: "Test"
            on_complete:
              success:
                - template: downstream-pipeline
              failed:
                - template: alert-pipeline
                  input_map:
                    reason: "failed"
              max_chain_depth: 3
            phases:
              - id: phase_a
                name: Phase A
                model_tier: haiku
                thinking_level: off
                prompt_template: "Hello {input}"
        """)
        template = engine.load_template(tpl_path)
        errors = engine.validate_template(template)
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_on_complete_none_when_absent(self, engine, tmp_path):
        """When on_complete: is absent, PipelineTemplate.on_complete must be None."""
        tpl_path = tmp_path / "no-chain.yaml"
        tpl_path.write_text(_minimal_template_yaml())
        template = engine.load_template(tpl_path)
        assert template.on_complete is None

    def test_on_complete_is_parsed_correctly(self, engine, tmp_path):
        tpl_path = self._write_yaml(tmp_path, "chain-tpl2.yaml", """\
            id: chain-tpl2
            name: "Chain Test Template 2"
            version: "1.0.0"
            description: "Test"
            author: "Test"
            on_complete:
              success:
                - template: post-success-pipeline
                  input_map:
                    source: "parent"
              max_chain_depth: 7
            phases:
              - id: phase_a
                name: Phase A
                model_tier: haiku
                thinking_level: off
                prompt_template: "Hello {input}"
        """)
        template = engine.load_template(tpl_path)
        assert template.on_complete is not None
        assert isinstance(template.on_complete, OnCompleteConfig)
        assert len(template.on_complete.success) == 1
        assert template.on_complete.success[0].template == "post-success-pipeline"
        assert template.on_complete.success[0].input_map == {"source": "parent"}
        assert template.on_complete.max_chain_depth == 7

    def test_validate_template_flags_invalid_on_complete(self, engine):
        """Manually constructed template with corrupt on_complete should produce errors."""
        # Directly set on_complete to an invalid object bypassing load_template
        template = PipelineTemplate(
            id="test-invalid-chain",
            name="Test Invalid Chain",
            phases=[],
        )
        # Inject a broken on_complete (bypasses __post_init__ guard by direct attribute set)
        object.__setattr__(template, "on_complete", "not-an-on-complete-config")
        errors = engine.validate_template(template)
        assert any("on_complete" in e for e in errors), (
            f"Expected on_complete error, got: {errors}"
        )


# ---------------------------------------------------------------------------
# AC-08 & AC-09: Additional edge cases
# ---------------------------------------------------------------------------


class TestOnCompleteEdgeCases:
    def test_input_map_defaults_to_empty_dict(self):
        """AC-09: OnCompleteEntry.input_map defaults to {} when not specified."""
        entry = OnCompleteEntry(template="some-pipeline")
        assert entry.input_map == {}

    def test_on_complete_with_empty_success_and_failed(self):
        config = OnCompleteConfig()
        assert config.success == []
        assert config.failed == []
        assert config.max_chain_depth == 5

    def test_pipeline_template_on_complete_defaults_to_none(self):
        """AC-08: PipelineTemplate.on_complete is None by default."""
        template = PipelineTemplate(id="t", name="Test")
        assert template.on_complete is None

    def test_pipeline_template_accepts_on_complete(self):
        config = OnCompleteConfig(
            success=[OnCompleteEntry(template="child-pipeline")],
        )
        template = PipelineTemplate(id="t", name="Test", on_complete=config)
        assert template.on_complete is config
        assert len(template.on_complete.success) == 1

    def test_on_complete_config_normalises_none_lists(self):
        """None lists should be normalised to [] in __post_init__."""
        config = OnCompleteConfig(success=None, failed=None)  # type: ignore[arg-type]
        assert config.success == []
        assert config.failed == []

    def test_unknown_fields_in_on_complete_are_ignored(self):
        """Unknown keys in on_complete: block should be silently ignored."""
        raw = {
            "success": [{"template": "pipe-a"}],
            "unknown_future_field": "should-be-ignored",
        }
        config = _parse_on_complete_config(raw)
        assert config is not None
        assert len(config.success) == 1
