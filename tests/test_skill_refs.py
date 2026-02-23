"""Tests for skill_refs feature — external prompt injection via PhaseDefinition.

Covers:
- skill loading from file (PhaseSequencer._load_skill)
- frontmatter stripping
- skill_context template variable injection
- missing skill file validation error (validate_template)
- empty skill_refs (backward compat)
- multiple skill_refs
"""

import textwrap
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from src.orchestration_engine.templates import (
    PhaseDefinition,
    PipelineTemplate,
    TemplateEngine,
)
from src.orchestration_engine.sequencer import PhaseSequencer, _SafeDict
from src.orchestration_engine.runner import DryRunExecutor, TaskRunner
from src.orchestration_engine.config import EngineConfig, QueueConfig, ModelsConfig
from src.orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path):
    """Temporary directory that acts as our skills folder."""
    d = tmp_path / "skills"
    d.mkdir()
    return d


@pytest.fixture
def template_dir(tmp_path):
    """Temporary directory for templates."""
    d = tmp_path / "templates"
    d.mkdir()
    return d


@pytest.fixture
def test_config():
    return EngineConfig(
        queue=QueueConfig(max_workers=1, poll_interval_seconds=1),
        models=ModelsConfig(default_tier="sonnet-4"),
        dry_run=True,
    )


@pytest.fixture
def fast_runner(test_config):
    db = Database(":memory:")
    runner = TaskRunner(database=db, config=test_config)
    runner.executors = [DryRunExecutor(delay_seconds=0.0, failure_rate=0.0)]
    return runner


def _make_template(
    phases: list,
    template_path: Path = None,
) -> PipelineTemplate:
    """Build a minimal PipelineTemplate for testing."""
    return PipelineTemplate(
        id="test-pipeline",
        name="Test Pipeline",
        version="1.0.0",
        description="test",
        author="tester",
        phases=phases,
        template_path=template_path,
    )


def _make_sequencer(template: PipelineTemplate, fast_runner) -> PhaseSequencer:
    return PhaseSequencer(template=template, runner=fast_runner, config={})


# ---------------------------------------------------------------------------
# _load_skill — unit tests
# ---------------------------------------------------------------------------


class TestLoadSkill:
    """Tests for PhaseSequencer._load_skill."""

    def test_load_skill_without_frontmatter(self, skills_dir):
        """A plain text file is loaded as-is."""
        skill_file = skills_dir / "plain.md"
        skill_file.write_text("You are an expert researcher.\nBe concise.")

        name, content = PhaseSequencer._load_skill(str(skill_file))
        assert name == "plain"
        assert content == "You are an expert researcher.\nBe concise."

    def test_load_skill_with_frontmatter_strips_it(self, skills_dir):
        """Frontmatter between --- delimiters is stripped; body is returned."""
        skill_file = skills_dir / "analyst.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: data_analyst
            version: 1.0
            ---
            You are a skilled data analyst.
            Focus on numbers.
        """))

        name, content = PhaseSequencer._load_skill(str(skill_file))
        assert name == "data_analyst"
        assert "You are a skilled data analyst." in content
        assert "---" not in content
        assert "name:" not in content

    def test_frontmatter_name_takes_precedence_over_stem(self, skills_dir):
        """The 'name:' key in frontmatter overrides the filename stem."""
        skill_file = skills_dir / "my_file.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: custom_skill_name
            ---
            Skill body.
        """))

        name, _ = PhaseSequencer._load_skill(str(skill_file))
        assert name == "custom_skill_name"

    def test_filename_stem_used_when_no_name_in_frontmatter(self, skills_dir):
        """Filename stem used when frontmatter lacks 'name' key."""
        skill_file = skills_dir / "writing_style.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            version: 2.0
            ---
            Write in an engaging style.
        """))

        name, _ = PhaseSequencer._load_skill(str(skill_file))
        assert name == "writing_style"

    def test_relative_path_resolved_against_template_dir(self, skills_dir):
        """Relative skill_ref is resolved relative to template_dir first."""
        skill_file = skills_dir / "style.md"
        skill_file.write_text("Be concise and clear.")

        name, content = PhaseSequencer._load_skill("style.md", template_dir=skills_dir)
        assert name == "style"
        assert content == "Be concise and clear."

    def test_missing_skill_raises_file_not_found(self, skills_dir):
        """FileNotFoundError is raised when skill file cannot be located."""
        with pytest.raises(FileNotFoundError, match="no_such_skill.md"):
            PhaseSequencer._load_skill("no_such_skill.md", template_dir=skills_dir)

    def test_absolute_path(self, skills_dir):
        """An absolute path is loaded directly."""
        skill_file = skills_dir / "abs_skill.md"
        skill_file.write_text("Absolute skill content.")

        name, content = PhaseSequencer._load_skill(str(skill_file.resolve()))
        assert name == "abs_skill"
        assert content == "Absolute skill content."

    def test_empty_frontmatter_body_returned(self, skills_dir):
        """Empty frontmatter (just ---) still returns the body correctly."""
        skill_file = skills_dir / "minimal.md"
        skill_file.write_text("---\n---\nJust the body.")

        name, content = PhaseSequencer._load_skill(str(skill_file))
        assert content == "Just the body."

    def test_body_stripped_of_leading_trailing_whitespace(self, skills_dir):
        """Returned body has leading/trailing whitespace stripped."""
        skill_file = skills_dir / "padded.md"
        skill_file.write_text("---\nname: padded\n---\n\n\n  Some content.  \n\n")

        _, content = PhaseSequencer._load_skill(str(skill_file))
        assert content == "Some content."


# ---------------------------------------------------------------------------
# PhaseDefinition — dataclass field
# ---------------------------------------------------------------------------


class TestPhaseDefinitionSkillRefs:
    def test_default_empty_list(self):
        """skill_refs defaults to empty list — existing phases unaffected."""
        phase = PhaseDefinition(id="p1", name="Phase 1")
        assert phase.skill_refs == []

    def test_none_coerced_to_empty_list(self):
        """None value for skill_refs (from YAML) is normalised to []."""
        phase = PhaseDefinition(id="p1", name="Phase 1", skill_refs=None)
        assert phase.skill_refs == []

    def test_skill_refs_stored(self):
        """skill_refs list is stored as given."""
        phase = PhaseDefinition(id="p1", name="Phase 1", skill_refs=["a.md", "b.md"])
        assert phase.skill_refs == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# Template loading — skill_refs parsed from YAML
# ---------------------------------------------------------------------------


class TestTemplateLoadSkillRefs:
    def test_skill_refs_parsed_from_yaml(self, template_dir):
        """skill_refs in YAML phase data are loaded into PhaseDefinition."""
        tpl_file = template_dir / "with_skills.yaml"
        tpl_file.write_text(textwrap.dedent("""\
            id: test-skills
            name: Test With Skills
            version: 1.0.0
            description: Testing skill_refs parsing
            author: tester
            phases:
              - id: phase1
                name: Phase 1
                skill_refs:
                  - skills/tone.md
                  - skills/format.md
        """))

        engine = TemplateEngine(templates_dir=template_dir)
        template = engine.load_template(tpl_file)

        assert template.phases[0].skill_refs == ["skills/tone.md", "skills/format.md"]

    def test_template_without_skill_refs_loads_fine(self, template_dir):
        """Existing templates without skill_refs continue to work (empty list)."""
        tpl_file = template_dir / "no_skills.yaml"
        tpl_file.write_text(textwrap.dedent("""\
            id: test-no-skills
            name: Test No Skills
            version: 1.0.0
            description: No skill refs
            author: tester
            phases:
              - id: phase1
                name: Phase 1
                prompt_template: "Write about {input}"
        """))

        engine = TemplateEngine(templates_dir=template_dir)
        template = engine.load_template(tpl_file)

        assert template.phases[0].skill_refs == []

    def test_template_path_stored_on_pipeline_template(self, template_dir):
        """load_template stores the resolved file path in template.template_path."""
        tpl_file = template_dir / "simple.yaml"
        tpl_file.write_text(textwrap.dedent("""\
            id: simple
            name: Simple
            version: 1.0.0
            description: desc
            author: auth
            phases: []
        """))

        engine = TemplateEngine(templates_dir=template_dir)
        template = engine.load_template(tpl_file)

        assert template.template_path is not None
        assert template.template_path == tpl_file.resolve()


# ---------------------------------------------------------------------------
# validate_template — skill_ref file existence check
# ---------------------------------------------------------------------------


class TestValidateTemplateSkillRefs:
    def test_missing_skill_ref_produces_error(self, template_dir):
        """validate_template reports an error when a skill_ref file is missing."""
        phase = PhaseDefinition(
            id="p1",
            name="Phase 1",
            skill_refs=["nonexistent_skill.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        assert any("nonexistent_skill.md" in e for e in errors)
        assert any("skill_ref" in e.lower() for e in errors)

    def test_existing_skill_ref_passes_validation(self, template_dir, skills_dir):
        """validate_template passes when skill_ref files exist."""
        skill_file = skills_dir / "existing_skill.md"
        skill_file.write_text("Skill content.")

        phase = PhaseDefinition(
            id="p1",
            name="Phase 1",
            skill_refs=[str(skill_file)],  # absolute path
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        skill_errors = [e for e in errors if "skill_ref" in e.lower()]
        assert skill_errors == []

    def test_relative_skill_ref_resolved_against_template_dir(self, template_dir):
        """Relative skill_ref is resolved relative to template_dir in validation."""
        skill_file = template_dir / "my_skill.md"
        skill_file.write_text("Content.")

        phase = PhaseDefinition(
            id="p1",
            name="Phase 1",
            skill_refs=["my_skill.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        skill_errors = [e for e in errors if "skill_ref" in e.lower()]
        assert skill_errors == []

    def test_multiple_missing_skill_refs_each_reported(self, template_dir):
        """Each missing skill_ref produces its own error."""
        phase = PhaseDefinition(
            id="p1",
            name="Phase 1",
            skill_refs=["missing_a.md", "missing_b.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        skill_errors = [e for e in errors if "skill_ref" in e.lower()]
        assert len(skill_errors) == 2

    def test_empty_skill_refs_no_errors(self, template_dir):
        """Empty skill_refs list produces no skill-related errors."""
        phase = PhaseDefinition(id="p1", name="Phase 1", skill_refs=[])
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        skill_errors = [e for e in errors if "skill_ref" in e.lower()]
        assert skill_errors == []

    def test_error_message_includes_phase_id(self, template_dir):
        """The error message identifies which phase has the bad skill_ref."""
        phase = PhaseDefinition(
            id="my_special_phase",
            name="Phase",
            skill_refs=["ghost.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")

        engine = TemplateEngine(templates_dir=template_dir)
        errors = engine.validate_template(template)

        assert any("my_special_phase" in e for e in errors)


# ---------------------------------------------------------------------------
# _build_phase_input — skill_context injection
# ---------------------------------------------------------------------------


class TestBuildPhaseInputSkillContext:
    def test_skill_context_injected_into_template(self, template_dir, fast_runner):
        """skill_context[name] is available in prompt_template format strings."""
        skill_file = template_dir / "tone.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: tone
            ---
            Always write in a professional tone.
        """))

        phase = PhaseDefinition(
            id="write",
            name="Write",
            prompt_template="Use this guidance: {skill_context[tone]}\n\nWrite: {input[topic]}",
            skill_refs=["tone.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {"topic": "my topic"})
        assert "Always write in a professional tone." in result
        assert "Write: my topic" in result

    def test_multiple_skill_refs_all_injected(self, template_dir, fast_runner):
        """Multiple skill_refs are all available as skill_context[name]."""
        (template_dir / "tone.md").write_text("---\nname: tone\n---\nProfessional tone.")
        (template_dir / "format.md").write_text("---\nname: format\n---\nUse bullet lists.")

        phase = PhaseDefinition(
            id="write",
            name="Write",
            prompt_template="{skill_context[tone]} | {skill_context[format]}",
            skill_refs=["tone.md", "format.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {})
        assert "Professional tone." in result
        assert "Use bullet lists." in result

    def test_empty_skill_refs_no_skill_context_keys(self, template_dir, fast_runner):
        """Empty skill_refs: prompt renders normally without skill_context."""
        phase = PhaseDefinition(
            id="write",
            name="Write",
            prompt_template="Write about {input[topic]}",
            skill_refs=[],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {"topic": "cats"})
        assert result == "Write about cats"

    def test_missing_skill_file_produces_placeholder(self, template_dir, fast_runner):
        """If a skill file is missing at runtime, a placeholder is inserted."""
        phase = PhaseDefinition(
            id="write",
            name="Write",
            prompt_template="Context: {skill_context[ghost]}\nWrite.",
            skill_refs=["ghost_skill.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        # Should not raise — uses placeholder
        result = seq._build_phase_input(phase, {})
        assert "SKILL_LOAD_ERROR" in result or "<MISSING" in result

    def test_skill_context_without_template_path(self, template_dir, fast_runner):
        """Absolute path skill_refs work even when template_path is None."""
        skill_file = template_dir / "abs.md"
        skill_file.write_text("---\nname: abs_skill\n---\nAbsolute content.")

        phase = PhaseDefinition(
            id="p1",
            name="P1",
            prompt_template="{skill_context[abs_skill]}",
            skill_refs=[str(skill_file.resolve())],
        )
        template = _make_template([phase], template_path=None)
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {})
        assert "Absolute content." in result

    def test_no_prompt_template_returns_empty(self, template_dir, fast_runner):
        """Phases without prompt_template still return empty string (compat)."""
        phase = PhaseDefinition(
            id="p1",
            name="P1",
            skill_refs=["some_skill.md"],
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {})
        assert result == ""


# ---------------------------------------------------------------------------
# Backward compatibility — templates without skill_refs
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_existing_template_yaml_no_skill_refs_loads(self, template_dir):
        """YAML template without skill_refs loads without error."""
        tpl_file = template_dir / "legacy.yaml"
        tpl_file.write_text(textwrap.dedent("""\
            id: legacy
            name: Legacy Template
            version: 1.0.0
            description: Old template without skill_refs
            author: legacy_author
            phases:
              - id: research
                name: Research
                task_type: research
                prompt_template: "Research: {input}"
              - id: write
                name: Write
                task_type: content
                depends_on: [research]
                prompt_template: "Write based on {previous_output[research]}"
        """))

        engine = TemplateEngine(templates_dir=template_dir)
        template = engine.load_template(tpl_file)

        assert template.phases[0].skill_refs == []
        assert template.phases[1].skill_refs == []

        errors = engine.validate_template(template)
        assert errors == []

    def test_build_phase_input_no_skill_refs(self, template_dir, fast_runner):
        """Sequencer works fine for phases without skill_refs."""
        phase = PhaseDefinition(
            id="p1",
            name="P1",
            prompt_template="Research: {input[topic]}",
        )
        template = _make_template([phase], template_path=template_dir / "fake.yaml")
        seq = _make_sequencer(template, fast_runner)

        result = seq._build_phase_input(phase, {"topic": "AI"})
        assert result == "Research: AI"
