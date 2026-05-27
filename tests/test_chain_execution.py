"""Tests for Issue #330.2 — Chain Execution Engine.

Covers acceptance criteria:
- AC-01: interpolate_input_map replaces {{run_id}}, {{output_dir}}, {{status}}
- AC-02: interpolate_input_map resolves dotted paths ({{final_output.key}})
- AC-03: Unknown placeholders are left verbatim (not silently dropped)
- AC-04: Non-string values in input_map pass through unchanged
- AC-05: evaluate_on_complete returns [] when template.on_complete is None
- AC-06: evaluate_on_complete selects success entries on final_status='success'
- AC-07: evaluate_on_complete selects failed entries on final_status='failed'
- AC-08: evaluate_on_complete enforces max_chain_depth (returns [] when at limit)
- AC-09: evaluate_on_complete sets child chain_depth = parent_depth + 1
- AC-10: evaluate_on_complete interpolates input_map with parent context
- AC-11: spawn_chain_runs inserts DB records with correct parent_run_id + chain_depth
- AC-12: spawn_chain_runs returns spawned run IDs
- AC-13: spawn_chain_runs is non-fatal when template resolution fails
- AC-14: spawn_chain_runs uses Popen with start_new_session=True
- AC-15: MAX_ALLOWED_CHAIN_DEPTH hard cap is enforced regardless of template config
- AC-16: evaluate_on_complete maps scoring_failed to failed entries
- AC-17: Empty input_map in OnCompleteEntry results in empty interpolated map
- AC-18: interpolate_input_map with empty input_map returns empty dict
"""

import json
import textwrap
import uuid
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.chains import (
    MAX_ALLOWED_CHAIN_DEPTH,
    _make_child_output_dir,
    _resolve_dotted,
    _safe_parse_json,
    evaluate_on_complete,
    interpolate_input_map,
    spawn_chain_runs,
)
from orchestration_engine.db import Database
from orchestration_engine.templates import (
    OnCompleteConfig,
    OnCompleteEntry,
    PipelineTemplate,
    TemplateEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# #863: in_memory_db now sourced from tests/conftest.py canonical fixture.


@pytest.fixture
def minimal_run() -> Dict[str, Any]:
    """Return a minimal parent pipeline run dict (as returned by db.get_pipeline_run)."""
    return {
        "run_id": "parent-run-abc123",
        "template_path": "/tmp/fake-template.yaml",
        "template_id": "fake-pipeline",
        "input_json": json.dumps({"topic": "hello", "lang": "en"}),
        "mode": "dry-run",
        "output_dir": "/tmp/orch-output/parent-run-abc123",
        "status": "success",
        "gateway_url": None,
        "skip_scoring": 0,
        "chain_depth": 0,
        "parent_run_id": None,
    }


@pytest.fixture
def on_complete_success_only() -> OnCompleteConfig:
    """OnCompleteConfig with a single success entry."""
    return OnCompleteConfig(
        success=[
            OnCompleteEntry(
                template="notify-pipeline",
                input_map={"source_run": "{{run_id}}", "status": "{{status}}"},
            )
        ],
        failed=[],
        max_chain_depth=3,
    )


@pytest.fixture
def on_complete_both() -> OnCompleteConfig:
    """OnCompleteConfig with both success and failed entries."""
    return OnCompleteConfig(
        success=[OnCompleteEntry(template="success-pipeline")],
        failed=[OnCompleteEntry(template="failure-pipeline")],
        max_chain_depth=5,
    )


@pytest.fixture
def template_with_on_complete(on_complete_success_only: OnCompleteConfig) -> PipelineTemplate:
    """PipelineTemplate with on_complete configured."""
    return PipelineTemplate(
        id="parent-pipeline",
        name="Parent Pipeline",
        on_complete=on_complete_success_only,
    )


@pytest.fixture
def template_no_on_complete() -> PipelineTemplate:
    """PipelineTemplate without any on_complete block."""
    return PipelineTemplate(
        id="solo-pipeline",
        name="Solo Pipeline",
        on_complete=None,
    )


def _write_minimal_template(tmp_path: Path, template_name: str = "child-pipeline") -> Path:
    """Write a minimal valid template YAML and return its path."""
    content = textwrap.dedent(f"""\
        id: {template_name}
        name: "Child Pipeline"
        version: "1.0.0"
        description: "Minimal child pipeline for chain tests."
        author: "Test"
        phases:
          - id: phase_a
            name: Phase A
            model_tier: haiku
            thinking_level: off
            prompt_template: "Hello {{{{input}}}}"
    """)
    p = tmp_path / f"{template_name}.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Unit tests: _resolve_dotted
# ---------------------------------------------------------------------------


class TestResolveDotted:
    def test_top_level_key(self):
        assert _resolve_dotted("run_id", {"run_id": "abc"}) == "abc"

    def test_nested_key(self):
        ctx = {"final_output": {"summary": "All good"}}
        assert _resolve_dotted("final_output.summary", ctx) == "All good"

    def test_deeply_nested(self):
        ctx = {"a": {"b": {"c": "deep"}}}
        assert _resolve_dotted("a.b.c", ctx) == "deep"

    def test_missing_key_returns_none(self):
        assert _resolve_dotted("missing", {"other": "x"}) is None

    def test_missing_nested_returns_none(self):
        ctx = {"final_output": {}}
        assert _resolve_dotted("final_output.missing_key", ctx) is None

    def test_non_dict_intermediate_returns_none(self):
        ctx = {"final_output": "just a string"}
        assert _resolve_dotted("final_output.key", ctx) is None

    def test_value_coerced_to_string(self):
        ctx = {"count": 42}
        result = _resolve_dotted("count", ctx)
        assert result == "42"


# ---------------------------------------------------------------------------
# AC-01, AC-02, AC-03, AC-04: interpolate_input_map
# ---------------------------------------------------------------------------


class TestInterpolateInputMap:
    """AC-01 through AC-04: placeholder interpolation."""

    def test_replaces_run_id_placeholder(self):
        """AC-01: {{run_id}} is replaced with the context run_id."""
        result = interpolate_input_map(
            {"child_run_ref": "{{run_id}}"},
            {"run_id": "abc-123"},
        )
        assert result["child_run_ref"] == "abc-123"

    def test_replaces_output_dir_placeholder(self):
        """AC-01: {{output_dir}} is replaced."""
        result = interpolate_input_map(
            {"dir": "{{output_dir}}/child"},
            {"output_dir": "/tmp/parent-out"},
        )
        assert result["dir"] == "/tmp/parent-out/child"

    def test_replaces_status_placeholder(self):
        """AC-01: {{status}} is replaced with the final status string."""
        result = interpolate_input_map(
            {"parent_status": "{{status}}"},
            {"status": "success"},
        )
        assert result["parent_status"] == "success"

    def test_resolves_dotted_path(self):
        """AC-02: {{final_output.key}} resolves via dotted lookup."""
        ctx = {
            "final_output": {"pr_url": "https://github.com/pr/1"},
        }
        result = interpolate_input_map({"pr": "{{final_output.pr_url}}"}, ctx)
        assert result["pr"] == "https://github.com/pr/1"

    def test_multiple_placeholders_in_one_value(self):
        """Multiple {{...}} tokens in one string value are all replaced."""
        result = interpolate_input_map(
            {"msg": "Run {{run_id}} finished with {{status}}"},
            {"run_id": "xyz", "status": "success"},
        )
        assert result["msg"] == "Run xyz finished with success"

    def test_unknown_placeholder_left_verbatim(self):
        """AC-03: Unknown placeholders are left as {{token}}, not dropped."""
        result = interpolate_input_map(
            {"val": "{{unknown_key}}"},
            {"run_id": "abc"},
        )
        assert result["val"] == "{{unknown_key}}"

    def test_non_string_value_passes_through_unchanged(self):
        """AC-04: Integer/bool/list/dict values are not modified."""
        result = interpolate_input_map(
            {"count": 42, "flag": True, "items": [1, 2], "meta": {"x": 1}},
            {"run_id": "abc"},
        )
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["items"] == [1, 2]
        assert result["meta"] == {"x": 1}

    def test_empty_input_map_returns_empty_dict(self):
        """AC-18: Empty input_map produces an empty result dict."""
        result = interpolate_input_map({}, {"run_id": "abc", "status": "success"})
        assert result == {}

    def test_value_without_placeholders_unchanged(self):
        """Strings without {{}} tokens are returned as-is."""
        result = interpolate_input_map(
            {"key": "plain value"},
            {"run_id": "abc"},
        )
        assert result["key"] == "plain value"


# ---------------------------------------------------------------------------
# AC-05 through AC-10: evaluate_on_complete
# ---------------------------------------------------------------------------


class TestEvaluateOnComplete:
    """Acceptance tests for the evaluate_on_complete function."""

    def test_returns_empty_when_on_complete_is_none(self, template_no_on_complete, minimal_run):
        """AC-05: Returns [] when template.on_complete is None."""
        result = evaluate_on_complete(
            template=template_no_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert result == []

    def test_selects_success_entries_on_success(self, template_with_on_complete, minimal_run):
        """AC-06: Success entries are selected when final_status='success'."""
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(results) == 1
        assert results[0]["template_name"] == "notify-pipeline"

    def test_selects_failed_entries_on_failed(self, minimal_run):
        """AC-07: Failed entries are selected when final_status='failed'."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[OnCompleteEntry(template="success-pipeline")],
                failed=[OnCompleteEntry(template="failure-pipeline")],
                max_chain_depth=5,
            ),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="failed",
        )
        assert len(results) == 1
        assert results[0]["template_name"] == "failure-pipeline"

    def test_selects_failed_entries_on_scoring_failed(self, minimal_run):
        """AC-16: scoring_failed maps to the failed entries list."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[OnCompleteEntry(template="success-pipeline")],
                failed=[OnCompleteEntry(template="scoring-alert-pipeline")],
                max_chain_depth=5,
            ),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="scoring_failed",
        )
        assert len(results) == 1
        assert results[0]["template_name"] == "scoring-alert-pipeline"

    def test_depth_limit_enforced_at_max(self, template_with_on_complete, minimal_run):
        """AC-08: Returns [] when parent depth == max_chain_depth."""
        # template_with_on_complete has max_chain_depth=3
        minimal_run["chain_depth"] = 3  # already at limit
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results == []

    def test_depth_limit_enforced_above_max(self, template_with_on_complete, minimal_run):
        """AC-08: Returns [] when parent depth > max_chain_depth."""
        minimal_run["chain_depth"] = 10
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results == []

    def test_depth_not_exceeded_below_max(self, template_with_on_complete, minimal_run):
        """AC-08: Children ARE spawned when parent depth < max_chain_depth."""
        minimal_run["chain_depth"] = 2  # below max_chain_depth=3
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(results) == 1

    def test_child_depth_is_parent_plus_one(self, template_with_on_complete, minimal_run):
        """AC-09: child chain_depth = parent chain_depth + 1."""
        minimal_run["chain_depth"] = 1
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(results) == 1
        assert results[0]["chain_depth"] == 2

    def test_input_map_is_interpolated(self, minimal_run):
        """AC-10: input_map values are interpolated with parent context."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[
                    OnCompleteEntry(
                        template="notify-pipeline",
                        input_map={
                            "parent_id": "{{run_id}}",
                            "parent_status": "{{status}}",
                            "outdir": "{{output_dir}}",
                        },
                    )
                ],
                max_chain_depth=5,
            ),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(results) == 1
        im = results[0]["input_map"]
        assert im["parent_id"] == "parent-run-abc123"
        assert im["parent_status"] == "success"
        assert im["outdir"] == "/tmp/orch-output/parent-run-abc123"

    def test_final_output_interpolated_in_input_map(self, minimal_run):
        """AC-10: {{final_output.key}} tokens are resolved from result."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[
                    OnCompleteEntry(
                        template="report-pipeline",
                        input_map={"pr_link": "{{final_output.pr_url}}"},
                    )
                ],
                max_chain_depth=5,
            ),
        )
        result = {
            "final_output": {
                "result": {"pr_url": "https://github.com/pr/99"},
            }
        }
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result=result,
            final_status="success",
        )
        assert results[0]["input_map"]["pr_link"] == "https://github.com/pr/99"

    def test_empty_success_list_returns_empty(self, minimal_run):
        """AC-06: When success list is empty, returns []."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(success=[], failed=[], max_chain_depth=5),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results == []

    def test_empty_input_map_entry_produces_empty_interpolated_map(self, minimal_run):
        """AC-17: OnCompleteEntry with empty input_map → empty interpolated map."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[OnCompleteEntry(template="p1", input_map={})],
                max_chain_depth=5,
            ),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results[0]["input_map"] == {}

    def test_multiple_success_entries_all_returned(self, minimal_run):
        """Multiple entries in success list → multiple child configs returned."""
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[
                    OnCompleteEntry(template="pipeline-a"),
                    OnCompleteEntry(template="pipeline-b"),
                ],
                max_chain_depth=5,
            ),
        )
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(results) == 2
        assert results[0]["template_name"] == "pipeline-a"
        assert results[1]["template_name"] == "pipeline-b"

    def test_parent_run_id_set_in_child_config(self, template_with_on_complete, minimal_run):
        """evaluate_on_complete sets parent_run_id in each child config."""
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results[0]["parent_run_id"] == "parent-run-abc123"

    def test_mode_inherited_from_parent(self, template_with_on_complete, minimal_run):
        """Child configs inherit 'mode' from parent run."""
        minimal_run["mode"] = "openclaw"
        results = evaluate_on_complete(
            template=template_with_on_complete,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results[0]["mode"] == "openclaw"

    def test_max_allowed_chain_depth_hard_cap(self, minimal_run):
        """AC-15: MAX_ALLOWED_CHAIN_DEPTH is enforced even if template sets higher."""
        high_depth_config = OnCompleteConfig(
            success=[OnCompleteEntry(template="child")],
            max_chain_depth=MAX_ALLOWED_CHAIN_DEPTH + 10,  # exceeds hard cap
        )
        template = PipelineTemplate(
            id="parent", name="Parent", on_complete=high_depth_config
        )
        # Set chain_depth to exactly MAX_ALLOWED_CHAIN_DEPTH
        minimal_run["chain_depth"] = MAX_ALLOWED_CHAIN_DEPTH
        results = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert results == []


# ---------------------------------------------------------------------------
# AC-11, AC-12, AC-13, AC-14: spawn_chain_runs
# ---------------------------------------------------------------------------


class TestSpawnChainRuns:
    """Acceptance tests for spawn_chain_runs."""

    def test_inserts_run_with_correct_parent_and_depth(self, in_memory_db, tmp_path):
        """AC-11: Inserted child run has correct parent_run_id and chain_depth."""
        template_path = _write_minimal_template(tmp_path, "child-pipeline")

        child_configs = [
            {
                "template_name": str(template_path),
                "input_map": {"key": "value"},
                "chain_depth": 1,
                "parent_run_id": "parent-abc",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            }
        ]

        with patch("orchestration_engine.chains._spawn_daemon") as mock_spawn:
            spawned = spawn_chain_runs(
                child_configs=child_configs,
                db=in_memory_db,
                db_path="/tmp/test.db",
                parent_run_id="parent-abc",
            )

        assert len(spawned) == 1
        child_run_id = spawned[0]
        run = in_memory_db.get_pipeline_run(child_run_id)
        assert run is not None
        assert run["parent_run_id"] == "parent-abc"
        assert run["chain_depth"] == 1

    def test_input_map_stored_as_input_json(self, in_memory_db, tmp_path):
        """Child run's input_json matches the resolved input_map."""
        template_path = _write_minimal_template(tmp_path, "child-pipeline")
        child_configs = [
            {
                "template_name": str(template_path),
                "input_map": {"foo": "bar", "x": "42"},
                "chain_depth": 1,
                "parent_run_id": "p-001",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            }
        ]
        with patch("orchestration_engine.chains._spawn_daemon"):
            spawned = spawn_chain_runs(child_configs, in_memory_db, "/tmp/db", "p-001")

        run = in_memory_db.get_pipeline_run(spawned[0])
        assert run is not None
        parsed_input = json.loads(run["input_json"])
        assert parsed_input == {"foo": "bar", "x": "42"}

    def test_returns_spawned_run_ids(self, in_memory_db, tmp_path):
        """AC-12: Returns list of successfully inserted run IDs."""
        tpl1 = _write_minimal_template(tmp_path, "child-a")
        tpl2 = _write_minimal_template(tmp_path, "child-b")

        child_configs = [
            {
                "template_name": str(tpl1),
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-001",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            },
            {
                "template_name": str(tpl2),
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-001",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            },
        ]

        with patch("orchestration_engine.chains._spawn_daemon"):
            spawned = spawn_chain_runs(child_configs, in_memory_db, "/tmp/db", "p-001")

        assert len(spawned) == 2
        # Each returned ID must correspond to an actual DB record
        for run_id in spawned:
            assert in_memory_db.get_pipeline_run(run_id) is not None

    def test_non_fatal_when_template_not_found(self, in_memory_db):
        """AC-13: Missing template is logged and skipped; other children still spawn."""
        child_configs = [
            {
                "template_name": "nonexistent-template-xyz",
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-001",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            }
        ]
        # Should not raise — just return empty list
        spawned = spawn_chain_runs(child_configs, in_memory_db, "/tmp/db", "p-001")
        assert spawned == []

    def test_non_fatal_partial_failure(self, in_memory_db, tmp_path):
        """AC-13: A bad config is skipped; valid configs still produce child runs."""
        good_tpl = _write_minimal_template(tmp_path, "good-child")
        child_configs = [
            {
                "template_name": "DOES-NOT-EXIST",
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-multi",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            },
            {
                "template_name": str(good_tpl),
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-multi",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            },
        ]
        with patch("orchestration_engine.chains._spawn_daemon"):
            spawned = spawn_chain_runs(child_configs, in_memory_db, "/tmp/db", "p-multi")

        assert len(spawned) == 1

    def test_daemon_spawned_with_start_new_session(self, in_memory_db, tmp_path):
        """AC-14: Daemon is spawned via Popen with start_new_session=True."""
        template_path = _write_minimal_template(tmp_path, "child-pipeline")
        child_configs = [
            {
                "template_name": str(template_path),
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-sess",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            }
        ]

        with patch("orchestration_engine.chains.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            spawned = spawn_chain_runs(
                child_configs, in_memory_db, "/tmp/db", "p-sess"
            )

        assert mock_popen.called
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True

    def test_spawn_failure_marks_child_as_failed(self, in_memory_db, tmp_path):
        """When Popen raises, child run is updated to failed status."""
        template_path = _write_minimal_template(tmp_path, "child-pipeline")
        child_configs = [
            {
                "template_name": str(template_path),
                "input_map": {},
                "chain_depth": 1,
                "parent_run_id": "p-fail",
                "mode": "dry-run",
                "gateway_url": None,
                "skip_scoring": False,
            }
        ]

        with patch(
            "orchestration_engine.chains.subprocess.Popen",
            side_effect=OSError("cannot fork"),
        ):
            spawned = spawn_chain_runs(
                child_configs, in_memory_db, "/tmp/db", "p-fail"
            )

        # Spawn failed → not returned in list
        assert spawned == []

        # The DB record should still exist (was inserted) but marked failed
        # Find the child run by querying for runs with parent_run_id
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
            cursor = conn.execute(
                "SELECT run_id, status FROM pipeline_runs WHERE parent_run_id = ?",
                ("p-fail",),
            )
            rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "failed"

    def test_empty_child_configs_returns_empty_list(self, in_memory_db):
        """Calling spawn_chain_runs with empty list returns [] immediately."""
        with patch("orchestration_engine.chains._spawn_daemon") as mock_spawn:
            result = spawn_chain_runs([], in_memory_db, "/tmp/db", "p-empty")
        assert result == []
        mock_spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_make_child_output_dir_format(self):
        parent = "parent-run-abc123xyz"
        child = "child-run-def456uvw"
        path = _make_child_output_dir(parent_run_id=parent, child_run_id=child)
        # Should use first 8 chars of each ID
        assert "parent-r" in path  # parent[:8]
        assert "child-ru" in path  # child[:8]
        assert path.startswith("/tmp/orch-chains/")

    def test_safe_parse_json_valid(self):
        result = _safe_parse_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_safe_parse_json_invalid_returns_empty(self):
        result = _safe_parse_json("not json")
        assert result == {}

    def test_safe_parse_json_empty_returns_empty(self):
        assert _safe_parse_json("") == {}
        assert _safe_parse_json(None) == {}

    def test_safe_parse_json_non_dict_returns_empty(self):
        assert _safe_parse_json("[1, 2, 3]") == {}
        assert _safe_parse_json('"just a string"') == {}


# ---------------------------------------------------------------------------
# Integration-style: evaluate_on_complete → spawn_chain_runs with real DB
# ---------------------------------------------------------------------------


class TestChainIntegration:
    """Integration tests combining evaluate + spawn with a real in-memory DB."""

    def test_evaluate_and_spawn_end_to_end(self, in_memory_db, tmp_path, minimal_run):
        """Full pipeline: evaluate produces configs, spawn inserts + starts daemons."""
        template_path = _write_minimal_template(tmp_path, "notify-pipeline")

        # Patch TemplateEngine to resolve our local template
        template = PipelineTemplate(
            id="parent",
            name="Parent",
            on_complete=OnCompleteConfig(
                success=[
                    OnCompleteEntry(
                        template=str(template_path),
                        input_map={"parent": "{{run_id}}"},
                    )
                ],
                max_chain_depth=5,
            ),
        )

        child_configs = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert len(child_configs) == 1
        assert child_configs[0]["input_map"]["parent"] == "parent-run-abc123"

        with patch("orchestration_engine.chains._spawn_daemon") as mock_spawn:
            spawned = spawn_chain_runs(
                child_configs=child_configs,
                db=in_memory_db,
                db_path="/tmp/test.db",
                parent_run_id="parent-run-abc123",
            )

        assert len(spawned) == 1
        mock_spawn.assert_called_once()
        run = in_memory_db.get_pipeline_run(spawned[0])
        assert run["parent_run_id"] == "parent-run-abc123"
        assert run["chain_depth"] == 1

    def test_no_children_spawned_when_on_complete_absent(
        self, in_memory_db, minimal_run
    ):
        """When template has no on_complete, nothing is inserted to DB."""
        template = PipelineTemplate(id="solo", name="Solo", on_complete=None)
        child_configs = evaluate_on_complete(
            template=template,
            run=minimal_run,
            result={},
            final_status="success",
        )
        assert child_configs == []
        # No children in DB
        with in_memory_db._locked():
            conn = in_memory_db.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM pipeline_runs WHERE parent_run_id = ?",
                ("parent-run-abc123",),
            )
            count = cursor.fetchone()[0]
        assert count == 0
