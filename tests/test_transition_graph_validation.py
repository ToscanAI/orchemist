"""Tests for transition graph validation at template load time (Issue #232).

This file is the authoritative test for #232 acceptance criteria, covering
all 7 validation rules implemented in TemplateEngine.validate_template() and
TemplateEngine.validate_template_extended().

Rules:
    Rule 1: All transition targets must be known phase IDs (error)
    Rule 2: default_transitions merged per-key into each phase's effective transitions
    Rule 3: Cycles in the transition graph → warning (not error)
    Rule 4: max_iterations > 0 when any transitions are declared (error)
    Rule 5: "Transition-involved" phase classification
    Rule 6: At most one phase per parallel wave has transitions (error)
    Rule 7: depends_on on a transition target → warning (not error)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from orchestration_engine.templates import PhaseDefinition, PipelineTemplate, TemplateEngine


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_phase(
    id: str = "p1",
    transitions: Optional[Dict[str, str]] = None,
    depends_on: Optional[List[str]] = None,
    **kwargs,
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition with sensible defaults."""
    defaults: dict = dict(
        id=id,
        name=id.replace("_", " ").title(),
        prompt_template=f"do {{input}} for {id}",
    )
    defaults.update(kwargs)
    if transitions is not None:
        defaults["transitions"] = transitions
    if depends_on is not None:
        defaults["depends_on"] = depends_on
    return PhaseDefinition(**defaults)


def _make_template(
    phases: List[PhaseDefinition],
    default_transitions: Optional[Dict[str, str]] = None,
    max_iterations: int = 10,
    **kwargs,
) -> PipelineTemplate:
    """Build a minimal PipelineTemplate."""
    defaults: dict = dict(
        id="test-pipeline",
        name="Test Pipeline",
        phases=phases,
        max_iterations=max_iterations,
    )
    if default_transitions is not None:
        defaults["default_transitions"] = default_transitions
    defaults.update(kwargs)
    return PipelineTemplate(**defaults)


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write YAML content to a temp file and return its path."""
    p = tmp_path / "template.yaml"
    p.write_text(content)
    return p


def _minimal_yaml_with_phases(phases_yaml: str, pipeline_extras: str = "") -> str:
    """Build a minimal pipeline YAML string with the given phases block."""
    return (
        "id: test-pipeline\n"
        "name: Test Pipeline\n"
        "version: 1.0.0\n"
        "description: A test pipeline for transition graph validation.\n"
        "author: Test Author\n"
        f"{pipeline_extras}\n"
        f"phases:\n{phases_yaml}"
    )


ENGINE = TemplateEngine()


# ---------------------------------------------------------------------------
# Rule 1: All transition targets must be known phase IDs
# ---------------------------------------------------------------------------


class TestRule1_TransitionTargetsMustExist:
    """Error when a transition target phase ID does not exist in the template."""

    def test_valid_target_passes(self):
        """transitions: {success: phase_b} where phase_b exists → no error."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        # Filter only transition-related errors
        transition_errors = [e for e in errors if "transition target" in e]
        assert transition_errors == []

    def test_missing_target_error(self):
        """transitions: {success: nonexistent} → error containing 'nonexistent'."""
        phases = [_make_phase("phase_a", transitions={"success": "nonexistent"})]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        assert len(transition_errors) == 1
        assert "nonexistent" in transition_errors[0]

    def test_default_transitions_missing_target(self):
        """default_transitions: {success: ghost} where ghost doesn't exist → error."""
        phases = [_make_phase("phase_a")]
        tmpl = _make_template(phases, default_transitions={"success": "ghost"})
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        assert any("ghost" in e for e in transition_errors)

    def test_merged_missing_target(self):
        """default has valid target; phase override has invalid → only invalid reported."""
        phases = [
            _make_phase("phase_a", transitions={"failed": "missing_phase"}),
            _make_phase("phase_b"),
        ]
        # default success → phase_b (valid); phase_a overrides failed → missing_phase (invalid)
        tmpl = _make_template(phases, default_transitions={"success": "phase_b"})
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        # missing_phase should be reported as a bad target
        assert any("missing_phase" in e for e in transition_errors)
        # phase_b is a valid target — it should not be flagged as a missing target.
        # Note: the error message may mention "phase_b" in the list of *known* phases,
        # so we check specifically that "phase_b" doesn't appear as the bad target itself.
        for err in transition_errors:
            assert "target 'phase_b'" not in err, (
                f"phase_b is a valid phase and should never be the missing target: {err}"
            )

    def test_multiple_missing_targets(self):
        """Two bad targets → at least two separate transition errors."""
        phases = [
            _make_phase("phase_a", transitions={"success": "ghost_1", "failed": "ghost_2"}),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        assert len(transition_errors) >= 2
        targets_reported = " ".join(transition_errors)
        assert "ghost_1" in targets_reported
        assert "ghost_2" in targets_reported

    def test_typo_target_reported_with_known_phases(self):
        """Error message includes the set of known phase IDs to help authors fix typos."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phaze_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        assert len(transition_errors) == 1
        # Error should mention known phase IDs
        assert "phase_a" in transition_errors[0] or "phase_b" in transition_errors[0]
        assert "phaze_b" in transition_errors[0]

    def test_self_transition_valid(self):
        """transitions: {success: same_phase} → no Rule 1 error (is a cycle, not missing)."""
        phases = [_make_phase("phase_a", transitions={"success": "phase_a"})]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        # Self-reference is a valid phase ID — Rule 1 should not fire
        assert transition_errors == []


# ---------------------------------------------------------------------------
# Rule 2: Per-key merge semantics for default_transitions
# ---------------------------------------------------------------------------


class TestRule2_PerKeyMergeSemantics:
    """_compute_effective_transitions uses {**defaults, **phase} per-key merge."""

    def test_phase_with_no_transitions_inherits_all_defaults(self):
        """Phase transitions={}, default has 2 keys → effective has both."""
        phases = [_make_phase("phase_a", transitions={})]
        tmpl = _make_template(phases, default_transitions={"success": "phase_a", "failed": "phase_a"})
        effective = ENGINE._compute_effective_transitions(tmpl)
        assert effective["phase_a"] == {"success": "phase_a", "failed": "phase_a"}

    def test_phase_key_overrides_default_key(self):
        """Phase and default both define 'success' → phase value wins."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_a"}),
            _make_phase("phase_b", transitions={"success": "phase_b"}),
        ]
        tmpl = _make_template(
            phases,
            default_transitions={"success": "phase_b"},
        )
        effective = ENGINE._compute_effective_transitions(tmpl)
        # phase_a overrides success to "phase_a" (itself)
        assert effective["phase_a"]["success"] == "phase_a"
        # phase_b overrides success to "phase_b" (itself)
        assert effective["phase_b"]["success"] == "phase_b"

    def test_phase_preserves_non_overridden_default_keys(self):
        """Phase defines 'failed', default defines 'success' → effective has both."""
        phases = [
            _make_phase("phase_a", transitions={"failed": "phase_a"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases, default_transitions={"success": "phase_b"})
        effective = ENGINE._compute_effective_transitions(tmpl)
        assert effective["phase_a"]["success"] == "phase_b"   # inherited from default
        assert effective["phase_a"]["failed"] == "phase_a"    # phase-level

    def test_empty_default_transitions_phase_transitions_unchanged(self):
        """No defaults → effective == phase.transitions."""
        phases = [_make_phase("phase_a", transitions={"success": "phase_a"})]
        tmpl = _make_template(phases)  # default_transitions defaults to {}
        effective = ENGINE._compute_effective_transitions(tmpl)
        assert effective["phase_a"] == {"success": "phase_a"}

    def test_empty_both(self):
        """Both defaults and phase transitions empty → effective is {}."""
        phases = [_make_phase("phase_a", transitions={})]
        tmpl = _make_template(phases)
        effective = ENGINE._compute_effective_transitions(tmpl)
        assert effective["phase_a"] == {}

    def test_helper_returns_all_phase_ids(self):
        """_compute_effective_transitions returns an entry for every phase."""
        phases = [
            _make_phase("alpha"),
            _make_phase("beta"),
            _make_phase("gamma"),
        ]
        tmpl = _make_template(phases)
        effective = ENGINE._compute_effective_transitions(tmpl)
        assert set(effective.keys()) == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Rule 3: Cycle detection in transition graph warns (not errors)
# ---------------------------------------------------------------------------


class TestRule3_CycleDetectionWarns:
    """Cycles in the transition graph produce warnings, not errors."""

    def _extended(self, tmpl: PipelineTemplate):
        """Call validate_template_extended with synthetic raw_data."""
        raw_data = {
            "description": "test",
            "author": "Test Author",
            "version": "1.0.0",
            "use_cases": ["test"],
            "example_input": {"key": "val"},
        }
        return ENGINE.validate_template_extended(tmpl, raw_data)

    def test_no_cycle_no_warning(self):
        """Linear A→B→C transitions → no cycle warning."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b", transitions={"success": "phase_c"}),
            _make_phase("phase_c"),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        cycle_warnings = [w for w in warnings if "Transition cycle" in w]
        assert cycle_warnings == []

    def test_simple_cycle_warns(self):
        """A→B, B→A via transitions → warning in validate_template_extended."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b", transitions={"success": "phase_a"}),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        cycle_warnings = [w for w in warnings if "Transition cycle" in w]
        assert len(cycle_warnings) >= 1

    def test_self_loop_warns(self):
        """transitions: {success: self} → cycle warning."""
        phases = [_make_phase("phase_a", transitions={"success": "phase_a"})]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        cycle_warnings = [w for w in warnings if "Transition cycle" in w]
        assert len(cycle_warnings) >= 1

    def test_cycle_does_not_error(self):
        """Cycle produces warning, NOT error."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b", transitions={"success": "phase_a"}),
        ]
        tmpl = _make_template(phases)
        errors, warnings = self._extended(tmpl)
        # No errors from the extended validator due to transitions cycle
        cycle_errors = [e for e in errors if "Transition cycle" in e]
        assert cycle_errors == []
        # But there should be a warning
        cycle_warnings = [w for w in warnings if "Transition cycle" in w]
        assert len(cycle_warnings) >= 1

    def test_cycle_message_contains_phase_ids(self):
        """Warning text includes the phase IDs forming the loop."""
        phases = [
            _make_phase("alpha", transitions={"success": "beta"}),
            _make_phase("beta", transitions={"success": "alpha"}),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        cycle_warnings = [w for w in warnings if "Transition cycle" in w]
        assert len(cycle_warnings) >= 1
        combined = " ".join(cycle_warnings)
        assert "alpha" in combined
        assert "beta" in combined

    def test_no_cycle_in_acyclic_transition_graph(self):
        """_detect_transition_cycles returns [] for a pure DAG."""
        effective = {
            "a": {"success": "b"},
            "b": {"success": "c"},
            "c": {},
        }
        all_ids = {"a", "b", "c"}
        cycles = ENGINE._detect_transition_cycles(effective, all_ids)
        assert cycles == []


# ---------------------------------------------------------------------------
# Rule 4: max_iterations > 0 when transitions exist
# ---------------------------------------------------------------------------


class TestRule4_MaxIterationsPositive:
    """Pipeline max_iterations must be > 0 when any transitions are declared."""

    def test_transitions_with_positive_max_iterations_passes(self):
        """Template has transitions + max_iterations=5 → no Rule 4 error."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases, max_iterations=5)
        errors = ENGINE.validate_template(tmpl)
        rule4_errors = [e for e in errors if "max_iterations" in e and "must be > 0" in e]
        assert rule4_errors == []

    def test_no_transitions_passes_regardless(self):
        """No transitions at all → no Rule 4 error even at minimum max_iterations."""
        phases = [_make_phase("phase_a")]
        # PipelineTemplate clamps to 1, so max_iterations will be 1
        tmpl = _make_template(phases, max_iterations=1)
        errors = ENGINE.validate_template(tmpl)
        rule4_errors = [e for e in errors if "max_iterations" in e and "must be > 0" in e]
        assert rule4_errors == []

    def test_pipeline_default_clamped_to_1_by_post_init(self):
        """PipelineTemplate(max_iterations=0).max_iterations == 1 (clamped)."""
        tmpl = PipelineTemplate(id="t", name="T", max_iterations=0)
        # __post_init__ clamps to max(1, 0) == 1
        assert tmpl.max_iterations == 1


# ---------------------------------------------------------------------------
# Rule 5: Transition-involved phase classification
# ---------------------------------------------------------------------------


class TestRule5_TransitionInvolvedClassification:
    """Phases are 'transition-involved' if they have transitions OR are targets."""

    def test_phase_with_transitions_is_involved(self):
        """Phase with non-empty effective transitions is in phases_with_transitions."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases)
        effective = ENGINE._compute_effective_transitions(tmpl)
        phases_with = {pid for pid, eff in effective.items() if eff}
        assert "phase_a" in phases_with

    def test_phase_targeted_by_transition_is_involved(self):
        """Phase with no own transitions but is a target → in transition_involved."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases)
        effective = ENGINE._compute_effective_transitions(tmpl)

        all_targets = set()
        for eff in effective.values():
            all_targets.update(eff.values())
        phases_with = {pid for pid, eff in effective.items() if eff}
        transition_involved = phases_with | all_targets

        assert "phase_b" in transition_involved

    def test_phase_with_no_connection_not_involved(self):
        """Phase neither has transitions nor is a target → NOT in transition_involved."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
            _make_phase("phase_c"),  # isolated
        ]
        tmpl = _make_template(phases)
        effective = ENGINE._compute_effective_transitions(tmpl)

        all_targets = set()
        for eff in effective.values():
            all_targets.update(eff.values())
        phases_with = {pid for pid, eff in effective.items() if eff}
        transition_involved = phases_with | all_targets

        assert "phase_c" not in transition_involved

    def test_default_transitions_target_included(self):
        """Target referenced in default_transitions → in transition_involved for affected phases."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b"),
        ]
        # Every phase inherits success → phase_b from defaults
        tmpl = _make_template(phases, default_transitions={"success": "phase_b"})
        effective = ENGINE._compute_effective_transitions(tmpl)

        all_targets = set()
        for eff in effective.values():
            all_targets.update(eff.values())

        # phase_b is a target via default_transitions propagated to phase_a
        assert "phase_b" in all_targets


# ---------------------------------------------------------------------------
# Rule 6: At most one phase per parallel wave has transitions
# ---------------------------------------------------------------------------


class TestRule6_AtMostOneTransitionPhasePerWave:
    """Error when two phases in the same topological wave both have transitions."""

    def test_single_transition_phase_in_wave_passes(self):
        """One phase with transitions in wave → no Rule 6 error."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        rule6_errors = [e for e in errors if "multiple transition phases" in e]
        assert rule6_errors == []

    def test_two_transition_phases_in_same_wave_error(self):
        """A and B run in parallel (both depend on start), both have transitions → Rule 6 error."""
        phases = [
            # start is in wave 0; phase_a and phase_b are in wave 1 (parallel)
            _make_phase("start"),
            _make_phase("phase_a", depends_on=["start"], transitions={"success": "phase_c"}),
            _make_phase("phase_b", depends_on=["start"], transitions={"success": "phase_c"}),
            _make_phase("phase_c"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        rule6_errors = [e for e in errors if "multiple transition phases" in e]
        assert len(rule6_errors) >= 1

    def test_two_transition_phases_in_different_waves_passes(self):
        """A (wave 0) has transitions, B depends_on A and also has transitions → wave 1 → no Rule 6."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b", depends_on=["phase_a"], transitions={"success": "phase_c"}),
            _make_phase("phase_c", depends_on=["phase_b"]),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        rule6_errors = [e for e in errors if "multiple transition phases" in e]
        assert rule6_errors == []

    def test_error_message_contains_wave_index(self):
        """Rule 6 error message includes wave index."""
        phases = [
            # start is in wave 0; phase_a and phase_b are in wave 1 (parallel)
            _make_phase("start"),
            _make_phase("phase_a", depends_on=["start"], transitions={"success": "phase_c"}),
            _make_phase("phase_b", depends_on=["start"], transitions={"success": "phase_c"}),
            _make_phase("phase_c"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        rule6_errors = [e for e in errors if "multiple transition phases" in e]
        assert len(rule6_errors) >= 1
        assert "Wave" in rule6_errors[0]
        # Wave index 1 because phase_a and phase_b are in the second wave (after start)
        assert "1" in rule6_errors[0]

    def test_no_transitions_multiple_parallel_phases_passes(self):
        """Multiple parallel phases without transitions → no Rule 6 error."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b"),
            _make_phase("phase_c"),
        ]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        rule6_errors = [e for e in errors if "multiple transition phases" in e]
        assert rule6_errors == []


# ---------------------------------------------------------------------------
# Rule 7: depends_on on a transition target → warning (not error)
# ---------------------------------------------------------------------------


class TestRule7_DependsOnOnTransitionTarget:
    """Advisory warning when a transition target phase also has depends_on."""

    def _extended(self, tmpl: PipelineTemplate):
        """Call validate_template_extended with minimal valid raw_data."""
        raw_data = {
            "description": "test",
            "author": "Test Author",
            "version": "1.0.0",
            "use_cases": ["test"],
            "example_input": {"key": "val"},
        }
        return ENGINE.validate_template_extended(tmpl, raw_data)

    def test_transition_target_with_no_depends_on_no_warning(self):
        """Target phase has empty depends_on → no Rule 7 warning."""
        phases = [
            _make_phase("phase_a", transitions={"success": "phase_b"}),
            _make_phase("phase_b", depends_on=[]),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        rule7_warnings = [w for w in warnings if "is a transition target" in w]
        assert rule7_warnings == []

    def test_transition_target_with_depends_on_warns(self):
        """Target phase has depends_on: [other] → Rule 7 warning."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b", transitions={"success": "phase_c"}),
            _make_phase("phase_c", depends_on=["phase_a"]),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        rule7_warnings = [w for w in warnings if "is a transition target" in w]
        assert len(rule7_warnings) >= 1

    def test_non_target_phase_with_depends_on_no_warning(self):
        """Non-target phase with depends_on → no Rule 7 warning."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b", depends_on=["phase_a"]),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        rule7_warnings = [w for w in warnings if "is a transition target" in w]
        assert rule7_warnings == []

    def test_warning_message_names_the_phase(self):
        """Rule 7 warning contains the offending phase ID."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b", transitions={"success": "phase_c"}),
            _make_phase("phase_c", depends_on=["phase_a"]),
        ]
        tmpl = _make_template(phases)
        _, warnings = self._extended(tmpl)
        rule7_warnings = [w for w in warnings if "is a transition target" in w]
        assert len(rule7_warnings) >= 1
        assert "phase_c" in rule7_warnings[0]

    def test_warns_not_errors(self):
        """Rule 7 produces a warning, NOT an error."""
        phases = [
            _make_phase("phase_a"),
            _make_phase("phase_b", transitions={"success": "phase_c"}),
            _make_phase("phase_c", depends_on=["phase_a"]),
        ]
        tmpl = _make_template(phases)
        errors, warnings = self._extended(tmpl)
        # No errors due to Rule 7 (it is advisory only)
        rule7_errors = [e for e in errors if "is a transition target" in e]
        assert rule7_errors == []
        # But a warning should be emitted
        rule7_warnings = [w for w in warnings if "is a transition target" in w]
        assert len(rule7_warnings) >= 1


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestValidateTemplateIntegration:
    """End-to-end integration tests covering multiple rules working together."""

    def _extended(self, tmpl: PipelineTemplate, **raw_overrides):
        """Call validate_template_extended with valid raw_data."""
        raw_data = {
            "description": "A fully valid pipeline for integration testing.",
            "author": "Integration Test Author",
            "version": "1.0.0",
            "use_cases": ["integration testing"],
            "example_input": {"topic": "test"},
        }
        raw_data.update(raw_overrides)
        return ENGINE.validate_template_extended(tmpl, raw_data)

    def test_full_valid_transition_template_passes(self):
        """Complete template with valid transitions → no transition-graph errors."""
        phases = [
            # research has a self-loop on failure (intentional cycle, warn not error)
            _make_phase("research", transitions={"success": "write", "failed": "research"}),
            # write has NO depends_on to avoid Rule 7 warning (transition target + depends_on)
            _make_phase("write"),
        ]
        tmpl = _make_template(phases, max_iterations=5)

        struct_errors = ENGINE.validate_template(tmpl)
        # No transition-graph structural errors expected
        transition_errors = [
            e for e in struct_errors
            if "transition target" in e
            or ("max_iterations" in e and "must be > 0" in e)
            or "multiple transition phases" in e
        ]
        assert transition_errors == []

        _, warnings = self._extended(tmpl)
        # Rule 7 should not fire: write has no depends_on
        dep_warnings = [w for w in warnings if "is a transition target" in w]
        assert dep_warnings == []

    def test_full_invalid_target_returns_errors(self):
        """Template with bad target → validate_template() returns non-empty errors."""
        phases = [_make_phase("phase_a", transitions={"success": "does_not_exist"})]
        tmpl = _make_template(phases)
        errors = ENGINE.validate_template(tmpl)
        transition_errors = [e for e in errors if "transition target" in e]
        assert len(transition_errors) >= 1
        assert "does_not_exist" in transition_errors[0]

    def test_yaml_round_trip_merge_semantics(self, tmp_path):
        """Load from YAML with default_transitions + phase override → effective correct."""
        yaml_text = (
            "id: merge-test\n"
            "name: Merge Test\n"
            "version: 1.0.0\n"
            "description: Tests merge semantics\n"
            "author: Test Author\n"
            "default_transitions:\n"
            "  success: phase_b\n"
            "  failed: phase_a\n"
            "phases:\n"
            "  - id: phase_a\n"
            "    name: Phase A\n"
            "    prompt_template: 'do {input}'\n"
            "    transitions:\n"
            "      failed: phase_b\n"       # overrides default failed→phase_a
            "  - id: phase_b\n"
            "    name: Phase B\n"
            "    prompt_template: 'do {input}'\n"
        )
        path = _write_yaml(tmp_path, yaml_text)
        tmpl = TemplateEngine().load_template(path)
        effective = ENGINE._compute_effective_transitions(tmpl)

        # phase_a: inherits success→phase_b from default; overrides failed→phase_b
        assert effective["phase_a"]["success"] == "phase_b"
        assert effective["phase_a"]["failed"] == "phase_b"   # overridden

        # phase_b: inherits both defaults (no override)
        assert effective["phase_b"]["success"] == "phase_b"
        assert effective["phase_b"]["failed"] == "phase_a"

    def test_existing_templates_still_pass_structural_validate(self):
        """Regression guard: all templates in templates/ pass validate_template() without
        new transition-related errors (they have no transitions defined)."""
        import glob
        repo_root = Path(__file__).parent.parent
        template_paths = sorted(
            glob.glob(str(repo_root .joinpath("templates") / "*.yaml"))
            + glob.glob(str(repo_root .joinpath("templates") / "*.yml"))
            + glob.glob(str(repo_root / "examples" / "*.yaml"))
            + glob.glob(str(repo_root / "examples" / "*.yml"))
        )
        engine = TemplateEngine()
        transition_error_categories = [
            "transition target",
            "max_iterations",
            "multiple transition phases",
        ]
        for tpath in template_paths:
            tmpl = engine.load_template(Path(tpath))
            errors = engine.validate_template(tmpl)
            # No transition-graph errors should appear in existing templates
            transition_errors = [
                e for e in errors
                if any(cat in e for cat in transition_error_categories)
            ]
            assert transition_errors == [], (
                f"Template {tpath} unexpectedly produced transition errors: {transition_errors}"
            )
