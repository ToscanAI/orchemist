"""Tests for phase transition data model fields (Issue #231).

Covers:
- PhaseOutcome enum values and string behaviour
- PhaseDefinition.transitions and max_iterations defaults
- None normalization in PhaseDefinition.__post_init__
- PipelineTemplate.default_transitions and max_iterations defaults/normalization
- load_template() parsing of transitions/max_iterations/default_transitions from YAML
- PhaseSequencer._phase_map construction and lookup
- Backward compatibility with templates that omit transition fields
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest
import yaml

from orchestration_engine.templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from orchestration_engine.transitions import PhaseOutcome
from orchestration_engine.sequencer import PhaseSequencer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_phase(**kwargs) -> PhaseDefinition:
    """Build a minimal PhaseDefinition with sensible defaults."""
    defaults = dict(id="p1", name="Phase 1", prompt_template="do {input}")
    defaults.update(kwargs)
    return PhaseDefinition(**defaults)


def _make_template(phases: List[PhaseDefinition] = None, **kwargs) -> PipelineTemplate:
    """Build a minimal PipelineTemplate."""
    if phases is None:
        phases = [_make_phase()]
    defaults = dict(id="t1", name="Test Template", phases=phases)
    defaults.update(kwargs)
    return PipelineTemplate(**defaults)


def _make_sequencer(template: PipelineTemplate) -> PhaseSequencer:
    """Build a PhaseSequencer with a stub runner."""
    runner = MagicMock()
    runner.queue = MagicMock()
    runner.executors = []
    return PhaseSequencer(template=template, runner=runner)


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write YAML content to a temp file and return its path."""
    p = tmp_path / "template.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 1. PhaseOutcome enum
# ---------------------------------------------------------------------------


class TestPhaseOutcome:
    def test_enum_values_exist(self):
        assert PhaseOutcome.SUCCESS == "success"
        assert PhaseOutcome.FAILED == "failed"
        assert PhaseOutcome.TIMEOUT == "timeout"
        assert PhaseOutcome.SKIPPED == "skipped"

    def test_is_str_subclass(self):
        """PhaseOutcome inherits from str so it compares equal to plain strings."""
        assert isinstance(PhaseOutcome.SUCCESS, str)
        assert PhaseOutcome.SUCCESS == "success"
        assert PhaseOutcome.FAILED == "failed"

    def test_all_members(self):
        members = {m.value for m in PhaseOutcome}
        assert members == {"success", "failed", "timeout", "skipped"}

    def test_from_string(self):
        """Can construct from string value."""
        assert PhaseOutcome("success") is PhaseOutcome.SUCCESS
        assert PhaseOutcome("timeout") is PhaseOutcome.TIMEOUT

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            PhaseOutcome("unknown")


# ---------------------------------------------------------------------------
# 2. PhaseDefinition — new fields and defaults
# ---------------------------------------------------------------------------


class TestPhaseDefinitionFields:
    def test_transitions_default_is_empty_dict(self):
        phase = _make_phase()
        assert phase.transitions == {}
        assert isinstance(phase.transitions, dict)

    def test_max_iterations_default_is_zero(self):
        phase = _make_phase()
        assert phase.max_iterations == 0

    def test_transitions_set_explicitly(self):
        phase = _make_phase(transitions={"success": "next_phase", "failed": "error_handler"})
        assert phase.transitions["success"] == "next_phase"
        assert phase.transitions["failed"] == "error_handler"

    def test_max_iterations_set_explicitly(self):
        phase = _make_phase(max_iterations=5)
        assert phase.max_iterations == 5

    def test_none_transitions_normalised_to_empty_dict(self):
        """YAML null for transitions should become an empty dict."""
        phase = _make_phase(transitions=None)
        assert phase.transitions == {}

    def test_none_max_iterations_normalised_to_zero(self):
        """YAML null for max_iterations should become 0."""
        phase = _make_phase(max_iterations=None)
        assert phase.max_iterations == 0

    def test_negative_max_iterations_clamped_to_zero(self):
        phase = _make_phase(max_iterations=-3)
        assert phase.max_iterations == 0

    def test_float_max_iterations_coerced_to_int(self):
        """YAML might produce 3.0 instead of 3."""
        phase = _make_phase(max_iterations=3.0)
        assert phase.max_iterations == 3
        assert isinstance(phase.max_iterations, int)


# ---------------------------------------------------------------------------
# 3. PipelineTemplate — new fields and defaults
# ---------------------------------------------------------------------------


class TestPipelineTemplateFields:
    def test_default_transitions_default_is_empty_dict(self):
        tmpl = _make_template()
        assert tmpl.default_transitions == {}

    def test_max_iterations_default_is_10(self):
        tmpl = _make_template()
        assert tmpl.max_iterations == 10

    def test_default_transitions_set_explicitly(self):
        tmpl = _make_template(default_transitions={"success": "phase_b"})
        assert tmpl.default_transitions["success"] == "phase_b"

    def test_max_iterations_set_explicitly(self):
        tmpl = _make_template(max_iterations=7)
        assert tmpl.max_iterations == 7

    def test_none_default_transitions_normalised(self):
        tmpl = _make_template(default_transitions=None)
        assert tmpl.default_transitions == {}

    def test_none_max_iterations_normalised_to_10(self):
        tmpl = _make_template(max_iterations=None)
        assert tmpl.max_iterations == 10

    def test_zero_max_iterations_clamped_to_1(self):
        """Pipeline max_iterations must be > 0 per spec."""
        tmpl = _make_template(max_iterations=0)
        assert tmpl.max_iterations == 1

    def test_negative_max_iterations_clamped_to_1(self):
        tmpl = _make_template(max_iterations=-5)
        assert tmpl.max_iterations == 1

    def test_float_max_iterations_coerced_to_int(self):
        tmpl = _make_template(max_iterations=8.0)
        assert tmpl.max_iterations == 8
        assert isinstance(tmpl.max_iterations, int)


# ---------------------------------------------------------------------------
# 4. load_template() — YAML parsing
# ---------------------------------------------------------------------------


class TestLoadTemplateTransitions:
    """TemplateEngine.load_template() parses transition fields from YAML."""

    def _minimal_yaml(self, extra_pipeline: str = "", extra_phase: str = "") -> str:
        """Return a minimal valid pipeline YAML string.

        Args:
            extra_pipeline: Extra top-level YAML fields (properly indented, no leading newline).
            extra_phase:    Extra phase-level YAML fields (indented 4 spaces, no leading newline).
        """
        phase_block = (
            "  - id: phase_a\n"
            "    name: Phase A\n"
            "    prompt_template: do {input}\n"
        )
        if extra_phase:
            for line in extra_phase.splitlines():
                phase_block += f"    {line}\n"
        doc = (
            "id: test-pipeline\n"
            "name: Test Pipeline\n"
            "version: 1.0.0\n"
            "description: A test pipeline\n"
            "author: Test Author\n"
            f"phases:\n{phase_block}"
        )
        if extra_pipeline:
            doc += extra_pipeline + "\n"
        return doc

    def test_backward_compat_no_transitions_fields(self, tmp_path):
        """Templates without transitions fields load with defaults."""
        path = _write_yaml(tmp_path, self._minimal_yaml())
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.default_transitions == {}
        assert tmpl.max_iterations == 10
        assert tmpl.phases[0].transitions == {}
        assert tmpl.phases[0].max_iterations == 0

    def test_pipeline_level_default_transitions_parsed(self, tmp_path):
        extra = "default_transitions:\n  success: phase_b\n  failed: error_phase"
        path = _write_yaml(tmp_path, self._minimal_yaml(extra_pipeline=extra))
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.default_transitions == {"success": "phase_b", "failed": "error_phase"}

    def test_pipeline_level_max_iterations_parsed(self, tmp_path):
        path = _write_yaml(tmp_path, self._minimal_yaml(extra_pipeline="max_iterations: 20"))
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.max_iterations == 20

    def test_phase_level_transitions_parsed(self, tmp_path):
        yaml_text = (
            "id: test-pipeline\n"
            "name: Test Pipeline\n"
            "version: 1.0.0\n"
            "description: A test pipeline\n"
            "author: Test Author\n"
            "phases:\n"
            "  - id: phase_a\n"
            "    name: Phase A\n"
            "    prompt_template: do {input}\n"
            "    transitions:\n"
            "      success: phase_b\n"
            "      failed: rollback\n"
            "  - id: phase_b\n"
            "    name: Phase B\n"
            "    prompt_template: do {input}\n"
        )
        path = _write_yaml(tmp_path, yaml_text)
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.phases[0].transitions == {"success": "phase_b", "failed": "rollback"}
        # phase_b has no transitions — defaults to empty
        assert tmpl.phases[1].transitions == {}

    def test_phase_level_max_iterations_parsed(self, tmp_path):
        extra_phase = "max_iterations: 3"
        path = _write_yaml(tmp_path, self._minimal_yaml(extra_phase=extra_phase))
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.phases[0].max_iterations == 3

    def test_null_default_transitions_normalised(self, tmp_path):
        """YAML null (null / ~) for default_transitions becomes empty dict."""
        path = _write_yaml(tmp_path, self._minimal_yaml(extra_pipeline="default_transitions: null"))
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.default_transitions == {}

    def test_null_pipeline_max_iterations_uses_default(self, tmp_path):
        """YAML null for pipeline max_iterations keeps default 10."""
        path = _write_yaml(tmp_path, self._minimal_yaml(extra_pipeline="max_iterations: null"))
        engine = TemplateEngine()
        tmpl = engine.load_template(path)
        assert tmpl.max_iterations == 10

    def test_unknown_phase_field_still_warns_not_errors(self, tmp_path, caplog):
        """Unknown phase fields log a warning but don't crash loading."""
        import logging
        path = _write_yaml(
            tmp_path,
            self._minimal_yaml(extra_phase="totally_unknown_field: oops"),
        )
        engine = TemplateEngine()
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.templates"):
            tmpl = engine.load_template(path)
        assert tmpl.phases[0].id == "phase_a"
        assert any("totally_unknown_field" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. PhaseSequencer._phase_map
# ---------------------------------------------------------------------------


class TestPhaseSequencerPhaseMap:
    def test_phase_map_built_on_init(self):
        phases = [
            _make_phase(id="alpha", name="Alpha"),
            _make_phase(id="beta", name="Beta"),
            _make_phase(id="gamma", name="Gamma"),
        ]
        tmpl = _make_template(phases=phases)
        seq = _make_sequencer(tmpl)
        assert set(seq._phase_map.keys()) == {"alpha", "beta", "gamma"}

    def test_phase_map_values_are_phase_definitions(self):
        phase = _make_phase(id="solo", name="Solo Phase")
        tmpl = _make_template(phases=[phase])
        seq = _make_sequencer(tmpl)
        retrieved = seq._phase_map["solo"]
        assert retrieved is phase

    def test_phase_map_lookup_matches_get_phase(self):
        phases = [_make_phase(id="p1", name="P1"), _make_phase(id="p2", name="P2")]
        tmpl = _make_template(phases=phases)
        seq = _make_sequencer(tmpl)
        for phase in phases:
            assert seq._phase_map[phase.id] is seq._get_phase(phase.id)

    def test_phase_map_missing_key_returns_none_with_get(self):
        tmpl = _make_template()
        seq = _make_sequencer(tmpl)
        assert seq._phase_map.get("nonexistent") is None

    def test_phase_map_empty_for_empty_template(self):
        tmpl = PipelineTemplate(id="empty", name="Empty", phases=[])
        seq = _make_sequencer(tmpl)
        assert seq._phase_map == {}


# ---------------------------------------------------------------------------
# 6. Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Ensure old code paths that don't use transition fields still work."""

    def test_template_without_transitions_executes_normally(self):
        """A template with no transitions fields should still build _phase_map."""
        phase = _make_phase(id="legacy_phase")
        tmpl = _make_template(phases=[phase])
        seq = _make_sequencer(tmpl)
        # _phase_map must be present and contain the phase
        assert "legacy_phase" in seq._phase_map

    def test_phase_without_transitions_has_empty_dict(self):
        phase = _make_phase()
        assert phase.transitions == {}

    def test_pipeline_without_default_transitions_has_empty_dict(self):
        tmpl = _make_template()
        assert tmpl.default_transitions == {}

    def test_existing_phase_fields_unaffected(self):
        """All pre-#231 fields keep their values after the dataclass change."""
        phase = _make_phase(
            id="check",
            name="Check",
            description="desc",
            task_type="research",
            model_tier="haiku",
            thinking_level="off",
            timeout_minutes=15,
            retries=2,
            retry_delay_seconds=5,
            write_files=True,
            working_dir="/tmp",
        )
        assert phase.id == "check"
        assert phase.task_type == "research"
        assert phase.model_tier == "haiku"
        assert phase.retries == 2
        assert phase.retry_delay_seconds == 5
        assert phase.write_files is True
        assert phase.working_dir == "/tmp"
        # New fields still at defaults
        assert phase.transitions == {}
        assert phase.max_iterations == 0
