"""Acceptance tests for Issue #704 template composition (extends + exclude_phases).

These tests are written ONLY from the behavioral contracts (Section A) in
``.orchemist/runs/704-92868/behavioral.md``. The implementation is NOT yet
visible to the test author. These tests become the immutable constraint for
the implementer.

Each test corresponds to one or more BC-N contracts. The mapping is in the
test's docstring.

The tests use an ISOLATED ``TemplateEngine(project_dir=tmp_path)`` per test so
that resolution does not depend on the user's ~/.orch/templates or the
bundled templates directory (unless the test intentionally exercises bundled).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from src.orchestration_engine.templates import (
    PipelineTemplate,
    TemplateEngine,
    TemplateNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_BASE_PHASE_FIELDS = {
    "name": "Phase",
    "model_tier": "sonnet",
    "thinking_level": "low",
    "prompt_template": "do work",
    "timeout_minutes": 30,
}


def _make_phase(
    pid: str,
    *,
    transitions: Optional[Dict[str, str]] = None,
    **overrides,
) -> Dict:
    """Build a minimal valid phase dict with optional overrides."""
    phase = {"id": pid, **_BASE_PHASE_FIELDS}
    phase.update(overrides)
    if transitions is not None:
        phase["transitions"] = transitions
    return phase


_SENTINEL = object()  # used to distinguish "not passed" from "explicitly None/empty"


def _write_template(
    path: Path,
    *,
    id: str,
    name: str = "T",
    version=_SENTINEL,
    description=_SENTINEL,
    author=_SENTINEL,
    extends: Optional[str] = None,
    exclude_phases: Optional[List] = None,
    phases: Optional[List[Dict]] = None,
    **extras,
) -> Path:
    """Write a minimal valid template YAML file. Returns the path.

    Only ``id`` and ``name`` are unconditionally written. ``version``,
    ``description``, and ``author`` are only written when the caller passes
    them — letting callers verify inheritance behaviour by deliberately
    omitting those keys from the child.
    """
    body: Dict = {"id": id, "name": name}
    if version is not _SENTINEL:
        body["version"] = version
    if description is not _SENTINEL:
        body["description"] = description
    if author is not _SENTINEL:
        body["author"] = author
    if extends is not None:
        body["extends"] = extends
    if exclude_phases is not None:
        body["exclude_phases"] = exclude_phases
    if phases is not None:
        body["phases"] = phases
    body.update(extras)
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return path


@pytest.fixture
def isolated_engine(tmp_path: Path) -> TemplateEngine:
    """A TemplateEngine that resolves only from tmp_path (and bundled
    fallback). Ensures tests don't pick up the user's ~/.orch/templates."""
    # Point user_dir at a non-existent subdir so it's effectively disabled.
    return TemplateEngine(
        project_dir=tmp_path,
        user_dir=tmp_path / "user-noexist",
    )


# ---------------------------------------------------------------------------
# BC-1: Basic extends — parent phases included
# ---------------------------------------------------------------------------


def test_bc1_basic_extends_inherits_parent_phase(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-1: extends loads parent + child correctly."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        name="Child",
        extends="parent",
    )

    template = isolated_engine.load_template(child_path)

    assert template.id == "child"
    assert template.name == "Child"
    assert len(template.phases) == 1
    assert template.phases[0].id == "p1"
    assert template.extends == "parent"
    assert template.excluded_phase_ids == []


# ---------------------------------------------------------------------------
# BC-2: Child phase override — field-level merge
# ---------------------------------------------------------------------------


def test_bc2_field_level_merge_inherits_unspecified(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-2: child overrides prompt_template; other fields inherit from parent."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[
            _make_phase(
                "p1",
                model_tier="sonnet",
                thinking_level="high",
                prompt_template="PARENT",
                max_iterations=5,
            )
        ],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        phases=[{"id": "p1", "prompt_template": "CHILD"}],
    )

    template = isolated_engine.load_template(child_path)

    phase = next(p for p in template.phases if p.id == "p1")
    assert phase.prompt_template == "CHILD"
    assert phase.model_tier == "sonnet"
    assert phase.thinking_level == "high"
    assert phase.max_iterations == 5


# ---------------------------------------------------------------------------
# BC-3: Child adds new phase — appended after parent
# ---------------------------------------------------------------------------


def test_bc3_child_adds_new_phase_appended(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-3: parent phases first, then child-only phases appended in declaration order."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        phases=[_make_phase("p2")],
    )

    template = isolated_engine.load_template(child_path)
    assert [p.id for p in template.phases] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# BC-4: exclude_phases drops named parent phases
# ---------------------------------------------------------------------------


def test_bc4_exclude_phases_drops_named(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-4: exclude_phases removes the listed parent phases."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1"), _make_phase("p2"), _make_phase("p3")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        exclude_phases=["p2"],
    )

    template = isolated_engine.load_template(child_path)
    assert [p.id for p in template.phases] == ["p1", "p3"]
    assert "p2" in template.excluded_phase_ids


# ---------------------------------------------------------------------------
# BC-5: exclude_phases listing non-existent phase — warning, no error
# ---------------------------------------------------------------------------


def test_bc5_exclude_phases_unmatched_warns(
    tmp_path: Path,
    isolated_engine: TemplateEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BC-5: excluding a phase id not in parent is a warning, not an error."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1"), _make_phase("p2")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        exclude_phases=["does_not_exist"],
    )

    with caplog.at_level(logging.WARNING):
        template = isolated_engine.load_template(child_path)  # should not raise

    assert len(template.phases) == 2
    warning_msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "does_not_exist" in warning_msgs


# ---------------------------------------------------------------------------
# BC-6: Circular extends — clear error (length-2 cycle)
# ---------------------------------------------------------------------------


def test_bc6_circular_extends_length_two(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-6: A extends B extends A raises ValueError mentioning both names."""
    a_path = _write_template(
        tmp_path / "a.yaml",
        id="a",
        extends="b",
    )
    _write_template(
        tmp_path / "b.yaml",
        id="b",
        extends="a",
    )

    with pytest.raises(ValueError) as exc_info:
        isolated_engine.load_template(a_path)

    msg = str(exc_info.value).lower()
    assert "circular extends" in msg
    assert "a" in msg
    assert "b" in msg


# ---------------------------------------------------------------------------
# BC-7: Non-existent parent — clear error
# ---------------------------------------------------------------------------


def test_bc7_non_existent_parent_errors(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-7: extends pointing at a missing template raises with a clear message."""
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="does_not_exist",
    )

    with pytest.raises((TemplateNotFoundError, ValueError, FileNotFoundError)) as exc_info:
        isolated_engine.load_template(child_path)

    assert "does_not_exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# BC-8: Multi-level inheritance — full chain resolved
# ---------------------------------------------------------------------------


def test_bc8_multi_level_inheritance(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-8: child extends parent extends grandparent — all phases combined in order."""
    _write_template(
        tmp_path / "grandparent.yaml",
        id="grandparent",
        phases=[_make_phase("g")],
    )
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        extends="grandparent",
        phases=[_make_phase("p")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        phases=[_make_phase("c")],
    )

    template = isolated_engine.load_template(child_path)
    assert [p.id for p in template.phases] == ["g", "p", "c"]


# ---------------------------------------------------------------------------
# BC-9: extends without phases — parent unchanged
# ---------------------------------------------------------------------------


def test_bc9_extends_without_phases_unchanged(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-9: child with no phases inherits parent's phases verbatim."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1"), _make_phase("p2")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        # No phases:
    )

    template = isolated_engine.load_template(child_path)
    assert len(template.phases) == 2
    assert [p.id for p in template.phases] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# BC-10: Transition to excluded phase — validation error
# ---------------------------------------------------------------------------


def test_bc10_transition_to_excluded_phase_validation_error(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-10: load succeeds; validate flags transitions pointing at excluded phases."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[
            _make_phase("p1", transitions={"success": "p2"}),
            _make_phase("p2"),
        ],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        exclude_phases=["p2"],
    )

    # load_template must SUCCEED
    template = isolated_engine.load_template(child_path)
    assert len(template.phases) == 1
    assert template.phases[0].id == "p1"

    # validate_template must report the dangling transition
    errors = isolated_engine.validate_template(template)
    assert len(errors) >= 1
    matched = [
        e for e in errors
        if "p1" in e and "p2" in e and ("transition" in e.lower() or "excluded" in e.lower())
    ]
    assert matched, f"No transition/excluded error mentioning p1 and p2 found in: {errors!r}"


# ---------------------------------------------------------------------------
# BC-11: Existing bundled standard template still loads — regression
# ---------------------------------------------------------------------------


def test_bc11_bundled_standard_pipeline_unchanged() -> None:
    """BC-11: coding-pipeline-standard.yaml still loads with 12 phase IDs."""
    bundled_root = Path(__file__).parent.parent / "templates"
    std_path = bundled_root / "coding-pipeline-standard.yaml"
    assert std_path.exists(), f"bundled standard template missing: {std_path}"

    template = TemplateEngine().load_template(std_path)
    expected_ids = [
        "existing_symbols_inventory",
        "spec",
        "behavioral",
        "spec_adversary",
        "postmortem_spec",
        "acceptance_test",
        "implement",
        "acceptance_run",
        "review",
        "fix",
        "postmortem_review",
        "test",
    ]
    assert [p.id for p in template.phases] == expected_ids


# ---------------------------------------------------------------------------
# BC-12: skip-spec post-refactor — 9 phases via extends
# ---------------------------------------------------------------------------


def test_bc12_skip_spec_post_refactor_has_nine_phases() -> None:
    """BC-12: coding-pipeline-skip-spec.yaml uses extends and has 9 merged phases.

    Per BC-3 the new child-only phases (`acceptance_test_adversary`,
    `verify_tests_integrity`) are appended AFTER the parent-derived phases.
    The pipeline executes via `transitions:` (state machine), not YAML order,
    so the literal index of the new phases is irrelevant — the only
    order-sensitive constraint is that `acceptance_test` remains at index 0
    (the engine uses `phases[0]` as the entry point).
    """
    bundled_root = Path(__file__).parent.parent / "templates"
    skip_path = bundled_root / "coding-pipeline-skip-spec.yaml"
    assert skip_path.exists(), f"bundled skip-spec template missing: {skip_path}"

    template = TemplateEngine().load_template(skip_path)
    expected_phase_ids = {
        "acceptance_test",
        "acceptance_test_adversary",
        "implement",
        "verify_tests_integrity",
        "acceptance_run",
        "review",
        "fix",
        "postmortem_review",
        "test",
    }
    actual_ids = [p.id for p in template.phases]
    assert set(actual_ids) == expected_phase_ids
    assert len(actual_ids) == 9
    # Entry point must remain acceptance_test (sequencer reads phases[0])
    assert actual_ids[0] == "acceptance_test"

    assert template.extends == "coding-pipeline-standard"
    assert set(template.excluded_phase_ids) == {
        "existing_symbols_inventory",
        "spec",
        "behavioral",
        "spec_adversary",
        "postmortem_spec",
    }

    errors = TemplateEngine().validate_template(template)
    assert errors == [], f"skip-spec validation errors after refactor: {errors!r}"


# ---------------------------------------------------------------------------
# BC-13: Drift lint still functions
# ---------------------------------------------------------------------------


def test_bc13_drift_lint_passes() -> None:
    """BC-13: scripts/check_template_sync.py exits 0 against bundled templates."""
    repo_root = Path(__file__).parent.parent
    script = repo_root / "scripts" / "check_template_sync.py"
    assert script.exists(), f"drift lint script missing: {script}"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"drift lint failed (rc={result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The "OK:" output line should mention strict + anchored phases
    assert "OK" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# BC-14: Top-level field inheritance (author, category, tags)
# ---------------------------------------------------------------------------


def test_bc14_top_level_field_inheritance(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-14: child inherits parent's author/category/tags when omitted."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        author="Toscan",
        category="code",
        tags=["code", "test"],
        phases=[_make_phase("p1")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        # author / category / tags intentionally omitted
        extends="parent",
    )

    template = isolated_engine.load_template(child_path)
    assert template.author == "Toscan"
    assert template.category == "code"
    assert template.tags == ["code", "test"]


# ---------------------------------------------------------------------------
# BC-15: extends wrong type — clear error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [123, ["a", "b"], {"foo": "bar"}, ""])
def test_bc15_extends_wrong_type_errors(
    tmp_path: Path, isolated_engine: TemplateEngine, bad_value
) -> None:
    """BC-15: extends not a non-empty string raises ValueError."""
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends=bad_value,
    )

    with pytest.raises((ValueError, TypeError)) as exc_info:
        isolated_engine.load_template(child_path)

    assert "extends" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# BC-16: exclude_phases wrong type — clear error
# ---------------------------------------------------------------------------


def test_bc16_exclude_phases_wrong_type_errors(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-16: exclude_phases must be a list — string raises ValueError."""
    _write_template(
        tmp_path / "parent.yaml",
        id="parent",
        phases=[_make_phase("p1")],
    )
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="child",
        extends="parent",
        exclude_phases="p1",  # string, not list
    )

    with pytest.raises((ValueError, TypeError)) as exc_info:
        isolated_engine.load_template(child_path)

    assert "exclude_phases" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# BC-17: exclude_phases without extends — warning, no error
# ---------------------------------------------------------------------------


def test_bc17_exclude_phases_without_extends_warns(
    tmp_path: Path,
    isolated_engine: TemplateEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BC-17: exclude_phases without extends is ignored with a warning."""
    child_path = _write_template(
        tmp_path / "child.yaml",
        id="solo",
        exclude_phases=["anything"],
        phases=[_make_phase("p1")],
    )

    with caplog.at_level(logging.WARNING):
        template = isolated_engine.load_template(child_path)  # should not raise

    assert template.id == "solo"
    warning_msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "exclude_phases" in warning_msgs.lower()
    assert "extends" in warning_msgs.lower()


# ---------------------------------------------------------------------------
# BC-18: Multi-level circular extends — clear error
# ---------------------------------------------------------------------------


def test_bc18_multi_level_circular_extends(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-18: a→b→c→a raises ValueError naming all three."""
    a_path = _write_template(tmp_path / "a.yaml", id="a", extends="b")
    _write_template(tmp_path / "b.yaml", id="b", extends="c")
    _write_template(tmp_path / "c.yaml", id="c", extends="a")

    with pytest.raises(ValueError) as exc_info:
        isolated_engine.load_template(a_path)

    msg = str(exc_info.value).lower()
    assert "circular extends" in msg
    for name in ("a", "b", "c"):
        assert name in msg, f"name {name!r} missing from cycle error: {msg}"


# ---------------------------------------------------------------------------
# BC-19: Self-cycle (A extends A) — clear error
# ---------------------------------------------------------------------------


def test_bc19_self_cycle(
    tmp_path: Path, isolated_engine: TemplateEngine
) -> None:
    """BC-19: a template that extends itself raises ValueError."""
    a_path = _write_template(tmp_path / "a.yaml", id="a", extends="a")

    with pytest.raises(ValueError) as exc_info:
        isolated_engine.load_template(a_path)

    msg = str(exc_info.value).lower()
    assert "circular extends" in msg
    assert "a" in msg
