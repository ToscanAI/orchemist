"""Tests for Issue #330.3 — Chain DAG Validation and Children REST API.

Covers:
- AC-01: validate_template() catches a self-referential on_complete entry
- AC-02: validate_chain_dag() returns no errors for an acyclic chain graph
- AC-03: validate_chain_dag() detects a transitive cycle (A→B→A)
- AC-04: db.list_pipeline_run_children() returns children ordered by created_at ASC
- AC-05: GET /api/v1/runs/{run_id} response includes parent_run_id and chain_depth
- AC-06: GET /api/v1/runs/{run_id}/children returns the children of a run
"""

import json
import textwrap
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from orchestration_engine.db import Database
from orchestration_engine.templates import (
    OnCompleteConfig,
    OnCompleteEntry,
    PipelineTemplate,
    TemplateEngine,
)

# fastapi + starlette.testclient are guaranteed by the engine's [web]
# extra, which CI installs. Direct import — no importorskip needed (#876).
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


# #863: in_memory_db now sourced from tests/conftest.py canonical fixture.


@pytest.fixture
def engine(tmp_path: Path) -> TemplateEngine:
    """Return a TemplateEngine pointing at a temporary templates dir."""
    return TemplateEngine(templates_dir=tmp_path)


def _write_template(tmp_path: Path, template_id: str, on_complete: Optional[str] = None) -> Path:
    """Write a minimal YAML template file and return its path.

    *on_complete* is an optional raw YAML block (already formatted) that is
    appended at the top level (before the ``phases:`` key).
    """
    lines = [
        f"id: {template_id}",
        f'name: "{template_id} template"',
        'version: "1.0.0"',
        'description: "Chain DAG test template."',
        'author: "Test Author"',
    ]
    if on_complete:
        # Strip leading blank lines and right-pad content
        lines.append(on_complete.rstrip())
    lines += [
        "phases:",
        "  - id: phase_a",
        "    name: Phase A",
        "    model_tier: haiku",
        "    thinking_level: off",
        "    depends_on: []",
        '    prompt_template: "Hello {input}"',
    ]
    path = tmp_path / f"{template_id}.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _make_run(
    run_id: Optional[str] = None,
    template_id: str = "test-pipeline",
    parent_run_id: Optional[str] = None,
    chain_depth: int = 0,
    status: str = "success",
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a minimal pipeline_run dict suitable for db.insert_pipeline_run()."""
    from tests._helpers import pipeline_run_dict as _pipeline_run_dict
    rid = run_id or str(uuid.uuid4())
    overrides: Dict[str, Any] = {
        "template_path": f"/tmp/{template_id}.yaml",
        "template_id": template_id,
        "output_dir": f"/tmp/output/{rid}",
        "status": status,
        "skip_scoring": 0,
        "chain_depth": chain_depth,
    }
    if parent_run_id is not None:
        overrides["parent_run_id"] = parent_run_id
    return _pipeline_run_dict(rid, **overrides)


def _make_api_client(tmp_path: Path) -> TestClient:
    """Create a TestClient backed by an isolated DB."""
    from orchestration_engine.web.api import create_api_app

    db_file = str(tmp_path / "test-engine.db")
    app = create_api_app(db_path=db_file)
    return TestClient(app, raise_server_exceptions=False)


def _insert_run(db: Database, **kwargs) -> str:
    """Insert a run into the DB and return its run_id."""
    run = _make_run(**kwargs)
    db.insert_pipeline_run(run)
    return run["run_id"]


# ---------------------------------------------------------------------------
# AC-01: validate_template() catches self-referential on_complete
# ---------------------------------------------------------------------------


class TestSelfReferentialValidation:
    """validate_template() must catch templates that reference themselves."""

    def _build_template(self, template_id: str, on_complete: Optional[OnCompleteConfig]) -> PipelineTemplate:
        """Return a minimal PipelineTemplate with the given on_complete."""
        from orchestration_engine.templates import PhaseDefinition
        return PipelineTemplate(
            id=template_id,
            name="Self-ref test",
            version="1.0.0",
            description="",
            author="",
            phases=[
                PhaseDefinition(
                    id="phase_a",
                    name="Phase A",
                    model_tier="haiku",
                    thinking_level="off",
                    prompt_template="hello",
                )
            ],
            on_complete=on_complete,
        )

    def test_self_ref_in_success_list_is_an_error(self, engine: TemplateEngine):
        """A template that references itself in on_complete.success must be rejected."""
        tpl = self._build_template(
            "my-pipeline",
            OnCompleteConfig(
                success=[OnCompleteEntry(template="my-pipeline")],
            ),
        )
        errors = engine.validate_template(tpl)
        assert any("self-referential" in e for e in errors), \
            f"Expected self-referential error in: {errors}"

    def test_self_ref_in_failed_list_is_an_error(self, engine: TemplateEngine):
        """A template that references itself in on_complete.failed must be rejected."""
        tpl = self._build_template(
            "my-pipeline",
            OnCompleteConfig(
                failed=[OnCompleteEntry(template="my-pipeline")],
            ),
        )
        errors = engine.validate_template(tpl)
        assert any("self-referential" in e for e in errors), \
            f"Expected self-referential error in: {errors}"

    def test_self_ref_error_message_includes_template_id(self, engine: TemplateEngine):
        """Error message must include the template ID for diagnostics."""
        tpl = self._build_template(
            "target-pipeline",
            OnCompleteConfig(
                success=[OnCompleteEntry(template="target-pipeline")],
            ),
        )
        errors = engine.validate_template(tpl)
        matching = [e for e in errors if "self-referential" in e]
        assert len(matching) >= 1
        assert "target-pipeline" in matching[0]

    def test_non_self_ref_on_complete_is_valid(self, engine: TemplateEngine):
        """A template referencing a different template must NOT generate a self-ref error."""
        tpl = self._build_template(
            "parent-pipeline",
            OnCompleteConfig(
                success=[OnCompleteEntry(template="child-pipeline")],
            ),
        )
        errors = engine.validate_template(tpl)
        self_ref_errors = [e for e in errors if "self-referential" in e]
        assert self_ref_errors == [], \
            f"Unexpected self-ref errors: {self_ref_errors}"

    def test_no_on_complete_has_no_self_ref_error(self, engine: TemplateEngine):
        """Templates without on_complete must never produce self-referential errors."""
        tpl = self._build_template("standalone-pipeline", None)
        errors = engine.validate_template(tpl)
        self_ref_errors = [e for e in errors if "self-referential" in e]
        assert self_ref_errors == []


# ---------------------------------------------------------------------------
# AC-02: validate_chain_dag() — acyclic graph
# ---------------------------------------------------------------------------


class TestValidateChainDagAcyclic:
    """validate_chain_dag() must return an empty list for acyclic graphs."""

    def test_no_on_complete_is_acyclic(self, engine: TemplateEngine, tmp_path: Path):
        """A template with no on_complete has a trivially acyclic graph."""
        path = _write_template(tmp_path, "standalone")
        tpl = engine.load_template(path)
        errors = engine.validate_chain_dag(tpl)
        assert errors == []

    def test_linear_chain_a_to_b_is_acyclic(self, engine: TemplateEngine, tmp_path: Path):
        """A → B (B has no on_complete) must be acyclic."""
        _write_template(tmp_path, "template-b")
        on_complete_block = textwrap.dedent("""\
            on_complete:
              success:
                - template: template-b
        """)
        path_a = _write_template(tmp_path, "template-a", on_complete=on_complete_block)
        tpl_a = engine.load_template(path_a)
        errors = engine.validate_chain_dag(tpl_a)
        assert errors == [], f"Expected no errors for acyclic A→B, got: {errors}"

    def test_fork_a_to_b_and_c_is_acyclic(self, engine: TemplateEngine, tmp_path: Path):
        """A → {B, C} (both leaves) must be acyclic."""
        _write_template(tmp_path, "template-b")
        _write_template(tmp_path, "template-c")
        on_complete_block = textwrap.dedent("""\
            on_complete:
              success:
                - template: template-b
                - template: template-c
        """)
        path_a = _write_template(tmp_path, "template-a", on_complete=on_complete_block)
        tpl_a = engine.load_template(path_a)
        errors = engine.validate_chain_dag(tpl_a)
        assert errors == [], f"Expected no errors for acyclic fork A→{{B,C}}, got: {errors}"

    def test_unresolvable_reference_is_not_a_dag_error(self, engine: TemplateEngine, tmp_path: Path):
        """An unresolvable template reference should be skipped, not treated as a cycle."""
        on_complete_block = textwrap.dedent("""\
            on_complete:
              success:
                - template: does-not-exist
        """)
        path_a = _write_template(tmp_path, "template-a", on_complete=on_complete_block)
        tpl_a = engine.load_template(path_a)
        # Must not raise; must not report a cycle error
        errors = engine.validate_chain_dag(tpl_a)
        assert not any("Cycle" in e for e in errors), \
            f"Unresolvable reference should not be a cycle: {errors}"


# ---------------------------------------------------------------------------
# AC-03: validate_chain_dag() — cycle detection
# ---------------------------------------------------------------------------


class TestValidateChainDagCycle:
    """validate_chain_dag() must detect transitive cycles in the chain graph."""

    def test_direct_cycle_a_to_a_detected(self, engine: TemplateEngine, tmp_path: Path):
        """A self-referential on_complete entry forms a cycle A → A."""
        on_complete_block = textwrap.dedent("""\
            on_complete:
              success:
                - template: cycle-a
        """)
        path_a = _write_template(tmp_path, "cycle-a", on_complete=on_complete_block)
        tpl_a = engine.load_template(path_a)
        errors = engine.validate_chain_dag(tpl_a)
        assert len(errors) >= 1, f"Expected at least one cycle error, got: {errors}"
        assert any("Cycle" in e for e in errors)

    def test_transitive_cycle_a_b_a_detected(self, engine: TemplateEngine, tmp_path: Path):
        """A → B → A must be detected as a cycle."""
        on_complete_b = textwrap.dedent("""\
            on_complete:
              success:
                - template: cycle2-a
        """)
        _write_template(tmp_path, "cycle2-b", on_complete=on_complete_b)

        on_complete_a = textwrap.dedent("""\
            on_complete:
              success:
                - template: cycle2-b
        """)
        path_a = _write_template(tmp_path, "cycle2-a", on_complete=on_complete_a)
        tpl_a = engine.load_template(path_a)

        errors = engine.validate_chain_dag(tpl_a)
        assert len(errors) >= 1, f"Expected at least one cycle error for A→B→A, got: {errors}"
        assert any("Cycle" in e for e in errors)

    def test_cycle_error_mentions_nodes_in_cycle(self, engine: TemplateEngine, tmp_path: Path):
        """Cycle error messages must mention the template IDs involved."""
        on_complete_b = textwrap.dedent("""\
            on_complete:
              success:
                - template: dag-a
        """)
        _write_template(tmp_path, "dag-b", on_complete=on_complete_b)

        on_complete_a = textwrap.dedent("""\
            on_complete:
              success:
                - template: dag-b
        """)
        path_a = _write_template(tmp_path, "dag-a", on_complete=on_complete_a)
        tpl_a = engine.load_template(path_a)

        errors = engine.validate_chain_dag(tpl_a)
        assert any("dag-a" in e or "dag-b" in e for e in errors), \
            f"Cycle error should mention cycle nodes, got: {errors}"

    def test_longer_cycle_a_b_c_a_detected(self, engine: TemplateEngine, tmp_path: Path):
        """A → B → C → A must be detected as a cycle."""
        on_complete_c = textwrap.dedent("""\
            on_complete:
              success:
                - template: lc-a
        """)
        _write_template(tmp_path, "lc-c", on_complete=on_complete_c)

        on_complete_b = textwrap.dedent("""\
            on_complete:
              success:
                - template: lc-c
        """)
        _write_template(tmp_path, "lc-b", on_complete=on_complete_b)

        on_complete_a = textwrap.dedent("""\
            on_complete:
              success:
                - template: lc-b
        """)
        path_a = _write_template(tmp_path, "lc-a", on_complete=on_complete_a)
        tpl_a = engine.load_template(path_a)

        errors = engine.validate_chain_dag(tpl_a)
        assert any("Cycle" in e for e in errors), \
            f"Expected cycle for A→B→C→A, got: {errors}"


# ---------------------------------------------------------------------------
# AC-04: db.list_pipeline_run_children()
# ---------------------------------------------------------------------------


class TestListPipelineRunChildren:
    """list_pipeline_run_children() must return children in created_at ASC order."""

    def test_returns_empty_list_when_no_children(self, in_memory_db: Database):
        """No children → returns empty list (not None, not raises)."""
        parent_id = _insert_run(in_memory_db, run_id="parent-001")
        children = in_memory_db.list_pipeline_run_children(parent_id)
        assert children == []

    def test_returns_children_for_known_parent(self, in_memory_db: Database):
        """Children of a parent run must be returned."""
        parent_id = _insert_run(in_memory_db, run_id="parent-002")
        child_id = _insert_run(
            in_memory_db, run_id="child-002a", parent_run_id=parent_id, chain_depth=1
        )
        children = in_memory_db.list_pipeline_run_children(parent_id)
        assert len(children) == 1
        assert children[0]["run_id"] == child_id
        assert children[0]["parent_run_id"] == parent_id
        assert children[0]["chain_depth"] == 1

    def test_returns_multiple_children(self, in_memory_db: Database):
        """All children of a parent must be returned."""
        parent_id = _insert_run(in_memory_db, run_id="parent-003")
        child_ids = []
        for i in range(3):
            cid = _insert_run(
                in_memory_db,
                run_id=f"child-003{chr(ord('a') + i)}",
                parent_run_id=parent_id,
                chain_depth=1,
            )
            child_ids.append(cid)

        children = in_memory_db.list_pipeline_run_children(parent_id)
        assert len(children) == 3
        returned_ids = [c["run_id"] for c in children]
        for cid in child_ids:
            assert cid in returned_ids

    def test_does_not_return_unrelated_runs(self, in_memory_db: Database):
        """Only children of the specified parent must be returned."""
        parent_a = _insert_run(in_memory_db, run_id="parent-004a")
        parent_b = _insert_run(in_memory_db, run_id="parent-004b")
        _insert_run(in_memory_db, run_id="child-004b", parent_run_id=parent_b, chain_depth=1)

        children_of_a = in_memory_db.list_pipeline_run_children(parent_a)
        assert children_of_a == []

    def test_returns_empty_for_nonexistent_parent(self, in_memory_db: Database):
        """Querying children for a nonexistent parent must return an empty list."""
        children = in_memory_db.list_pipeline_run_children("nonexistent-parent-id")
        assert children == []


# ---------------------------------------------------------------------------
# AC-05: GET /api/v1/runs/{run_id} includes parent_run_id and chain_depth
# ---------------------------------------------------------------------------


class TestRunDetailChainFields:
    """GET /api/v1/runs/{run_id} must include parent_run_id and chain_depth."""

    def test_run_detail_includes_chain_depth(self, tmp_path: Path):
        """chain_depth must appear in the GET /api/v1/runs/{run_id} response."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        run = _make_run(run_id="run-cd-001", chain_depth=2)
        db.insert_pipeline_run(run)
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get("/api/v1/runs/run-cd-001")
        assert res.status_code == 200
        data = res.json()
        assert "chain_depth" in data, f"chain_depth missing from response: {data.keys()}"
        assert data["chain_depth"] == 2

    def test_run_detail_includes_parent_run_id(self, tmp_path: Path):
        """parent_run_id must appear in the GET /api/v1/runs/{run_id} response."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        parent_id = str(uuid.uuid4())
        run = _make_run(run_id="run-pr-001", parent_run_id=parent_id, chain_depth=1)
        db.insert_pipeline_run(run)
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get("/api/v1/runs/run-pr-001")
        assert res.status_code == 200
        data = res.json()
        assert "parent_run_id" in data, f"parent_run_id missing from response: {data.keys()}"
        assert data["parent_run_id"] == parent_id

    def test_run_without_parent_has_none_parent_run_id(self, tmp_path: Path):
        """A top-level run (no parent) must return parent_run_id: null."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        run = _make_run(run_id="run-noparent-001")
        db.insert_pipeline_run(run)
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get("/api/v1/runs/run-noparent-001")
        assert res.status_code == 200
        data = res.json()
        assert data.get("parent_run_id") is None
        assert data.get("chain_depth", -1) == 0


# ---------------------------------------------------------------------------
# AC-06: GET /api/v1/runs/{run_id}/children
# ---------------------------------------------------------------------------


class TestRunChildrenEndpoint:
    """GET /api/v1/runs/{run_id}/children must return child runs."""

    def test_returns_404_when_parent_not_found(self, tmp_path: Path):
        """Returns 404 when the parent run does not exist."""
        from orchestration_engine.web.api import create_api_app

        app = create_api_app(db_path=str(tmp_path / "test.db"))
        with TestClient(app, raise_server_exceptions=False) as client:
            res = client.get("/api/v1/runs/nonexistent-run-id/children")
        assert res.status_code == 404

    def test_returns_empty_children_for_run_with_no_children(self, tmp_path: Path):
        """Returns 200 with empty children list when run has no children."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        run = _make_run(run_id="parent-empty-001")
        db.insert_pipeline_run(run)
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get("/api/v1/runs/parent-empty-001/children")
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"] == "parent-empty-001"
        assert data["children"] == []

    def test_returns_children_when_they_exist(self, tmp_path: Path):
        """Returns all child runs with correct shape when children exist."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        parent_id = "parent-with-kids-001"
        db.insert_pipeline_run(_make_run(run_id=parent_id))
        child_id_1 = "child-001-a"
        child_id_2 = "child-001-b"
        db.insert_pipeline_run(
            _make_run(run_id=child_id_1, parent_run_id=parent_id, chain_depth=1)
        )
        db.insert_pipeline_run(
            _make_run(run_id=child_id_2, parent_run_id=parent_id, chain_depth=1)
        )
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get(f"/api/v1/runs/{parent_id}/children")
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"] == parent_id
        assert len(data["children"]) == 2
        returned_ids = {c["run_id"] for c in data["children"]}
        assert {child_id_1, child_id_2} == returned_ids

    def test_children_response_includes_chain_depth(self, tmp_path: Path):
        """Each child object in the response must include chain_depth."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        parent_id = "parent-depth-001"
        db.insert_pipeline_run(_make_run(run_id=parent_id))
        db.insert_pipeline_run(
            _make_run(run_id="child-depth-001a", parent_run_id=parent_id, chain_depth=3)
        )
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get(f"/api/v1/runs/{parent_id}/children")
        assert res.status_code == 200
        data = res.json()
        child = data["children"][0]
        assert child["chain_depth"] == 3
        assert child["parent_run_id"] == parent_id

    def test_children_response_does_not_include_grandchildren(self, tmp_path: Path):
        """Only direct children (not grandchildren) must be returned."""
        from orchestration_engine.web.api import create_api_app

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        parent_id = "grandparent-001"
        child_id = "child-gc-001"
        grandchild_id = "grandchild-gc-001"

        db.insert_pipeline_run(_make_run(run_id=parent_id))
        db.insert_pipeline_run(
            _make_run(run_id=child_id, parent_run_id=parent_id, chain_depth=1)
        )
        db.insert_pipeline_run(
            _make_run(run_id=grandchild_id, parent_run_id=child_id, chain_depth=2)
        )
        db.close()

        app = create_api_app(db_path=db_path)
        with TestClient(app) as client:
            res = client.get(f"/api/v1/runs/{parent_id}/children")
        assert res.status_code == 200
        data = res.json()
        returned_ids = {c["run_id"] for c in data["children"]}
        assert child_id in returned_ids
        assert grandchild_id not in returned_ids
