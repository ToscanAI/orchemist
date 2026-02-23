"""Tests for Issue #77 — Three diverse example pipeline templates.

Covers:
- All 3 templates load successfully (content-pipeline-v2, code-review-pipeline, research-pipeline)
- All 3 pass `orch validate` (exit code 0, no errors)
- Phase counts are correct (7, 5, 6)
- Dependencies are wired correctly
- Config schemas have required fields
- Doc fields are present (description, author, version, use_cases, tags, category, example_input)
"""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from src.orchestration_engine.cli import main
from src.orchestration_engine.templates import TemplateEngine

# ---------------------------------------------------------------------------
# Paths to the three example templates
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

CONTENT_PIPELINE = EXAMPLES_DIR / "content-pipeline-v2.yaml"
CODE_REVIEW_PIPELINE = EXAMPLES_DIR / "code-review-pipeline.yaml"
RESEARCH_PIPELINE = EXAMPLES_DIR / "research-pipeline.yaml"

ALL_TEMPLATES = [CONTENT_PIPELINE, CODE_REVIEW_PIPELINE, RESEARCH_PIPELINE]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    return TemplateEngine()


@pytest.fixture(scope="module")
def content_template(engine):
    return engine.load_template(CONTENT_PIPELINE)


@pytest.fixture(scope="module")
def code_review_template(engine):
    return engine.load_template(CODE_REVIEW_PIPELINE)


@pytest.fixture(scope="module")
def research_template(engine):
    return engine.load_template(RESEARCH_PIPELINE)


@pytest.fixture(scope="module")
def content_raw():
    return yaml.safe_load(CONTENT_PIPELINE.read_text())


@pytest.fixture(scope="module")
def code_review_raw():
    return yaml.safe_load(CODE_REVIEW_PIPELINE.read_text())


@pytest.fixture(scope="module")
def research_raw():
    return yaml.safe_load(RESEARCH_PIPELINE.read_text())


# ---------------------------------------------------------------------------
# 1. Template files exist and load successfully
# ---------------------------------------------------------------------------

class TestTemplatesExistAndLoad:
    def test_content_pipeline_file_exists(self):
        assert CONTENT_PIPELINE.exists(), f"Missing: {CONTENT_PIPELINE}"

    def test_code_review_pipeline_file_exists(self):
        assert CODE_REVIEW_PIPELINE.exists(), f"Missing: {CODE_REVIEW_PIPELINE}"

    def test_research_pipeline_file_exists(self):
        assert RESEARCH_PIPELINE.exists(), f"Missing: {RESEARCH_PIPELINE}"

    def test_content_pipeline_loads(self, content_template):
        assert content_template is not None
        assert content_template.id == "content-pipeline-v2"

    def test_code_review_pipeline_loads(self, code_review_template):
        assert code_review_template is not None
        assert code_review_template.id == "code-review-pipeline"

    def test_research_pipeline_loads(self, research_template):
        assert research_template is not None
        assert research_template.id == "research-pipeline"


# ---------------------------------------------------------------------------
# 2. All templates pass `orch validate` (exit code 0)
# ---------------------------------------------------------------------------

class TestOrchValidatePasses:
    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_validate_exits_0(self, template_path):
        """orch validate should exit 0 for all three example templates."""
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(template_path)])
        assert result.exit_code == 0, (
            f"orch validate failed for {template_path.name} "
            f"(exit {result.exit_code}):\n{result.output}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_validate_no_structural_errors(self, engine, template_path):
        """validate_template() should return no structural errors."""
        template = engine.load_template(template_path)
        errors = engine.validate_template(template)
        assert errors == [], f"{template_path.name} structural errors: {errors}"

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_validate_no_extended_errors(self, engine, template_path):
        """validate_template_extended() should return no errors (warnings OK)."""
        template = engine.load_template(template_path)
        raw_data = yaml.safe_load(template_path.read_text())
        errors, _warnings = engine.validate_template_extended(template, raw_data)
        assert errors == [], f"{template_path.name} extended errors: {errors}"


# ---------------------------------------------------------------------------
# 3. Phase counts are correct
# ---------------------------------------------------------------------------

class TestPhaseCounts:
    def test_content_pipeline_has_7_phases(self, content_template):
        assert len(content_template.phases) == 7, (
            f"Expected 7 phases, got {len(content_template.phases)}"
        )

    def test_code_review_pipeline_has_5_phases(self, code_review_template):
        assert len(code_review_template.phases) == 5, (
            f"Expected 5 phases, got {len(code_review_template.phases)}"
        )

    def test_research_pipeline_has_6_phases(self, research_template):
        assert len(research_template.phases) == 6, (
            f"Expected 6 phases, got {len(research_template.phases)}"
        )

    def test_content_pipeline_phase_ids(self, content_template):
        phase_ids = [p.id for p in content_template.phases]
        expected = ["research", "outline", "draft", "flow-review", "red-team", "apply-fixes", "final-review"]
        assert phase_ids == expected, f"Phase IDs mismatch: {phase_ids}"

    def test_code_review_pipeline_phase_ids(self, code_review_template):
        phase_ids = [p.id for p in code_review_template.phases]
        expected = ["parse", "complexity", "style", "security", "synthesize"]
        assert phase_ids == expected, f"Phase IDs mismatch: {phase_ids}"

    def test_research_pipeline_phase_ids(self, research_template):
        phase_ids = [p.id for p in research_template.phases]
        expected = ["discover", "extract", "cross-reference", "synthesize", "fact-check", "format"]
        assert phase_ids == expected, f"Phase IDs mismatch: {phase_ids}"


# ---------------------------------------------------------------------------
# 4. Dependencies wired correctly
# ---------------------------------------------------------------------------

class TestDependencies:
    # Content pipeline dependencies
    def test_content_research_has_no_deps(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "research")
        assert phase.depends_on == []

    def test_content_outline_has_no_deps(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "outline")
        assert phase.depends_on == []

    def test_content_draft_depends_on_research_and_outline(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "draft")
        assert set(phase.depends_on) == {"research", "outline"}

    def test_content_flow_review_depends_on_draft(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "flow-review")
        assert phase.depends_on == ["draft"]

    def test_content_red_team_depends_on_draft(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "red-team")
        assert phase.depends_on == ["draft"]

    def test_content_apply_fixes_depends_on_draft_flow_and_redteam(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "apply-fixes")
        assert set(phase.depends_on) == {"draft", "flow-review", "red-team"}

    def test_content_final_review_depends_on_apply_fixes(self, content_template):
        phase = next(p for p in content_template.phases if p.id == "final-review")
        assert phase.depends_on == ["apply-fixes"]

    # Code review pipeline dependencies
    def test_code_parse_has_no_deps(self, code_review_template):
        phase = next(p for p in code_review_template.phases if p.id == "parse")
        assert phase.depends_on == []

    def test_code_complexity_depends_on_parse(self, code_review_template):
        phase = next(p for p in code_review_template.phases if p.id == "complexity")
        assert phase.depends_on == ["parse"]

    def test_code_style_depends_on_parse(self, code_review_template):
        phase = next(p for p in code_review_template.phases if p.id == "style")
        assert phase.depends_on == ["parse"]

    def test_code_security_depends_on_parse(self, code_review_template):
        phase = next(p for p in code_review_template.phases if p.id == "security")
        assert phase.depends_on == ["parse"]

    def test_code_synthesize_depends_on_all_three(self, code_review_template):
        phase = next(p for p in code_review_template.phases if p.id == "synthesize")
        assert set(phase.depends_on) == {"complexity", "style", "security"}

    # Research pipeline dependencies
    def test_research_discover_has_no_deps(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "discover")
        assert phase.depends_on == []

    def test_research_extract_depends_on_discover(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "extract")
        assert phase.depends_on == ["discover"]

    def test_research_cross_reference_depends_on_extract(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "cross-reference")
        assert phase.depends_on == ["extract"]

    def test_research_synthesize_depends_on_cross_reference(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "synthesize")
        assert phase.depends_on == ["cross-reference"]

    def test_research_fact_check_depends_on_synthesize(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "fact-check")
        assert phase.depends_on == ["synthesize"]

    def test_research_format_depends_on_synthesize_and_fact_check(self, research_template):
        phase = next(p for p in research_template.phases if p.id == "format")
        assert set(phase.depends_on) == {"synthesize", "fact-check"}


# ---------------------------------------------------------------------------
# 5. Config schemas have required fields
# ---------------------------------------------------------------------------

class TestConfigSchemas:
    def test_content_pipeline_config_schema_has_type(self, content_template):
        assert content_template.config_schema.get("type") == "object"

    def test_content_pipeline_config_schema_has_properties(self, content_template):
        assert "properties" in content_template.config_schema

    def test_content_pipeline_config_schema_required_topic(self, content_template):
        required = content_template.config_schema.get("required", [])
        assert "topic" in required, f"'topic' not in required: {required}"

    def test_content_pipeline_config_schema_has_tone(self, content_template):
        props = content_template.config_schema.get("properties", {})
        assert "tone" in props

    def test_content_pipeline_config_schema_has_word_count(self, content_template):
        props = content_template.config_schema.get("properties", {})
        assert "word_count" in props

    def test_code_review_config_schema_required_diff(self, code_review_template):
        required = code_review_template.config_schema.get("required", [])
        assert "diff" in required, f"'diff' not in required: {required}"

    def test_code_review_config_schema_has_language(self, code_review_template):
        props = code_review_template.config_schema.get("properties", {})
        assert "language" in props

    def test_code_review_config_schema_has_severity_threshold(self, code_review_template):
        props = code_review_template.config_schema.get("properties", {})
        assert "severity_threshold" in props

    def test_research_config_schema_required_topic(self, research_template):
        required = research_template.config_schema.get("required", [])
        assert "topic" in required, f"'topic' not in required: {required}"

    def test_research_config_schema_has_depth(self, research_template):
        props = research_template.config_schema.get("properties", {})
        assert "depth" in props

    def test_research_config_schema_depth_has_enum(self, research_raw):
        depth_prop = research_raw["config_schema"]["properties"]["depth"]
        assert "enum" in depth_prop, "depth property should have enum values"
        assert set(depth_prop["enum"]) == {"quick", "standard", "deep"}

    def test_research_config_schema_has_max_sources(self, research_template):
        props = research_template.config_schema.get("properties", {})
        assert "max_sources" in props


# ---------------------------------------------------------------------------
# 6. Doc fields are present on all templates
# ---------------------------------------------------------------------------

class TestDocFields:
    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_author_is_toscan(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.author == "Toscan", (
            f"{template_path.name} author should be 'Toscan', got {template.author!r}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_version_is_semver(self, engine, template_path):
        import re
        template = engine.load_template(template_path)
        assert re.match(r"^\d+\.\d+\.\d+$", template.version), (
            f"{template_path.name} version {template.version!r} is not semver"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_description_is_non_empty(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.description.strip(), f"{template_path.name} has empty description"

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_use_cases_present_and_non_empty(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.use_cases, f"{template_path.name} has no use_cases"
        assert len(template.use_cases) >= 2, (
            f"{template_path.name} should have at least 2 use_cases"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_tags_present_and_non_empty(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.tags, f"{template_path.name} has no tags"
        assert len(template.tags) >= 3, (
            f"{template_path.name} should have at least 3 tags"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_category_is_non_empty(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.category.strip(), f"{template_path.name} has empty category"

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_example_input_present(self, engine, template_path):
        template = engine.load_template(template_path)
        assert template.example_input, f"{template_path.name} has no example_input"

    def test_content_pipeline_example_input_has_topic(self, content_template):
        assert "topic" in content_template.example_input

    def test_code_review_example_input_has_diff(self, code_review_template):
        assert "diff" in code_review_template.example_input

    def test_research_example_input_has_topic(self, research_template):
        assert "topic" in research_template.example_input

    def test_content_pipeline_version(self, content_template):
        assert content_template.version == "1.0.0"

    def test_code_review_version(self, code_review_template):
        assert code_review_template.version == "1.0.0"

    def test_research_version(self, research_template):
        assert research_template.version == "1.0.0"


# ---------------------------------------------------------------------------
# 7. Phase-level quality checks
# ---------------------------------------------------------------------------

class TestPhaseQuality:
    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_all_phases_have_prompt_templates(self, engine, template_path):
        template = engine.load_template(template_path)
        for phase in template.phases:
            assert phase.prompt_template.strip(), (
                f"{template_path.name}: phase '{phase.id}' has empty prompt_template"
            )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_all_phases_have_descriptions(self, engine, template_path):
        template = engine.load_template(template_path)
        for phase in template.phases:
            assert phase.description.strip(), (
                f"{template_path.name}: phase '{phase.id}' has empty description"
            )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_all_phases_use_sonnet_model_tier(self, engine, template_path):
        template = engine.load_template(template_path)
        for phase in template.phases:
            assert phase.model_tier == "sonnet", (
                f"{template_path.name}: phase '{phase.id}' model_tier is "
                f"{phase.model_tier!r}, expected 'sonnet'"
            )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: p.name)
    def test_all_phases_have_valid_thinking_levels(self, engine, template_path):
        valid = {"off", "low", "medium", "high"}
        template = engine.load_template(template_path)
        for phase in template.phases:
            assert phase.thinking_level in valid, (
                f"{template_path.name}: phase '{phase.id}' thinking_level "
                f"{phase.thinking_level!r} not in {valid}"
            )
