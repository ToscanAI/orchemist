"""Tests for the rubric_generator module — covers all 10 acceptance criteria.

Run with:
    pytest tests/test_rubric_generator.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from orchestration_engine.rubric_generator import (
    CriteriaTable,
    SkillData,
    _build_criteria_list,
    _clean_bold,
    _extract_checklist_items,
    _extract_do_dont_from_sections,
    _extract_tables,
    _extract_we_are_blocks,
    _parse_frontmatter,
    _strip_code_blocks,
    generate_rubric_file,
    generate_rubric_text,
    generate_yaml,
    parse_skill,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_SKILL = textwrap.dedent("""\
    ---
    name: test-skill
    description: A minimal test skill.
    ---

    # Test Skill

    Some introduction text.
""")

CHECKLIST_SKILL = textwrap.dedent("""\
    ---
    name: data-validation
    description: QA an analysis before sharing.
    ---

    # Data Validation Skill

    ## Data Quality Checks

    - [ ] **Source verification**: Confirmed which tables were used.
    - [ ] **Freshness**: Data is current enough.
    - [x] **Completeness**: No unexpected gaps.

    ### Sub-section Checks

        - [ ] **Deep check**: A sub-section item indented 4 spaces.
""")

TERMINOLOGY_TABLE_SKILL = textwrap.dedent("""\
    ---
    name: brand-voice
    description: Apply brand voice.
    ---

    # Brand Voice Skill

    ## Terminology Management

    ### Preferred Terms

    | Use This | Not This | Notes |
    |----------|----------|-------|
    | sign up (verb) | signup (verb) | noun form |
    | log in (verb) | login (verb) | noun form |
    | email | e-mail | No hyphen |
""")

WE_ARE_SKILL = textwrap.dedent("""\
    ---
    name: brand-voice
    description: Brand voice skill.
    ---

    # Brand Voice

    **Approachable**
    - **We are**: friendly, clear, jargon-free
    - **We are not**: dumbed-down, overly casual
    - **This sounds like**: Here's how to get started.

    **Bold**
    - **We are**: confident, direct, assertive
    - **We are not**: arrogant or aggressive
""")

CRITERIA_TABLE_SKILL = textwrap.dedent("""\
    ---
    name: data-validation
    description: QA data analyses.
    ---

    # Data Validation

    ## Result Sanity Checking

    | Metric Type | Sanity Check |
    |---|---|
    | User counts | Does this match known MAU/DAU figures? |
    | Revenue | Is this in the right order of magnitude? |
    | Conversion rates | Is this between 0% and 100%? |
""")

FULL_SKILL = textwrap.dedent("""\
    ---
    name: full-skill
    description: A comprehensive skill with all element types.
    ---

    # Full Skill

    ## Pre-Delivery Checklist

    - [ ] **Source verification**: Confirmed data sources.
    - [ ] **Freshness**: Data is current.

    ## Terminology

    | Use This | Not This | Notes |
    |----------|----------|-------|
    | sign up | signup | noun form |

    **Approachable**
    - **We are**: friendly and clear
    - **We are not**: dumbed-down

    ## Sanity Checks

    | Metric | Check |
    |--------|-------|
    | Revenue | In right order of magnitude? |
""")

MALFORMED_SKILL = textwrap.dedent("""\
    ---
    name: malformed-skill
    description: Has some malformed elements.
    ---

    # Malformed

    ## Table with one row only (no data)

    | Col1 | Col2 |
    |------|------|

    ## Normal checklist

    - [ ] **Valid item**: This should parse fine.

    Normal paragraph without any structure.
""")

NO_FRONTMATTER_SKILL = textwrap.dedent("""\
    # Skill Without Frontmatter

    ## Checklist

    - [ ] **Item one**: First item.
    - [ ] **Item two**: Second item.
""")

CODE_BLOCK_SKILL = textwrap.dedent("""\
    ---
    name: code-skill
    description: Skill with code blocks.
    ---

    # Code Skill

    ## Checklist

    - [ ] **Real item**: This should appear.

    ```sql
    -- This is SQL code
    SELECT COUNT(*) FROM table;
    - [ ] NOT A CHECKLIST ITEM (inside code block)
    ```

    - [ ] **Another real item**: Also captured.
""")


# ---------------------------------------------------------------------------
# Helper: write a temp skill file
# ---------------------------------------------------------------------------

def write_skill(tmp_path: Path, content: str, filename: str = "SKILL.md") -> Path:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Unit: internal helpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:
    def test_parse_frontmatter_valid(self) -> None:
        fields, body = _parse_frontmatter("---\nname: foo\ndescription: bar\n---\n\nBody text")
        assert fields["name"] == "foo"
        assert fields["description"] == "bar"
        assert "Body text" in body

    def test_parse_frontmatter_missing(self) -> None:
        fields, body = _parse_frontmatter("No frontmatter here")
        assert fields == {}
        assert "No frontmatter here" in body

    def test_clean_bold_with_label(self) -> None:
        assert _clean_bold("**Source verification**: Confirmed tables.") == "Source verification: Confirmed tables."

    def test_clean_bold_without_label(self) -> None:
        assert _clean_bold("plain text") == "plain text"

    def test_clean_bold_label_only_no_colon(self) -> None:
        # Bold without trailing colon/space is not cleaned (no checklist label pattern)
        assert _clean_bold("**Just a label**") == "**Just a label**"

    def test_clean_bold_label_with_colon(self) -> None:
        # Bold with colon is cleaned (the normal checklist item pattern)
        assert _clean_bold("**Label**: description") == "Label: description"

    def test_strip_code_blocks(self) -> None:
        text = "before\n```\n- [ ] NOT AN ITEM\n```\nafter"
        result = _strip_code_blocks(text)
        assert "NOT AN ITEM" not in result
        assert "before" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# Unit: checklist extraction (AC-5)
# ---------------------------------------------------------------------------

class TestChecklistExtraction:
    def test_basic_items_extracted(self) -> None:
        items = _extract_checklist_items(CHECKLIST_SKILL)
        texts = [i["text"] for i in items]
        assert any("Source verification" in t for t in texts)
        assert any("Freshness" in t for t in texts)
        assert any("Completeness" in t for t in texts)

    def test_completed_items_included(self) -> None:
        # [x] items must also be captured
        items = _extract_checklist_items(CHECKLIST_SKILL)
        texts = [i["text"] for i in items]
        assert any("Completeness" in t for t in texts)

    def test_section_tagging(self) -> None:
        items = _extract_checklist_items(CHECKLIST_SKILL)
        for item in items:
            if "Source verification" in item["text"]:
                assert item["section"] == "Data Quality Checks"

    def test_sub_section_items_included_with_section_tag(self) -> None:
        """AC-5: sub-section items are included and tagged with their parent section."""
        items = _extract_checklist_items(CHECKLIST_SKILL)
        sub = [i for i in items if "Deep check" in i["text"]]
        # Sub items should be captured (AC-5)
        assert len(sub) >= 1
        # Tagged with the last seen section heading
        assert sub[0]["section"] != ""

    def test_items_in_code_blocks_excluded(self) -> None:
        items = _extract_checklist_items(_strip_code_blocks(CODE_BLOCK_SKILL))
        texts = [i["text"] for i in items]
        assert not any("NOT A CHECKLIST ITEM" in t for t in texts)
        assert any("Real item" in t for t in texts)
        assert any("Another real item" in t for t in texts)

    def test_bold_label_stripped(self) -> None:
        items = _extract_checklist_items(CHECKLIST_SKILL)
        # No ** should remain in extracted text
        for item in items:
            assert "**" not in item["text"]


# ---------------------------------------------------------------------------
# Unit: DO/DONT from section headings
# ---------------------------------------------------------------------------

class TestDosDontsFromSections:
    _SECTION_SKILL = textwrap.dedent("""\
        ## Always Do This

        - Use version control
        - Write tests

        ## Avoid This

        - Skip documentation
        - Hard-code credentials
    """)

    def test_do_items_extracted(self) -> None:
        do_items, _ = _extract_do_dont_from_sections(self._SECTION_SKILL)
        assert "Use version control" in do_items

    def test_dont_items_extracted(self) -> None:
        _, dont_items = _extract_do_dont_from_sections(self._SECTION_SKILL)
        assert "Skip documentation" in dont_items


# ---------------------------------------------------------------------------
# Unit: DO/DONT from terminology tables (AC-6)
# ---------------------------------------------------------------------------

class TestTerminologyTableExtraction:
    def test_terminology_pairs_extracted(self) -> None:
        warns: list = []
        _, pairs = _extract_tables(TERMINOLOGY_TABLE_SKILL, warns)
        assert len(pairs) == 3
        do_vals = [p["do"] for p in pairs]
        dont_vals = [p["dont"] for p in pairs]
        assert "sign up (verb)" in do_vals
        assert "signup (verb)" in dont_vals
        assert "email" in do_vals
        assert "e-mail" in dont_vals

    def test_terminology_table_not_in_criteria_tables(self) -> None:
        warns: list = []
        tables, _ = _extract_tables(TERMINOLOGY_TABLE_SKILL, warns)
        # Terminology table must NOT be in criteria_tables
        assert all("Use This" not in t.columns for t in tables)

    def test_terminology_pairs_have_source(self) -> None:
        warns: list = []
        _, pairs = _extract_tables(TERMINOLOGY_TABLE_SKILL, warns)
        assert all(p.get("source") == "terminology_table" for p in pairs)


# ---------------------------------------------------------------------------
# Unit: DO/DONT from attribute blocks (AC-7)
# ---------------------------------------------------------------------------

class TestWeAreBlockExtraction:
    def test_we_are_pairs_extracted(self) -> None:
        pairs = _extract_we_are_blocks(WE_ARE_SKILL)
        assert len(pairs) == 2
        do_vals = [p["do"] for p in pairs]
        dont_vals = [p["dont"] for p in pairs]
        assert "friendly, clear, jargon-free" in do_vals
        assert "dumbed-down, overly casual" in dont_vals

    def test_we_are_pairs_have_source(self) -> None:
        pairs = _extract_we_are_blocks(WE_ARE_SKILL)
        assert all(p["source"] == "attribute_block" for p in pairs)

    def test_unpaired_we_are_ignored(self) -> None:
        text = "- **We are**: confident\n- **This sounds like**: example"
        pairs = _extract_we_are_blocks(text)
        assert len(pairs) == 0  # no "We are not" to pair with


# ---------------------------------------------------------------------------
# Unit: criteria table extraction (AC-8)
# ---------------------------------------------------------------------------

class TestCriteriaTableExtraction:
    def test_criteria_tables_extracted(self) -> None:
        warns: list = []
        tables, _ = _extract_tables(CRITERIA_TABLE_SKILL, warns)
        assert len(tables) == 1
        tbl = tables[0]
        assert "Metric Type" in tbl.columns
        assert "Sanity Check" in tbl.columns
        assert len(tbl.rows) == 3

    def test_table_rows_have_correct_data(self) -> None:
        warns: list = []
        tables, _ = _extract_tables(CRITERIA_TABLE_SKILL, warns)
        row_data = [row[0] for row in tables[0].rows]
        assert "User counts" in row_data
        assert "Revenue" in row_data

    def test_table_without_data_rows_skipped(self) -> None:
        warns: list = []
        tables, _ = _extract_tables(MALFORMED_SKILL, warns)
        # The table with only separator and no data rows should be skipped
        for tbl in tables:
            assert len(tbl.rows) > 0

    def test_table_named_from_heading(self) -> None:
        warns: list = []
        tables, _ = _extract_tables(CRITERIA_TABLE_SKILL, warns)
        assert any("Sanity" in t.name or "Result" in t.name for t in tables)


# ---------------------------------------------------------------------------
# Unit: parse_skill (integration)
# ---------------------------------------------------------------------------

class TestParseSkill:
    def test_parse_minimal_skill(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, MINIMAL_SKILL)
        data = parse_skill(p)
        assert data.skill_name == "test-skill"
        assert "minimal test skill" in data.description.lower()

    def test_parse_full_skill(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        assert data.skill_name == "full-skill"
        assert len(data.checklist_items) == 2
        # Terminology pairs from table
        term_pairs = [x for x in data.do_dont_pairs if x.get("source") == "terminology_table"]
        assert len(term_pairs) >= 1
        # Attribute block pairs
        attr_pairs = [x for x in data.do_dont_pairs if x.get("source") == "attribute_block"]
        assert len(attr_pairs) >= 1
        # Criteria tables
        assert len(data.criteria_tables) >= 1

    def test_name_derived_from_filename_when_no_frontmatter(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, NO_FRONTMATTER_SKILL, filename="my-skill.md")
        data = parse_skill(p)
        assert data.skill_name == "my-skill"
        assert len(data.warnings) > 0
        assert "derived from filename" in data.warnings[0]

    def test_source_file_is_absolute(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, MINIMAL_SKILL)
        data = parse_skill(p)
        assert Path(data.source_file).is_absolute()

    def test_code_blocks_not_parsed(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, CODE_BLOCK_SKILL)
        data = parse_skill(p)
        texts = [item["text"] for item in data.checklist_items]
        assert not any("NOT A CHECKLIST ITEM" in t for t in texts)


# ---------------------------------------------------------------------------
# AC-4: Missing file handling
# ---------------------------------------------------------------------------

class TestMissingFileHandling:
    def test_raises_value_error_for_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="File not found"):
            generate_rubric_file(tmp_path / "nonexistent.md")

    def test_raises_value_error_for_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            parse_skill(p)

    def test_raises_value_error_for_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="directory"):
            generate_rubric_file(tmp_path)

    def test_cli_exits_1_for_missing_file(self, tmp_path: Path) -> None:
        """AC-4: CLI must exit 1 and print error for missing file."""
        from orchestration_engine.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["rubric", "generate", str(tmp_path / "nonexistent.md")])
        assert result.exit_code == 1
        assert "nonexistent.md" in result.output or "nonexistent.md" in (result.stderr or "")


# ---------------------------------------------------------------------------
# AC-9: Rubric text quality
# ---------------------------------------------------------------------------

class TestRubricTextQuality:
    def test_four_required_sections_present(self, tmp_path: Path) -> None:
        """AC-9: Preamble, Scoring Scale, Specific Checks, Output Format."""
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Scoring Scale" in rubric
        assert "Specific Checks" in rubric
        assert "Output Format" in rubric
        # Preamble: the rubric starts with a title/intro paragraph
        assert "Quality Rubric" in rubric or "You are evaluating" in rubric

    def test_score_format_in_output_section(self, tmp_path: Path) -> None:
        """AC-9: 'Score: [0.0-1.0]' must appear so LLMJudgeGrader._SCORE_RE can match."""
        import re
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Score: [0.0-1.0]" in rubric

    def test_specific_checks_contains_derived_content(self, tmp_path: Path) -> None:
        """AC-9: Specific Checks must contain at least one check from the skill."""
        p = write_skill(tmp_path, CHECKLIST_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        # Find the Specific Checks section
        assert "Source verification" in rubric or "Freshness" in rubric

    def test_rubric_text_non_empty(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, MINIMAL_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert len(rubric) > 100

    def test_do_dont_pairs_in_rubric(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, TERMINOLOGY_TABLE_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "sign up" in rubric
        assert "signup" in rubric

    def test_we_are_pairs_in_rubric(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, WE_ARE_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "friendly, clear, jargon-free" in rubric
        assert "dumbed-down" in rubric


# ---------------------------------------------------------------------------
# AC-10: Valid YAML output
# ---------------------------------------------------------------------------

class TestValidYamlOutput:
    def test_yaml_parses_without_exception(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        yaml_str = generate_yaml(data)
        parsed = yaml.safe_load(yaml_str)
        assert parsed is not None

    def test_required_top_level_keys(self, tmp_path: Path) -> None:
        """AC-10: name, generated_from, generated_at, rubric, criteria."""
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert "name" in parsed
        assert "generated_from" in parsed
        assert "generated_at" in parsed
        assert "rubric" in parsed
        assert "criteria" in parsed

    def test_name_is_non_empty(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert isinstance(parsed["name"], str)
        assert len(parsed["name"]) > 0

    def test_rubric_is_non_empty_string(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert isinstance(parsed["rubric"], str)
        assert len(parsed["rubric"]) > 0

    def test_generated_from_is_absolute_path(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert Path(parsed["generated_from"]).is_absolute()

    def test_criteria_is_list(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert isinstance(parsed["criteria"], list)

    def test_criteria_contains_checklist_and_do_dont(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        types = {c["type"] for c in parsed["criteria"]}
        assert "checklist" in types
        assert "do_dont" in types

    def test_minimal_skill_produces_valid_yaml(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, MINIMAL_SKILL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert parsed["name"] == "test-skill"


# ---------------------------------------------------------------------------
# AC-2: Basic invocation (file written in cwd, exit 0)
# ---------------------------------------------------------------------------

class TestBasicInvocation:
    def test_basic_invocation_creates_file(self, tmp_path: Path) -> None:
        """AC-2: exit 0 and rubric.yaml written in current directory."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, FULL_SKILL)
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(main, ["rubric", "generate", str(p)])
            assert result.exit_code == 0, result.output
            assert Path("full-skill-rubric.yaml").exists()

    def test_output_file_is_valid_yaml(self, tmp_path: Path) -> None:
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, FULL_SKILL)
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(main, ["rubric", "generate", str(p)])
            parsed = yaml.safe_load(Path("full-skill-rubric.yaml").read_text())
            assert "rubric" in parsed


# ---------------------------------------------------------------------------
# AC-3: Custom output path with parent directory creation
# ---------------------------------------------------------------------------

class TestCustomOutputPath:
    def test_custom_output_path_used(self, tmp_path: Path) -> None:
        """AC-3: file written to --output path, not default name."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, FULL_SKILL)
        out = tmp_path / "custom" / "my-rubric.yaml"
        runner = CliRunner()
        result = runner.invoke(main, ["rubric", "generate", str(p), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert not (tmp_path / "full-skill-rubric.yaml").exists()

    def test_parent_directories_created(self, tmp_path: Path) -> None:
        """AC-3: parent directories are created if they do not exist."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, FULL_SKILL)
        out = tmp_path / "deeply" / "nested" / "path" / "rubric.yaml"
        runner = CliRunner()
        result = runner.invoke(main, ["rubric", "generate", str(p), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_force_overwrites_existing_file(self, tmp_path: Path) -> None:
        """generate_rubric_file with force=True overwrites an existing file."""
        p = write_skill(tmp_path, FULL_SKILL)
        out = tmp_path / "rubric.yaml"
        out.write_text("old content", encoding="utf-8")
        result = generate_rubric_file(p, output=out, force=True)
        assert result == out
        content = out.read_text()
        assert "Generated by: orch rubric generate" in content

    def test_no_force_raises_on_existing_file(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        out = tmp_path / "rubric.yaml"
        out.write_text("old content", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            generate_rubric_file(p, output=out, force=False)


# ---------------------------------------------------------------------------
# AC-1: CLI command registration
# ---------------------------------------------------------------------------

class TestCliCommandRegistration:
    def test_rubric_group_exists(self) -> None:
        """AC-1: orch rubric --help shows the rubric group."""
        from orchestration_engine.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["rubric", "--help"])
        assert result.exit_code == 0
        assert "rubric" in result.output.lower()

    def test_generate_subcommand_exists(self) -> None:
        """AC-1: orch rubric generate --help works."""
        from orchestration_engine.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["rubric", "generate", "--help"])
        assert result.exit_code == 0
        assert "SKILL_FILE" in result.output or "skill_file" in result.output.lower()

    def test_main_help_includes_rubric(self) -> None:
        from orchestration_engine.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "rubric" in result.output


# ---------------------------------------------------------------------------
# Edge cases and malformed markdown (constraint: handle malformed markdown)
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_malformed_skill_parses_without_crash(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, MALFORMED_SKILL)
        data = parse_skill(p)
        assert data.skill_name == "malformed-skill"
        # The valid checklist item should still be extracted
        assert any("Valid item" in i["text"] for i in data.checklist_items)

    def test_no_frontmatter_skill_uses_filename(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, NO_FRONTMATTER_SKILL, filename="fallback-skill.md")
        data = parse_skill(p)
        assert data.skill_name == "fallback-skill"

    def test_skill_with_only_tables_generates_rubric(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, CRITERIA_TABLE_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert len(rubric) > 50

    def test_generate_rubric_file_returns_path(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, FULL_SKILL)
        out = tmp_path / "rubric.yaml"
        result = generate_rubric_file(p, output=out)
        assert result == out
        assert out.exists()

    def test_windows_line_endings_handled(self, tmp_path: Path) -> None:
        content = MINIMAL_SKILL.replace("\n", "\r\n")
        p = tmp_path / "SKILL.md"
        p.write_bytes(content.encode("utf-8"))
        data = parse_skill(p)
        assert data.skill_name == "test-skill"

    def test_unicode_content_handled(self, tmp_path: Path) -> None:
        content = MINIMAL_SKILL + "\n- [ ] **Unicode**: héllo wörld café 日本語\n"
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        assert any("Unicode" in i["text"] for i in data.checklist_items)

    def test_real_brand_voice_skill(self) -> None:
        """Smoke test against the actual brand-voice SKILL.md."""
        skill_path = Path("/home/toscan/knowledge-work-plugins/marketing/skills/brand-voice/SKILL.md")
        if not skill_path.exists():
            pytest.skip("Brand-voice skill file not available")
        data = parse_skill(skill_path)
        assert data.skill_name == "brand-voice"
        # Should have terminology pairs from the Preferred Terms table
        term_pairs = [p for p in data.do_dont_pairs if p.get("source") == "terminology_table"]
        assert len(term_pairs) > 0
        # Should have attribute block pairs (We are / We are not)
        attr_pairs = [p for p in data.do_dont_pairs if p.get("source") == "attribute_block"]
        assert len(attr_pairs) > 0
        # Criteria tables (channel tone table, etc.)
        assert len(data.criteria_tables) > 0

    def test_real_data_validation_skill(self) -> None:
        """Smoke test against the actual data-validation SKILL.md."""
        skill_path = Path("/home/toscan/knowledge-work-plugins/data/skills/data-validation/SKILL.md")
        if not skill_path.exists():
            pytest.skip("Data-validation skill file not available")
        data = parse_skill(skill_path)
        assert data.skill_name == "data-validation"
        # Should have checklist items
        assert len(data.checklist_items) > 0
        # Section tagging should work
        sections = {i["section"] for i in data.checklist_items}
        assert len(sections) > 1  # multiple sections


# ---------------------------------------------------------------------------
# build_criteria_list
# ---------------------------------------------------------------------------

class TestBuildCriteriaList:
    def test_checklist_type_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, CHECKLIST_SKILL)
        data = parse_skill(p)
        criteria = _build_criteria_list(data)
        types = [c["type"] for c in criteria]
        assert "checklist" in types

    def test_do_dont_type_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, TERMINOLOGY_TABLE_SKILL)
        data = parse_skill(p)
        criteria = _build_criteria_list(data)
        types = [c["type"] for c in criteria]
        assert "do_dont" in types

    def test_table_row_type_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, CRITERIA_TABLE_SKILL)
        data = parse_skill(p)
        criteria = _build_criteria_list(data)
        types = [c["type"] for c in criteria]
        assert "table_row" in types

    def test_do_dont_criteria_have_do_and_dont_keys(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, TERMINOLOGY_TABLE_SKILL)
        data = parse_skill(p)
        criteria = _build_criteria_list(data)
        dd = [c for c in criteria if c["type"] == "do_dont"]
        for item in dd:
            assert "do" in item
            assert "dont" in item
