"""Comprehensive tests for the plugin-command importer.

Covers:
- Frontmatter extraction (happy path, missing, malformed)
- Title (H1) extraction
- Section classification (meta vs inputs vs content)
- config_schema generation from Inputs section
- Phase generation with review insertion
- Phase ID de-duplication
- Skill-ref extraction and filtering
- Full round-trip: markdown → YAML → TemplateEngine.load_template → validate
- CLI: ``orch import plugin-command``
- Error handling for malformed inputs
"""

import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from orchestration_engine.importers.plugin_command import (
    META_SECTIONS,
    _classify_section,
    _extract_frontmatter,
    _extract_h1,
    _extract_skill_refs,
    _make_unique_id,
    _parse_document,
    _parse_inputs_section,
    _split_by_h2,
    import_plugin_command,
    import_plugin_command_from_string,
    slugify,
    snake_case,
)
from orchestration_engine.cli import main
from orchestration_engine.templates import TemplateEngine


# ===========================================================================
# Helpers
# ===========================================================================


def _load_yaml(text: str) -> Dict[str, Any]:
    """Parse the generated YAML (strip leading comment lines first)."""
    lines = text.splitlines()
    # Skip leading comment lines
    data_lines = []
    started = False
    for line in lines:
        if not started and line.startswith("#"):
            continue
        started = True
        data_lines.append(line)
    return yaml.safe_load("\n".join(data_lines)) or {}


def _validate(tmp_path: Path, yaml_text: str):
    """Write YAML to a temp file, load it, and run validate_template."""
    p = tmp_path / "template.yaml"
    p.write_text(yaml_text)
    engine = TemplateEngine()
    template = engine.load_template(p)
    errors = engine.validate_template(template)
    return template, errors


# ===========================================================================
# Unit tests: slugify / snake_case
# ===========================================================================


class TestSlugify:
    def test_basic(self):
        assert slugify("Campaign Plan") == "campaign-plan"

    def test_special_chars(self):
        assert slugify("Brand Voice & Tone") == "brand-voice-tone"

    def test_numbers(self):
        assert slugify("Step 1 — Overview") == "step-1-overview"

    def test_leading_trailing(self):
        assert slugify("  --- hello ---  ") == "hello"

    def test_already_slug(self):
        assert slugify("my-pipeline") == "my-pipeline"

    def test_empty(self):
        assert slugify("") == ""


class TestSnakeCase:
    def test_basic(self):
        assert snake_case("Campaign goal") == "campaign_goal"

    def test_with_parens(self):
        # The full strip is done higher up; here we just verify the conversion
        assert snake_case("target audience optional") == "target_audience_optional"

    def test_numbers(self):
        assert snake_case("Field 1") == "field_1"


# ===========================================================================
# Unit tests: frontmatter extraction
# ===========================================================================


class TestExtractFrontmatter:
    def test_happy_path(self):
        doc = textwrap.dedent(
            """\
            ---
            description: Test description
            argument-hint: "<input>"
            tags:
              - marketing
            ---

            # My Title
            """
        )
        fm, body = _extract_frontmatter(doc)
        assert fm["description"] == "Test description"
        assert fm["argument-hint"] == "<input>"
        assert fm["tags"] == ["marketing"]
        assert "# My Title" in body

    def test_no_frontmatter(self):
        doc = "# Just a title\n\nSome body text."
        fm, body = _extract_frontmatter(doc)
        assert fm == {}
        assert body == doc

    def test_empty_frontmatter(self):
        doc = "---\n---\n\n# Title\n"
        fm, body = _extract_frontmatter(doc)
        assert fm == {}
        assert "# Title" in body

    def test_unclosed_frontmatter(self):
        """Without closing ---, treat as no frontmatter."""
        doc = "---\ndescription: test\n\n# Title\n"
        fm, body = _extract_frontmatter(doc)
        assert fm == {}
        assert body == doc

    def test_malformed_yaml(self):
        doc = "---\n: bad: yaml: [\n---\n\n# Title\n"
        with pytest.raises(ValueError, match="[Ff]rontmatter"):
            _extract_frontmatter(doc)


# ===========================================================================
# Unit tests: H1 extraction
# ===========================================================================


class TestExtractH1:
    def test_finds_first_h1(self):
        body = "## Intro\n\n# My Title\n\nSome text.\n"
        title, rest = _extract_h1(body)
        assert title == "My Title"
        assert "# My Title" not in rest

    def test_no_h1(self):
        body = "## Section\n\nSome text.\n"
        title, rest = _extract_h1(body)
        assert title == ""
        assert rest == body

    def test_h1_with_extra_spaces(self):
        body = "#  Padded Title  \n"
        title, _ = _extract_h1(body)
        assert title == "Padded Title"


# ===========================================================================
# Unit tests: H2 section splitting
# ===========================================================================


class TestSplitByH2:
    def test_basic_split(self):
        body = textwrap.dedent(
            """\
            ## Section One
            Body of one.
            More body.

            ## Section Two
            Body of two.
            """
        )
        sections = _split_by_h2(body)
        assert len(sections) == 2
        assert sections[0].heading == "Section One"
        assert "Body of one." in sections[0].body
        assert sections[1].heading == "Section Two"
        assert "Body of two." in sections[1].body

    def test_text_before_first_h2_is_ignored(self):
        body = "Intro text\n\n## Section\nContent.\n"
        sections = _split_by_h2(body)
        assert len(sections) == 1
        assert sections[0].heading == "Section"

    def test_no_sections(self):
        body = "Just some text without any H2.\n"
        assert _split_by_h2(body) == []

    def test_h3_inside_section_is_included_in_body(self):
        body = "## Phase One\n### Sub A\n- item\n"
        sections = _split_by_h2(body)
        assert "### Sub A" in sections[0].body
        assert "- item" in sections[0].body


# ===========================================================================
# Unit tests: section classification
# ===========================================================================


class TestClassifySection:
    @pytest.mark.parametrize(
        "heading,expected",
        [
            ("Trigger", "meta"),
            ("trigger", "meta"),
            ("TRIGGER", "meta"),
            ("Inputs", "inputs"),
            ("INPUTS", "inputs"),
            ("Output", "meta"),
            ("Outputs", "meta"),
            ("Notes", "meta"),
            ("Instructions", "meta"),
            ("Campaign Brief Structure", "content"),
            ("Brand Voice", "content"),
            ("Content Generation by Type", "content"),
            ("SEO Considerations", "content"),
            ("Review Process", "content"),
        ],
    )
    def test_classification(self, heading, expected):
        assert _classify_section(heading) == expected


# ===========================================================================
# Unit tests: config_schema parsing
# ===========================================================================


class TestParseInputsSection:
    def test_required_fields(self):
        body = textwrap.dedent(
            """\
            Gather the following:

            1. **Campaign goal** — the primary objective
            2. **Target audience** — who the campaign is aimed at
            """
        )
        schema = _parse_inputs_section(body)
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "campaign_goal" in props
        assert "target_audience" in props
        assert props["campaign_goal"]["type"] == "string"
        assert "campaign_goal" in schema.get("required", [])
        assert "target_audience" in schema.get("required", [])

    def test_optional_field(self):
        body = textwrap.dedent(
            """\
            1. **Name** — your name
            2. **Budget range** (optional) — approximate budget
            """
        )
        schema = _parse_inputs_section(body)
        required = schema.get("required", [])
        assert "name" in required
        assert "budget_range" not in required
        assert "budget_range" in schema["properties"]

    def test_empty_inputs(self):
        """Empty Inputs section → synthetic fallback schema."""
        schema = _parse_inputs_section("Gather info from the user.")
        assert schema["type"] == "object"
        assert "properties" in schema
        assert len(schema["properties"]) >= 1

    def test_description_stripping(self):
        body = "1. **Topic** — main subject of the content\n"
        schema = _parse_inputs_section(body)
        assert schema["properties"]["topic"]["description"] == "main subject of the content"

    def test_em_dash_and_hyphen_variants(self):
        body = textwrap.dedent(
            """\
            1. **Field one** — em-dash variant
            2. **Field two** - hyphen variant
            """
        )
        schema = _parse_inputs_section(body)
        assert "field_one" in schema["properties"]
        assert "field_two" in schema["properties"]


# ===========================================================================
# Unit tests: phase ID de-duplication
# ===========================================================================


class TestMakeUniqueId:
    def test_first_use_unchanged(self):
        seen: Dict[str, int] = {}
        assert _make_unique_id("phase", seen) == "phase"
        assert seen["phase"] == 1

    def test_collision_gets_suffix(self):
        seen: Dict[str, int] = {}
        _make_unique_id("phase", seen)
        second = _make_unique_id("phase", seen)
        assert second == "phase-2"

    def test_multiple_collisions(self):
        seen: Dict[str, int] = {}
        ids = [_make_unique_id("phase", seen) for _ in range(4)]
        assert ids == ["phase", "phase-2", "phase-3", "phase-4"]


# ===========================================================================
# Unit tests: skill-ref extraction
# ===========================================================================


class TestExtractSkillRefs:
    def test_no_base_dir_returns_empty(self):
        text = "[brand-voice skill](../skills/brand-voice/SKILL.md)"
        assert _extract_skill_refs(text, base_dir=None) == []

    def test_resolves_existing_skill(self, tmp_path):
        skill_file = tmp_path / "skills" / "brand-voice" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("# Brand Voice Skill")

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()

        text = "[brand-voice skill](../skills/brand-voice/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=commands_dir)
        assert len(refs) == 1
        assert refs[0] == str(skill_file.resolve())

    def test_missing_skill_not_included(self, tmp_path):
        text = "[missing skill](../skills/nonexistent/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=tmp_path)
        assert refs == []

    def test_deduplication(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Skill")
        text = "[a](./SKILL.md) and [b](./SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=tmp_path)
        assert len(refs) == 1


# ===========================================================================
# Integration: parse_document
# ===========================================================================


class TestParseDocument:
    _SAMPLE = textwrap.dedent(
        """\
        ---
        description: Generate a campaign brief
        argument-hint: "<campaign objective>"
        tags:
          - marketing
        ---

        # Campaign Plan

        > Optional preamble.

        Intro text.

        ## Trigger

        User runs /campaign-plan.

        ## Inputs

        1. **Campaign goal** — the primary objective
        2. **Timeline** — campaign duration
        3. **Budget range** (optional) — approximate budget

        ## Campaign Brief Structure

        ### 1. Overview
        - Campaign name suggestion

        ### 2. Audience
        - Primary segment description

        ## Output

        Present the full campaign brief.
        """
    )

    def test_parses_title(self):
        parsed = _parse_document(self._SAMPLE)
        assert parsed.title == "Campaign Plan"

    def test_parses_frontmatter(self):
        parsed = _parse_document(self._SAMPLE)
        assert parsed.frontmatter["description"] == "Generate a campaign brief"
        assert "marketing" in parsed.frontmatter["tags"]

    def test_has_correct_sections(self):
        parsed = _parse_document(self._SAMPLE)
        headings = [s.heading for s in parsed.sections]
        assert "Trigger" in headings
        assert "Inputs" in headings
        assert "Campaign Brief Structure" in headings
        assert "Output" in headings

    def test_raises_on_missing_h1(self):
        bad = "## Section\n\nNo H1 here.\n"
        with pytest.raises(ValueError, match="H1"):
            _parse_document(bad)


# ===========================================================================
# Integration: full round-trip
# ===========================================================================


MINIMAL_COMMAND = textwrap.dedent(
    """\
    ---
    description: A minimal test command
    ---

    # Minimal Pipeline

    ## Trigger

    Run /minimal.

    ## Inputs

    1. **Topic** — the main subject

    ## Generate Content

    Write content about the topic.

    ## Output

    Present the content.
    """
)

MULTI_SECTION_COMMAND = textwrap.dedent(
    """\
    ---
    description: Multi-section test command
    argument-hint: "<subject>"
    tags:
      - test
    ---

    # Multi Phase Pipeline

    > Preamble.

    ## Trigger

    User runs /multi-phase.

    ## Inputs

    1. **Subject** — what to process
    2. **Tone** (optional) — the desired tone

    ## Research

    ### Gather Sources
    Find relevant sources for {subject}.
    - [ ] Check primary sources
    - [ ] Verify data

    ## Draft

    Write a first draft based on the research.

    ## Review and Polish

    Review the draft for quality.

    ## Output

    Deliver the final document.
    """
)


class TestRoundTrip:
    """Full markdown → YAML → load → validate cycle."""

    def test_minimal_generates_valid_yaml(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        template, errors = _validate(tmp_path, yaml_text)
        assert errors == [], f"Structural errors: {errors}"

    def test_minimal_template_id(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["id"] == "minimal-pipeline"

    def test_minimal_has_description(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["description"]

    def test_minimal_has_author(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["author"]

    def test_minimal_has_version(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["version"] == "1.0.0"

    def test_minimal_config_schema_type(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["config_schema"]["type"] == "object"
        assert "properties" in data["config_schema"]

    def test_minimal_config_schema_has_topic_field(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        props = data["config_schema"]["properties"]
        assert "topic" in props

    def test_minimal_phases(self):
        """Should have 2 phases: generate-content + its review."""
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert len(data["phases"]) == 2

    def test_minimal_review_phase_inserted(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        phase_ids = [p["id"] for p in data["phases"]]
        assert "generate-content" in phase_ids
        assert "generate-content-review" in phase_ids

    def test_review_phase_has_opus_tier(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        review = next(p for p in data["phases"] if p["id"] == "generate-content-review")
        assert review["model_tier"] == "opus"

    def test_review_phase_thinking_medium(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        review = next(p for p in data["phases"] if p["id"] == "generate-content-review")
        assert review["thinking_level"] == "medium"

    def test_review_phase_depends_on_content_phase(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        review = next(p for p in data["phases"] if p["id"] == "generate-content-review")
        assert "generate-content" in review["depends_on"]

    def test_content_phase_has_sonnet_tier(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        content = next(p for p in data["phases"] if p["id"] == "generate-content")
        assert content["model_tier"] == "sonnet"

    def test_multi_section_phase_count(self):
        """3 content sections → 6 phases (3 content + 3 review)."""
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        assert len(data["phases"]) == 6

    def test_multi_section_linear_deps(self):
        """Each phase depends on the previous one in a linear chain."""
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        phases = data["phases"]

        # First content phase has no deps
        assert phases[0]["depends_on"] == []

        # review-1 depends on content-1
        assert phases[0]["id"] in phases[1]["depends_on"]

        # content-2 depends on review-1
        assert phases[1]["id"] in phases[2]["depends_on"]

    def test_multi_section_no_duplicate_ids(self):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        ids = [p["id"] for p in data["phases"]]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_multi_section_validates(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        template, errors = _validate(tmp_path, yaml_text)
        assert errors == [], f"Structural errors: {errors}"

    def test_optional_field_not_in_required(self):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        required = data["config_schema"].get("required", [])
        assert "tone" not in required
        assert "subject" in required

    def test_prompt_template_contains_input_placeholder(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        content = next(p for p in data["phases"] if p["id"] == "generate-content")
        assert "{input}" in content["prompt_template"]

    def test_prompt_template_contains_previous_output_placeholder(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        content = next(p for p in data["phases"] if p["id"] == "generate-content")
        assert "{previous_output}" in content["prompt_template"]

    def test_tags_propagated_from_frontmatter(self):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        assert "test" in data["tags"]

    def test_use_cases_populated(self):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        data = _load_yaml(yaml_text)
        assert len(data["use_cases"]) >= 1

    def test_example_input_populated(self):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        data = _load_yaml(yaml_text)
        assert data["example_input"]


# ===========================================================================
# Real plugin command files (parametrize if available)
# ===========================================================================


REAL_COMMANDS = [
    "/home/toscan/knowledge-work-plugins/marketing/commands/campaign-plan.md",
    "/home/toscan/knowledge-work-plugins/marketing/commands/draft-content.md",
    "/home/toscan/knowledge-work-plugins/marketing/commands/brand-review.md",
]


@pytest.mark.parametrize("filepath", REAL_COMMANDS)
def test_real_command_round_trip(filepath, tmp_path):
    """Each real plugin command file must produce a valid template."""
    p = Path(filepath)
    if not p.exists():
        pytest.skip(f"Real command file not found: {filepath}")

    yaml_text = import_plugin_command(p, author="test-suite")
    template, errors = _validate(tmp_path, yaml_text)
    assert errors == [], f"{filepath}: structural errors: {errors}"


@pytest.mark.parametrize("filepath", REAL_COMMANDS)
def test_real_command_has_review_phases(filepath):
    """Each real command must have at least one review phase."""
    p = Path(filepath)
    if not p.exists():
        pytest.skip(f"Real command file not found: {filepath}")

    yaml_text = import_plugin_command(p)
    data = _load_yaml(yaml_text)
    review_phases = [ph for ph in data["phases"] if ph.get("model_tier") == "opus"]
    assert len(review_phases) >= 1, "Expected at least one review phase"


@pytest.mark.parametrize("filepath", REAL_COMMANDS)
def test_real_command_model_tiers_valid(filepath):
    """All phases must have valid model_tier values."""
    p = Path(filepath)
    if not p.exists():
        pytest.skip(f"Real command file not found: {filepath}")

    yaml_text = import_plugin_command(p)
    data = _load_yaml(yaml_text)
    valid_tiers = {"haiku", "sonnet", "opus"}
    for phase in data["phases"]:
        assert phase["model_tier"] in valid_tiers, (
            f"Phase '{phase['id']}' has invalid model_tier '{phase['model_tier']}'"
        )


# ===========================================================================
# Error handling
# ===========================================================================


class TestErrorHandling:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_plugin_command(tmp_path / "does-not-exist.md")

    def test_no_h1_raises(self):
        bad = "## Section\n\nNo H1 here.\n"
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string(bad)

    def test_malformed_frontmatter_raises(self):
        bad = "---\n: broken: yaml: [[\n---\n\n# Title\n## Section\n\nContent.\n"
        with pytest.raises(ValueError, match="[Ff]rontmatter"):
            import_plugin_command_from_string(bad)

    def test_no_content_sections_produces_minimal_schema(self):
        """Document with only meta sections → valid template with 0 phases."""
        doc = textwrap.dedent(
            """\
            ---
            description: No phases test
            ---

            # Empty Pipeline

            ## Trigger

            Run /empty.

            ## Inputs

            1. **Topic** — the subject

            ## Output

            Nothing.
            """
        )
        yaml_text = import_plugin_command_from_string(doc)
        data = _load_yaml(yaml_text)
        assert data["id"] == "empty-pipeline"
        assert data["config_schema"]["type"] == "object"
        # No content phases generated
        assert data["phases"] == []

    def test_duplicate_section_headings(self):
        """Duplicate H2 headings get de-duplicated IDs."""
        doc = textwrap.dedent(
            """\
            ---
            description: Dup test
            ---

            # Dup Pipeline

            ## Step One
            Content A.

            ## Step One
            Content B.

            ## Output
            Done.
            """
        )
        yaml_text = import_plugin_command_from_string(doc)
        data = _load_yaml(yaml_text)
        ids = [p["id"] for p in data["phases"]]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"


# ===========================================================================
# CLI tests
# ===========================================================================


def _cli(*args):
    runner = CliRunner()
    return runner.invoke(main, list(args), catch_exceptions=False)


class TestCLI:
    def test_import_help(self):
        result = _cli("import", "--help")
        assert result.exit_code == 0
        assert "plugin-command" in result.output.lower() or "import" in result.output.lower()

    def test_plugin_command_help(self):
        result = _cli("import", "plugin-command", "--help")
        assert result.exit_code == 0
        assert "COMMAND_FILE" in result.output or "command_file" in result.output.lower()

    def test_dry_run_prints_yaml(self, tmp_path):
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        result = _cli("import", "plugin-command", str(cmd_file), "--dry-run")
        assert result.exit_code == 0
        assert "id:" in result.output
        assert "phases:" in result.output

    def test_writes_output_file(self, tmp_path):
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        out_file = tmp_path / "out.yaml"
        result = _cli("import", "plugin-command", str(cmd_file), "--output", str(out_file))
        assert result.exit_code == 0
        assert out_file.exists()
        data = yaml.safe_load(out_file.read_text())
        assert data["id"] == "minimal-pipeline"

    def test_default_output_filename(self, tmp_path, monkeypatch):
        """Without --output, file is written to <template-id>.yaml."""
        monkeypatch.chdir(tmp_path)
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        result = _cli("import", "plugin-command", str(cmd_file))
        assert result.exit_code == 0
        expected = tmp_path / "minimal-pipeline.yaml"
        assert expected.exists(), f"Expected {expected} to be created"

    def test_custom_author(self, tmp_path):
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        out_file = tmp_path / "out.yaml"
        _cli(
            "import", "plugin-command",
            str(cmd_file),
            "--output", str(out_file),
            "--author", "test-author",
        )
        data = yaml.safe_load(out_file.read_text())
        assert data["author"] == "test-author"

    def test_validate_flag_runs_validation(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        out_file = tmp_path / "out.yaml"
        result = _cli(
            "import", "plugin-command",
            str(cmd_file),
            "--output", str(out_file),
            "--validate",
        )
        # Should complete without error (valid template)
        assert result.exit_code == 0
        # Validate output should mention structural checks
        assert "Structural" in result.output or "valid" in result.output.lower()

    def test_missing_input_file_fails(self):
        result = CliRunner().invoke(
            main,
            ["import", "plugin-command", "/nonexistent/file.md"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_dry_run_does_not_write_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_file = tmp_path / "test.md"
        cmd_file.write_text(MINIMAL_COMMAND)
        _cli("import", "plugin-command", str(cmd_file), "--dry-run")
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert yaml_files == [], "dry-run should not write any file"


# ===========================================================================
# Extended validation: generated YAML must pass orch validate --fix=False
# ===========================================================================


class TestExtendedValidation:
    """Ensure generated templates pass the extended linting checks in
    TemplateEngine.validate_template_extended()."""

    def _run_extended(self, yaml_text: str, tmp_path: Path):
        p = tmp_path / "tpl.yaml"
        p.write_text(yaml_text)
        engine = TemplateEngine()
        template = engine.load_template(p)
        import yaml as _yaml
        with open(p) as fh:
            raw = _yaml.safe_load(fh)
        errs, warns = engine.validate_template_extended(template, raw)
        return errs, warns

    def test_no_extended_errors_minimal(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        errs, _ = self._run_extended(yaml_text, tmp_path)
        assert errs == [], f"Extended errors: {errs}"

    def test_no_extended_errors_multi_section(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MULTI_SECTION_COMMAND)
        errs, _ = self._run_extended(yaml_text, tmp_path)
        assert errs == [], f"Extended errors: {errs}"

    def test_valid_model_tiers_no_warnings(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        _, warns = self._run_extended(yaml_text, tmp_path)
        tier_warnings = [w for w in warns if "model_tier" in w]
        assert tier_warnings == [], f"Unexpected model_tier warnings: {tier_warnings}"

    def test_valid_thinking_levels_no_warnings(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL_COMMAND)
        _, warns = self._run_extended(yaml_text, tmp_path)
        level_warnings = [w for w in warns if "thinking_level" in w]
        assert level_warnings == [], f"Unexpected thinking_level warnings: {level_warnings}"
