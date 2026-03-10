"""Tests for Issue #506 — Sprint Runner Meta-Template.

Covers:
- SR-01: sprint-runner-v1.yaml loads and validates without errors
- SR-02: sprint-runner-step-v1.yaml loads and validates without errors
- SR-03: sprint-runner-v1 has correct on_complete chain to sprint-runner-step-v1
- SR-04: sprint-runner-step-v1 has correct on_complete chain back to sprint-runner-v1
- SR-05: No self-referential on_complete in either template
- SR-06: max_chain_depth is set and > 5 in both templates
- SR-07: sprint-runner-v1 on_complete.failed is empty (fail-fast behaviour)
- SR-08: sprint-runner-step-v1 on_complete.failed is empty (fail-fast behaviour)
- SR-09: sprint-runner-v1 config_schema requires sprint_name, repo_path, repo_url
- SR-10: sprint-runner-step-v1 config_schema requires parent_output_dir
- SR-11: Both templates have exactly 6 phases (prepare, spec, implement, review, fix, test)
- SR-12: Both templates have example_input set (passes extended lint)
- SR-13: on_complete input_map in sprint-runner-v1 passes parent_output_dir to step
- SR-14: on_complete input_map in sprint-runner-step-v1 passes parent_output_dir back
- SR-15: sprint-runner-step-v1 on_complete passes issues_json as empty list to v1
"""

from pathlib import Path

import pytest

from orchestration_engine.templates import (
    OnCompleteConfig,
    PipelineTemplate,
    TemplateEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _load(name: str) -> PipelineTemplate:
    """Load a template by filename from the templates/ directory."""
    engine = TemplateEngine()
    return engine.load_template(TEMPLATES_DIR / name)


def _load_and_validate(name: str):
    """Return (template, errors) for a template in templates/."""
    engine = TemplateEngine()
    tpl = engine.load_template(TEMPLATES_DIR / name)
    errors = engine.validate_template(tpl)
    return tpl, errors


# ---------------------------------------------------------------------------
# SR-01 & SR-02: Both templates load and validate
# ---------------------------------------------------------------------------


class TestSprintRunnerLoadsAndValidates:
    def test_sprint_runner_v1_loads(self):
        """SR-01a: sprint-runner-v1.yaml loads without raising."""
        tpl = _load("sprint-runner-v1.yaml")
        assert tpl.id == "sprint-runner-v1"

    def test_sprint_runner_v1_validates(self):
        """SR-01b: sprint-runner-v1.yaml has no validation errors."""
        _, errors = _load_and_validate("sprint-runner-v1.yaml")
        assert errors == [], f"Unexpected validation errors: {errors}"

    def test_sprint_runner_step_v1_loads(self):
        """SR-02a: sprint-runner-step-v1.yaml loads without raising."""
        tpl = _load("sprint-runner-step-v1.yaml")
        assert tpl.id == "sprint-runner-step-v1"

    def test_sprint_runner_step_v1_validates(self):
        """SR-02b: sprint-runner-step-v1.yaml has no validation errors."""
        _, errors = _load_and_validate("sprint-runner-step-v1.yaml")
        assert errors == [], f"Unexpected validation errors: {errors}"


# ---------------------------------------------------------------------------
# SR-03 & SR-04: Ping-pong on_complete chain
# ---------------------------------------------------------------------------


class TestSprintRunnerPingPongChain:
    def test_sprint_runner_v1_chains_to_step(self):
        """SR-03: sprint-runner-v1 on_complete.success chains to sprint-runner-step-v1."""
        tpl = _load("sprint-runner-v1.yaml")
        assert tpl.on_complete is not None
        assert isinstance(tpl.on_complete, OnCompleteConfig)
        assert len(tpl.on_complete.success) == 1, "Expected exactly one success entry"
        assert tpl.on_complete.success[0].template == "sprint-runner-step-v1", (
            f"Expected 'sprint-runner-step-v1', got '{tpl.on_complete.success[0].template}'"
        )

    def test_sprint_runner_step_chains_back_to_v1(self):
        """SR-04: sprint-runner-step-v1 on_complete.success chains back to sprint-runner-v1."""
        tpl = _load("sprint-runner-step-v1.yaml")
        assert tpl.on_complete is not None
        assert isinstance(tpl.on_complete, OnCompleteConfig)
        assert len(tpl.on_complete.success) == 1, "Expected exactly one success entry"
        assert tpl.on_complete.success[0].template == "sprint-runner-v1", (
            f"Expected 'sprint-runner-v1', got '{tpl.on_complete.success[0].template}'"
        )


# ---------------------------------------------------------------------------
# SR-05: No self-referential on_complete (validate_template enforces this)
# ---------------------------------------------------------------------------


class TestNoSelfReference:
    def test_sprint_runner_v1_no_self_reference(self):
        """SR-05a: sprint-runner-v1 does not chain to itself."""
        tpl = _load("sprint-runner-v1.yaml")
        if tpl.on_complete:
            for entry in (tpl.on_complete.success + tpl.on_complete.failed):
                assert entry.template != tpl.id, (
                    f"Self-referential chain detected: {tpl.id} → {entry.template}"
                )

    def test_sprint_runner_step_v1_no_self_reference(self):
        """SR-05b: sprint-runner-step-v1 does not chain to itself."""
        tpl = _load("sprint-runner-step-v1.yaml")
        if tpl.on_complete:
            for entry in (tpl.on_complete.success + tpl.on_complete.failed):
                assert entry.template != tpl.id, (
                    f"Self-referential chain detected: {tpl.id} → {entry.template}"
                )


# ---------------------------------------------------------------------------
# SR-06: max_chain_depth is set and reasonable for a sprint
# ---------------------------------------------------------------------------


class TestMaxChainDepth:
    def test_sprint_runner_v1_chain_depth(self):
        """SR-06a: sprint-runner-v1 max_chain_depth supports more than 5 issues."""
        tpl = _load("sprint-runner-v1.yaml")
        assert tpl.on_complete is not None
        assert tpl.on_complete.max_chain_depth > 5, (
            f"max_chain_depth {tpl.on_complete.max_chain_depth} is too low "
            f"for a useful sprint (must be > 5)"
        )

    def test_sprint_runner_step_v1_chain_depth(self):
        """SR-06b: sprint-runner-step-v1 max_chain_depth matches v1."""
        tpl_v1 = _load("sprint-runner-v1.yaml")
        tpl_step = _load("sprint-runner-step-v1.yaml")
        assert tpl_step.on_complete is not None
        assert tpl_step.on_complete.max_chain_depth == tpl_v1.on_complete.max_chain_depth, (
            "max_chain_depth mismatch between sprint-runner-v1 and sprint-runner-step-v1"
        )


# ---------------------------------------------------------------------------
# SR-07 & SR-08: Fail-fast — on_complete.failed is empty in both templates
# ---------------------------------------------------------------------------


class TestFailFast:
    def test_sprint_runner_v1_failed_is_empty(self):
        """SR-07: sprint-runner-v1 on_complete.failed is empty (stop on failure)."""
        tpl = _load("sprint-runner-v1.yaml")
        assert tpl.on_complete is not None
        assert tpl.on_complete.failed == [], (
            "on_complete.failed must be empty to stop the chain on failure"
        )

    def test_sprint_runner_step_failed_is_empty(self):
        """SR-08: sprint-runner-step-v1 on_complete.failed is empty (stop on failure)."""
        tpl = _load("sprint-runner-step-v1.yaml")
        assert tpl.on_complete is not None
        assert tpl.on_complete.failed == [], (
            "on_complete.failed must be empty to stop the chain on failure"
        )


# ---------------------------------------------------------------------------
# SR-09 & SR-10: Config schema required fields
# ---------------------------------------------------------------------------


class TestConfigSchemaRequiredFields:
    def test_sprint_runner_v1_required_fields(self):
        """SR-09: sprint-runner-v1 config_schema requires sprint_name, repo_path, repo_url."""
        tpl = _load("sprint-runner-v1.yaml")
        schema = tpl.config_schema or {}
        required = schema.get("required", [])
        for field in ("sprint_name", "repo_path", "repo_url"):
            assert field in required, (
                f"Expected '{field}' in sprint-runner-v1 required fields, got: {required}"
            )

    def test_sprint_runner_step_required_fields(self):
        """SR-10: sprint-runner-step-v1 config_schema requires parent_output_dir."""
        tpl = _load("sprint-runner-step-v1.yaml")
        schema = tpl.config_schema or {}
        required = schema.get("required", [])
        assert "parent_output_dir" in required, (
            f"Expected 'parent_output_dir' in sprint-runner-step-v1 required fields, got: {required}"
        )
        for field in ("sprint_name", "repo_path", "repo_url"):
            assert field in required, (
                f"Expected '{field}' in sprint-runner-step-v1 required fields, got: {required}"
            )


# ---------------------------------------------------------------------------
# SR-11: Both templates have exactly 6 phases
# ---------------------------------------------------------------------------


class TestPhaseCount:
    EXPECTED_PHASE_IDS = {"prepare", "spec", "implement", "review", "fix", "test"}

    def test_sprint_runner_v1_phase_count(self):
        """SR-11a: sprint-runner-v1 has all 6 expected phases."""
        tpl = _load("sprint-runner-v1.yaml")
        phase_ids = {p.id for p in tpl.phases}
        assert phase_ids == self.EXPECTED_PHASE_IDS, (
            f"Expected phases {self.EXPECTED_PHASE_IDS}, got {phase_ids}"
        )

    def test_sprint_runner_step_v1_phase_count(self):
        """SR-11b: sprint-runner-step-v1 has all 6 expected phases."""
        tpl = _load("sprint-runner-step-v1.yaml")
        phase_ids = {p.id for p in tpl.phases}
        assert phase_ids == self.EXPECTED_PHASE_IDS, (
            f"Expected phases {self.EXPECTED_PHASE_IDS}, got {phase_ids}"
        )


# ---------------------------------------------------------------------------
# SR-12: example_input is set (passes extended lint)
# ---------------------------------------------------------------------------


class TestExampleInput:
    def test_sprint_runner_v1_has_example_input(self):
        """SR-12a: sprint-runner-v1 has example_input defined."""
        tpl = _load("sprint-runner-v1.yaml")
        assert tpl.example_input, "sprint-runner-v1 must have example_input set"
        assert "sprint_name" in tpl.example_input, (
            "example_input should contain sprint_name"
        )
        assert "repo_path" in tpl.example_input, (
            "example_input should contain repo_path"
        )

    def test_sprint_runner_step_v1_has_example_input(self):
        """SR-12b: sprint-runner-step-v1 has example_input defined."""
        tpl = _load("sprint-runner-step-v1.yaml")
        assert tpl.example_input, "sprint-runner-step-v1 must have example_input set"
        assert "parent_output_dir" in tpl.example_input, (
            "example_input should contain parent_output_dir"
        )


# ---------------------------------------------------------------------------
# SR-13 & SR-14 & SR-15: input_map content for correct data passing
# ---------------------------------------------------------------------------


class TestInputMapContent:
    def test_sprint_runner_v1_passes_output_dir_to_step(self):
        """SR-13: sprint-runner-v1 on_complete passes {{output_dir}} as parent_output_dir."""
        tpl = _load("sprint-runner-v1.yaml")
        entry = tpl.on_complete.success[0]
        assert "parent_output_dir" in entry.input_map, (
            "sprint-runner-v1 must pass parent_output_dir in on_complete input_map"
        )
        assert "output_dir" in entry.input_map["parent_output_dir"], (
            "parent_output_dir value must reference {{output_dir}} placeholder"
        )

    def test_sprint_runner_v1_passes_common_fields(self):
        """SR-13b: sprint-runner-v1 on_complete passes all common config fields."""
        tpl = _load("sprint-runner-v1.yaml")
        entry = tpl.on_complete.success[0]
        for key in ("sprint_name", "repo_path", "repo_url", "test_command"):
            assert key in entry.input_map, (
                f"sprint-runner-v1 on_complete must pass '{key}' to step template"
            )

    def test_sprint_runner_step_passes_output_dir_to_v1(self):
        """SR-14: sprint-runner-step-v1 on_complete passes {{output_dir}} as parent_output_dir."""
        tpl = _load("sprint-runner-step-v1.yaml")
        entry = tpl.on_complete.success[0]
        assert "parent_output_dir" in entry.input_map, (
            "sprint-runner-step-v1 must pass parent_output_dir in on_complete input_map"
        )
        assert "output_dir" in entry.input_map["parent_output_dir"], (
            "parent_output_dir value must reference {{output_dir}} placeholder"
        )

    def test_sprint_runner_step_passes_empty_issues_json_to_v1(self):
        """SR-15: sprint-runner-step-v1 on_complete passes issues_json=[] to sprint-runner-v1.

        This ensures sprint-runner-v1 uses the parent_output_dir path (not issues_json)
        when bouncing back from the step template.
        """
        tpl = _load("sprint-runner-step-v1.yaml")
        entry = tpl.on_complete.success[0]
        assert "issues_json" in entry.input_map, (
            "sprint-runner-step-v1 must pass issues_json in on_complete input_map "
            "so sprint-runner-v1 uses the parent_output_dir path instead"
        )
        assert entry.input_map["issues_json"] == "[]", (
            f"issues_json must be '[]' in step→v1 chain, got: {entry.input_map['issues_json']!r}"
        )
