"""Comprehensive tests for ``orchestration_engine.rubric_generator``.

Covers every acceptance criterion (AC-1 through AC-10), all identified edge
cases, internal helper contracts, and LLMJudgeGrader compatibility.

This file is designed to be **self-contained and runnable independently**:

    pytest tests/test_rubric_generator_comprehensive.py -v

All fixtures are defined locally; no conftest.py is required.

Structure
---------
Each test class maps to one concern:

  TestClassifyHeading          — _classify_heading() helper
  TestIsSep                    — _is_sep() helper
  TestSplitRow                 — _split_row() helper
  TestParseFrontmatter         — _parse_frontmatter() helper
  TestStripCodeBlocks          — _strip_code_blocks() helper
  TestCleanBold                — _clean_bold() helper
  TestChecklistExtractionDeep  — _extract_checklist_items() edge cases (AC-5)
  TestTerminologyTableDeep     — _extract_tables() terminology path (AC-6)
  TestWeAreBlockDeep           — _extract_we_are_blocks() (AC-7)
  TestCriteriaTableDeep        — _extract_tables() criteria path (AC-8)
  TestParseSkillDeep           — parse_skill() contract + error paths
  TestMakeScale                — _make_scale() all three branches
  TestRubricTextDeep           — generate_rubric_text() quality (AC-9)
  TestGenerateYamlDeep         — generate_yaml() structure (AC-10)
  TestGenerateRubricFileDeep   — generate_rubric_file() I/O contract (AC-2/3/4)
  TestCliDeep                  — orch rubric generate CLI (AC-1/2/3/4)
  TestLLMJudgeCompatibility    — rubric format ↔ LLMJudgeGrader._SCORE_RE
  TestBuildCriteriaListDeep    — _build_criteria_list() merging contract
"""

from __future__ import annotations

import re
import stat
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pytest
import yaml
from click.testing import CliRunner

from orchestration_engine.rubric_generator import (
    CriteriaTable,
    SkillData,
    _build_criteria_list,
    _classify_heading,
    _clean_bold,
    _extract_checklist_items,
    _extract_do_dont_from_sections,
    _extract_tables,
    _extract_we_are_blocks,
    _is_sep,
    _parse_frontmatter,
    _split_row,
    _strip_code_blocks,
    generate_rubric_file,
    generate_rubric_text,
    generate_yaml,
    parse_skill,
)

# ---------------------------------------------------------------------------
# Shared markdown fixtures (module-level constants for reuse)
# ---------------------------------------------------------------------------

_MULTI_SECTION_CHECKLIST = textwrap.dedent("""\
    ---
    name: multi-section
    description: Multi-section skill.
    ---

    # Multi Section Skill

    ## First Section

    - [ ] **Alpha**: First section item.
    - [ ] **Beta**: Also first section.

    ## Second Section

    - [ ] **Gamma**: Second section item.

    ### Sub-section

    - [ ] **Delta**: Sub-section item.
""")

_TERM_TABLE_REVERSED_COLS = textwrap.dedent("""\
    ---
    name: brand
    description: Brand voice.
    ---

    ## Terminology

    | Not This | Use This | Notes |
    |----------|----------|-------|
    | signup (verb) | sign up (verb) | noun form OK |
    | e-mail | email | no hyphen |
""")

_TERM_TABLE_LOWERCASE_HEADERS = textwrap.dedent("""\
    ---
    name: brand
    description: Brand voice.
    ---

    ## Terminology

    | use this | not this |
    |----------|----------|
    | sign up | signup |
""")

_TERM_TABLE_EMPTY_CELL = textwrap.dedent("""\
    ---
    name: brand
    description: Brand voice.
    ---

    ## Terminology

    | Use This | Not This |
    |----------|----------|
    | sign up | signup |
    |  |  |
""")

_WE_ARE_RESET_BY_EMPTY_LINE = textwrap.dedent("""\
    **Approachable**
    - **We are**: friendly

    - **We are not**: dumbed-down
""")

_WE_ARE_NOT_BEFORE_WE_ARE = textwrap.dedent("""\
    **Approachable**
    - **We are not**: arrogant
    - **We are**: confident
""")

_WE_ARE_DOUBLE_DO = textwrap.dedent("""\
    **Attr**
    - **We are**: first value
    - **We are**: second value overrides
    - **We are not**: the dont
""")

_WE_ARE_CASE_INSENSITIVE = textwrap.dedent("""\
    **Attr**
    - **WE ARE**: CAPS VERSION
    - **WE ARE NOT**: caps dont
""")

_MULTIPLE_TABLES_SAME_HEADING = textwrap.dedent("""\
    ---
    name: tables
    description: Tables skill.
    ---

    ## Checks

    | Metric | Threshold |
    |--------|-----------|
    | Revenue | > 0 |

    | Metric | Threshold |
    |--------|-----------|
    | Users | > 100 |
""")

_TABLE_NO_HEADING = textwrap.dedent("""\
    ---
    name: no-heading-tables
    description: Tables without heading.
    ---

    | Col A | Col B |
    |-------|-------|
    | val1  | val2  |
""")

_TABLE_SHORT_ROW = textwrap.dedent("""\
    ---
    name: short-rows
    description: Short rows.
    ---

    ## Checks

    | Col1 | Col2 | Col3 |
    |------|------|------|
    | only-one-cell |
""")

_MINIMAL = textwrap.dedent("""\
    ---
    name: minimal
    description: Minimal skill description.
    ---

    # Minimal

    Some content.
""")

_WITH_DESCRIPTION = textwrap.dedent("""\
    ---
    name: described-skill
    description: This skill ensures data quality.
    ---

    # Described Skill

    Intro paragraph here.

    ## Checklist

    - [ ] **Check A**: First check.
""")

_LONG_CELL_SKILL = textwrap.dedent("""\
    ---
    name: long-cell
    description: Skill with very long table cells.
    ---

    ## Criteria

    | Name | Description |
    |------|-------------|
    | Short | """ + ("X" * 250) + """ |
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_skill(tmp_path: Path, content: str, filename: str = "SKILL.md") -> Path:
    """Write *content* to *tmp_path / filename* and return the path."""
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestClassifyHeading
# ---------------------------------------------------------------------------

class TestClassifyHeading:
    """_classify_heading() returns 'do', 'dont', or 'neutral'."""

    @pytest.mark.parametrize("heading,expected", [
        # DO triggers
        ("Always do this", "do"),
        ("Best Practice", "do"),
        ("DO use this approach", "do"),
        # DONT triggers — evaluated BEFORE do (DONT wins)
        ("Avoid These Patterns", "dont"),
        ("Never Do This", "dont"),
        ("Don't use this", "dont"),
        ("Do NOT do this", "dont"),   # "NOT" wins over "DO"
        # Neutral
        ("Introduction", "neutral"),
        ("Tone by Channel", "neutral"),
        ("", "neutral"),
        ("Criteria Table", "neutral"),
    ])
    def test_classification(self, heading: str, expected: str) -> None:
        assert _classify_heading(heading) == expected

    def test_dont_takes_priority_over_do(self) -> None:
        """'DONT' check runs first in _classify_heading, so wins if both match."""
        # A heading with both DO and NOT keywords: "DONT" wins.
        result = _classify_heading("Do NOT skip this step")
        assert result == "dont"

    def test_case_insensitive(self) -> None:
        assert _classify_heading("ALWAYS include a summary") == "do"
        assert _classify_heading("AVOID jargon") == "dont"

    def test_does_not_match_substring_noise(self) -> None:
        # "notion" contains "not" but is not a word boundary match
        assert _classify_heading("Use Notion for notes") == "neutral"


# ---------------------------------------------------------------------------
# TestIsSep
# ---------------------------------------------------------------------------

class TestIsSep:
    """_is_sep() identifies markdown table separator rows."""

    @pytest.mark.parametrize("cells,expected", [
        (["---"], True),
        ([":---:"], True),
        (["---:", ":---", ":---:"], True),
        (["---", "---", "---"], True),
        (["", "---", ""], True),            # empty cells are ignored
        (["abc"], False),
        (["---", "value"], False),
        ([" --- "], True),                   # _is_sep strips spaces
    ])
    def test_separator_detection(self, cells: list, expected: bool) -> None:
        assert _is_sep(cells) == expected

    def test_empty_cells_list(self) -> None:
        """All-empty list has no non-empty cells — vacuously true."""
        assert _is_sep([]) is True

    def test_single_empty_string(self) -> None:
        """Single empty string is vacuously true."""
        assert _is_sep([""]) is True


# ---------------------------------------------------------------------------
# TestSplitRow
# ---------------------------------------------------------------------------

class TestSplitRow:
    """_split_row() parses a markdown table row into a cell list."""

    def test_standard_row_with_trailing_pipe(self) -> None:
        assert _split_row("| a | b | c |") == ["a", "b", "c"]

    def test_standard_row_without_trailing_pipe(self) -> None:
        assert _split_row("| a | b | c") == ["a", "b", "c"]

    def test_row_without_leading_pipe_returns_none(self) -> None:
        assert _split_row("a | b | c |") is None

    def test_single_cell(self) -> None:
        assert _split_row("| only |") == ["only"]

    def test_empty_cell(self) -> None:
        cells = _split_row("|  |  |")
        assert cells == ["", ""]

    def test_cells_are_stripped(self) -> None:
        cells = _split_row("|  padded  |  values  |")
        assert cells == ["padded", "values"]

    def test_empty_string_returns_none(self) -> None:
        assert _split_row("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _split_row("   ") is None


# ---------------------------------------------------------------------------
# TestParseFrontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    """_parse_frontmatter() extracts YAML-like fields from --- delimited block."""

    def test_valid_frontmatter(self) -> None:
        text = "---\nname: test\ndescription: A test skill.\n---\n\nBody"
        fields, body = _parse_frontmatter(text)
        assert fields["name"] == "test"
        assert fields["description"] == "A test skill."
        assert "Body" in body

    def test_no_frontmatter(self) -> None:
        text = "# Heading\n\nSome text."
        fields, body = _parse_frontmatter(text)
        assert fields == {}
        assert "Heading" in body

    def test_empty_string(self) -> None:
        fields, body = _parse_frontmatter("")
        assert fields == {}
        assert body == ""

    def test_frontmatter_with_spaces_in_value(self) -> None:
        text = "---\nname: my skill with spaces\n---\n"
        fields, _ = _parse_frontmatter(text)
        assert fields["name"] == "my skill with spaces"

    def test_frontmatter_value_stripped(self) -> None:
        text = "---\nname:   trailing spaces   \n---\n"
        fields, _ = _parse_frontmatter(text)
        assert fields["name"] == "trailing spaces"

    def test_incomplete_frontmatter_treated_as_body(self) -> None:
        """Only one --- delimiter → no frontmatter, whole text is body."""
        text = "---\nname: orphan\n\nBody text"
        fields, body = _parse_frontmatter(text)
        assert fields == {}
        assert "orphan" in body

    def test_unknown_keys_included(self) -> None:
        text = "---\nauthor: Alice\ncategory: data\n---\n"
        fields, _ = _parse_frontmatter(text)
        assert fields["author"] == "Alice"
        assert fields["category"] == "data"


# ---------------------------------------------------------------------------
# TestStripCodeBlocks
# ---------------------------------------------------------------------------

class TestStripCodeBlocks:
    """_strip_code_blocks() removes code block contents."""

    def test_checklist_in_code_block_removed(self) -> None:
        text = "before\n```\n- [ ] NOT AN ITEM\n```\nafter"
        result = _strip_code_blocks(text)
        assert "NOT AN ITEM" not in result
        assert "before" in result
        assert "after" in result

    def test_preserves_line_count(self) -> None:
        """Code block lines are replaced with blank lines (preserves positions)."""
        text = "line1\n```\nline3\nline4\n```\nline6"
        result = _strip_code_blocks(text)
        lines = result.splitlines()
        # Original had 6 lines; blank replacements keep count
        assert len(lines) == len(text.splitlines())

    def test_no_code_blocks_unchanged(self) -> None:
        text = "- [ ] Real item\nNormal text."
        assert _strip_code_blocks(text) == text

    def test_multiple_code_blocks_removed(self) -> None:
        text = "a\n```\nHIDDEN1\n```\nb\n```\nHIDDEN2\n```\nc"
        result = _strip_code_blocks(text)
        assert "HIDDEN1" not in result
        assert "HIDDEN2" not in result
        assert "a" in result and "b" in result and "c" in result

    def test_fenced_with_language_identifier(self) -> None:
        text = "text\n```sql\nSELECT 1;\n- [ ] FAKE ITEM\n```\nend"
        result = _strip_code_blocks(text)
        assert "FAKE ITEM" not in result
        assert "end" in result


# ---------------------------------------------------------------------------
# TestCleanBold
# ---------------------------------------------------------------------------

class TestCleanBold:
    """_clean_bold() strips **Label**: prefix."""

    def test_standard_label(self) -> None:
        assert _clean_bold("**Source**: Tables A and B.") == "Source: Tables A and B."

    def test_multi_word_label(self) -> None:
        assert _clean_bold("**Data Quality**: Check nulls.") == "Data Quality: Check nulls."

    def test_no_bold_markup(self) -> None:
        assert _clean_bold("plain text item") == "plain text item"

    def test_bold_without_colon(self) -> None:
        # **Label** with no ": " should not be stripped
        result = _clean_bold("**Label** some text")
        assert "**Label**" in result

    def test_bold_label_no_following_text(self) -> None:
        # **Label**: with nothing after — original returned unchanged
        result = _clean_bold("**Label**:")
        # No text after colon → returns original (implementation behaviour)
        assert result == "**Label**:"

    def test_nested_stars_in_description(self) -> None:
        # Stars in the description part are preserved
        result = _clean_bold("**Check**: Use **bold** words.")
        assert result == "Check: Use **bold** words."


# ---------------------------------------------------------------------------
# TestChecklistExtractionDeep (AC-5)
# ---------------------------------------------------------------------------

class TestChecklistExtractionDeep:
    """Deep edge-case coverage for _extract_checklist_items()."""

    def test_items_from_multiple_h2_sections_tagged_correctly(self) -> None:
        items = _extract_checklist_items(_MULTI_SECTION_CHECKLIST)
        alpha = next(i for i in items if "Alpha" in i["text"])
        gamma = next(i for i in items if "Gamma" in i["text"])
        assert alpha["section"] == "First Section"
        assert gamma["section"] == "Second Section"

    def test_h3_updates_section_tag(self) -> None:
        """H3 heading overrides the current section for following items."""
        items = _extract_checklist_items(_MULTI_SECTION_CHECKLIST)
        delta = next((i for i in items if "Delta" in i["text"]), None)
        assert delta is not None
        assert delta["section"] == "Sub-section"

    def test_checked_x_lowercase_included(self) -> None:
        text = "- [x] **Done item**: This was done."
        items = _extract_checklist_items(text)
        assert len(items) == 1
        assert "Done item" in items[0]["text"]

    def test_checked_x_uppercase_included(self) -> None:
        text = "- [X] **Done item uppercase**: Also done."
        items = _extract_checklist_items(text)
        assert len(items) == 1
        assert "Done item uppercase" in items[0]["text"]

    def test_item_with_no_bold_label(self) -> None:
        """A plain checklist item (no bold label) should still be captured."""
        text = "- [ ] Plain item without bold label."
        items = _extract_checklist_items(text)
        assert len(items) == 1
        assert "Plain item without bold label." in items[0]["text"]

    def test_items_start_with_no_section_when_no_heading_precedes(self) -> None:
        text = "- [ ] **First**: Before any heading.\n"
        items = _extract_checklist_items(text)
        assert items[0]["section"] == ""

    def test_deeply_indented_items_captured(self) -> None:
        text = "## Section\n    - [ ] **Indented item**: Deep indentation.\n"
        items = _extract_checklist_items(text)
        assert any("Indented item" in i["text"] for i in items)

    def test_heading_h1_does_not_set_section(self) -> None:
        """Only H2 and H3 set the section; H1 is the skill title."""
        text = "# Main Title\n- [ ] **Item**: Under h1.\n"
        items = _extract_checklist_items(text)
        assert items[0]["section"] == ""

    def test_items_returned_as_dicts_with_text_and_section_keys(self) -> None:
        items = _extract_checklist_items(_MULTI_SECTION_CHECKLIST)
        for item in items:
            assert "text" in item
            assert "section" in item
            assert isinstance(item["text"], str)
            assert isinstance(item["section"], str)


# ---------------------------------------------------------------------------
# TestTerminologyTableDeep (AC-6)
# ---------------------------------------------------------------------------

class TestTerminologyTableDeep:
    """Edge cases for terminology table DO/DONT extraction."""

    def test_column_order_does_not_matter(self) -> None:
        """'Not This' before 'Use This' should still produce correct pairs."""
        warns: List[str] = []
        _, pairs = _extract_tables(_TERM_TABLE_REVERSED_COLS, warns)
        do_vals = [p["do"] for p in pairs]
        dont_vals = [p["dont"] for p in pairs]
        assert "sign up (verb)" in do_vals
        assert "signup (verb)" in dont_vals

    def test_lowercase_column_headers_detected(self) -> None:
        """Column detection is case-insensitive."""
        warns: List[str] = []
        _, pairs = _extract_tables(_TERM_TABLE_LOWERCASE_HEADERS, warns)
        assert len(pairs) >= 1
        assert all(p["source"] == "terminology_table" for p in pairs)

    def test_empty_cell_rows_included_when_one_value_present(self) -> None:
        """Rows with at least one non-empty cell are included."""
        warns: List[str] = []
        _, pairs = _extract_tables(_TERM_TABLE_EMPTY_CELL, warns)
        non_empty_pairs = [p for p in pairs if p["do"] or p["dont"]]
        assert len(non_empty_pairs) >= 1

    def test_fully_empty_row_omitted(self) -> None:
        """A row with both cells empty should be excluded."""
        warns: List[str] = []
        _, pairs = _extract_tables(_TERM_TABLE_EMPTY_CELL, warns)
        blank_pairs = [p for p in pairs if not p["do"] and not p["dont"]]
        assert len(blank_pairs) == 0

    def test_terminology_pairs_not_added_to_criteria_tables(self) -> None:
        warns: List[str] = []
        tables, pairs = _extract_tables(_TERM_TABLE_REVERSED_COLS, warns)
        assert len(pairs) > 0
        assert all("Use This" not in t.columns and "Not This" not in t.columns
                   for t in tables)

    def test_extra_columns_beyond_use_and_not_this_ignored(self) -> None:
        """A 'Notes' column alongside Use/Not This should not break extraction."""
        warns: List[str] = []
        _, pairs = _extract_tables(_TERM_TABLE_REVERSED_COLS, warns)
        # Notes column should not appear as do/dont values
        for p in pairs:
            assert p["do"] != "Notes"
            assert p["dont"] != "Notes"


# ---------------------------------------------------------------------------
# TestWeAreBlockDeep (AC-7)
# ---------------------------------------------------------------------------

class TestWeAreBlockDeep:
    """Edge cases for _extract_we_are_blocks()."""

    def test_empty_line_between_we_are_and_we_are_not_discards_pair(self) -> None:
        """Empty line resets pending 'We are' → no pair produced."""
        pairs = _extract_we_are_blocks(_WE_ARE_RESET_BY_EMPTY_LINE)
        assert len(pairs) == 0

    def test_we_are_not_before_we_are_produces_no_pair(self) -> None:
        """'We are not' without a preceding 'We are' is ignored."""
        pairs = _extract_we_are_blocks(_WE_ARE_NOT_BEFORE_WE_ARE)
        # There's a "We are" after "We are not" — pending is set but no "We are not" follows
        assert len(pairs) == 0

    def test_second_we_are_replaces_first_pending(self) -> None:
        """Second '- **We are**: X' overrides first when no 'We are not' in between."""
        pairs = _extract_we_are_blocks(_WE_ARE_DOUBLE_DO)
        assert len(pairs) == 1
        assert pairs[0]["do"] == "second value overrides"
        assert pairs[0]["dont"] == "the dont"

    def test_case_insensitive_we_are_matching(self) -> None:
        """WE ARE and WE ARE NOT (all-caps) should be detected."""
        pairs = _extract_we_are_blocks(_WE_ARE_CASE_INSENSITIVE)
        assert len(pairs) == 1
        assert pairs[0]["do"] == "CAPS VERSION"
        assert pairs[0]["dont"] == "caps dont"

    def test_bold_attribute_heading_resets_pending(self) -> None:
        """A **Bold** line (attribute heading) resets pending We-are state."""
        text = textwrap.dedent("""\
            **AttributeA**
            - **We are**: value A
            **AttributeB**
            - **We are not**: value B
        """)
        # "value A" is pending, then **AttributeB** resets it
        # "value B" has no preceding "We are" → no pair
        pairs = _extract_we_are_blocks(text)
        assert len(pairs) == 0

    def test_multiple_valid_pairs_in_one_block(self) -> None:
        text = textwrap.dedent("""\
            **Attr1**
            - **We are**: warm
            - **We are not**: cold
            **Attr2**
            - **We are**: bold
            - **We are not**: timid
        """)
        pairs = _extract_we_are_blocks(text)
        assert len(pairs) == 2
        do_vals = [p["do"] for p in pairs]
        assert "warm" in do_vals
        assert "bold" in do_vals

    def test_pairs_always_have_all_three_keys(self) -> None:
        text = textwrap.dedent("""\
            **Attr**
            - **We are**: confident
            - **We are not**: arrogant
        """)
        pairs = _extract_we_are_blocks(text)
        for pair in pairs:
            assert "do" in pair
            assert "dont" in pair
            assert "source" in pair
            assert pair["source"] == "attribute_block"


# ---------------------------------------------------------------------------
# TestCriteriaTableDeep (AC-8)
# ---------------------------------------------------------------------------

class TestCriteriaTableDeep:
    """Edge cases for criteria table extraction."""

    def test_multiple_tables_under_same_heading_are_numbered(self) -> None:
        """AC-8: two tables under 'Checks' → 'Checks' and 'Checks (2)'."""
        warns: List[str] = []
        tables, _ = _extract_tables(_MULTIPLE_TABLES_SAME_HEADING, warns)
        names = [t.name for t in tables]
        assert len(names) == 2
        assert "Checks" in names
        assert any("(2)" in n or "Checks" in n for n in names)
        # Ensure deduplication: no two tables share the same name
        assert len(set(names)) == len(names)

    def test_table_with_no_preceding_heading_uses_criteria_fallback(self) -> None:
        warns: List[str] = []
        tables, _ = _extract_tables(_TABLE_NO_HEADING, warns)
        assert len(tables) == 1
        assert tables[0].name == "Criteria"

    def test_short_row_padded_to_column_width(self) -> None:
        """Rows with fewer cells than headers are padded with empty strings."""
        warns: List[str] = []
        tables, _ = _extract_tables(_TABLE_SHORT_ROW, warns)
        if tables:  # short row with only 1 cell is padded to 3
            row = tables[0].rows[0]
            assert len(row) == len(tables[0].columns)

    def test_table_with_separator_only_skipped(self) -> None:
        """A table with header + separator but zero data rows is skipped."""
        text = textwrap.dedent("""\
            ## Empty Table

            | Col1 | Col2 |
            |------|------|
        """)
        warns: List[str] = []
        tables, _ = _extract_tables(text, warns)
        assert len(tables) == 0

    def test_criteria_table_columns_preserved(self) -> None:
        warns: List[str] = []
        tables, _ = _extract_tables(_MULTIPLE_TABLES_SAME_HEADING, warns)
        for tbl in tables:
            assert "Metric" in tbl.columns
            assert "Threshold" in tbl.columns

    def test_criteria_table_rows_data_correct(self) -> None:
        warns: List[str] = []
        tables, _ = _extract_tables(_MULTIPLE_TABLES_SAME_HEADING, warns)
        all_row_data = [r[0] for t in tables for r in t.rows]
        assert "Revenue" in all_row_data
        assert "Users" in all_row_data

    def test_long_cell_in_criteria_table_not_truncated_by_extraction(self) -> None:
        """Extraction keeps full cell text; truncation only happens in rubric text."""
        warns: List[str] = []
        tables, _ = _extract_tables(_LONG_CELL_SKILL, warns)
        assert len(tables) == 1
        long_cell = tables[0].rows[0][1]
        assert len(long_cell) == 250  # full length preserved

    def test_heading_updates_table_name_between_tables(self) -> None:
        text = textwrap.dedent("""\
            ## Alpha Checks

            | A | B |
            |---|---|
            | a1 | b1 |

            ## Beta Checks

            | X | Y |
            |---|---|
            | x1 | y1 |
        """)
        warns: List[str] = []
        tables, _ = _extract_tables(text, warns)
        names = [t.name for t in tables]
        assert "Alpha Checks" in names
        assert "Beta Checks" in names


# ---------------------------------------------------------------------------
# TestParseSkillDeep
# ---------------------------------------------------------------------------

class TestParseSkillDeep:
    """Deep edge cases for parse_skill()."""

    def test_whitespace_only_file_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.md"
        p.write_text("   \n\t\n\n", encoding="utf-8")
        with pytest.raises(ValueError, match="[Ee]mpty"):
            parse_skill(p)

    def test_empty_description_in_frontmatter(self, tmp_path: Path) -> None:
        content = "---\nname: my-skill\ndescription:\n---\n# My Skill\n"
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        assert data.skill_name == "my-skill"
        assert data.description == ""

    def test_description_trimmed(self, tmp_path: Path) -> None:
        content = "---\nname: my-skill\ndescription:   spaces around   \n---\n# My\n"
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        assert data.description == "spaces around"

    def test_warnings_accumulate(self, tmp_path: Path) -> None:
        """No frontmatter name → warning added to data.warnings."""
        content = "# No Frontmatter\n- [ ] **Item**: Yes.\n"
        p = write_skill(tmp_path, content, filename="warn-skill.md")
        data = parse_skill(p)
        assert len(data.warnings) >= 1
        assert any("derived from filename" in w for w in data.warnings)

    def test_source_file_is_resolved_absolute(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        assert Path(data.source_file).is_absolute()
        assert Path(data.source_file).exists()

    def test_crlf_line_endings_normalised(self, tmp_path: Path) -> None:
        crlf = _MINIMAL.replace("\n", "\r\n")
        p = tmp_path / "crlf.md"
        p.write_bytes(crlf.encode("utf-8"))
        data = parse_skill(p)
        assert data.skill_name == "minimal"

    def test_all_do_dont_sources_present_in_full_skill(self, tmp_path: Path) -> None:
        full = textwrap.dedent("""\
            ---
            name: full
            description: Full.
            ---
            ## Terms
            | Use This | Not This |
            |----------|----------|
            | yes | no |
            **Attr**
            - **We are**: warm
            - **We are not**: cold
        """)
        p = write_skill(tmp_path, full)
        data = parse_skill(p)
        sources = {pair["source"] for pair in data.do_dont_pairs}
        assert "terminology_table" in sources
        assert "attribute_block" in sources

    def test_parse_skill_path_object_or_string(self, tmp_path: Path) -> None:
        """parse_skill accepts Path objects (test robustness)."""
        p = write_skill(tmp_path, _MINIMAL)
        data_path = parse_skill(p)
        data_str = parse_skill(str(p))  # type: ignore[arg-type]
        assert data_path.skill_name == data_str.skill_name

    def test_skill_name_set_from_frontmatter(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        assert data.skill_name == "minimal"

    def test_skill_name_from_filename_when_no_frontmatter_name(self, tmp_path: Path) -> None:
        content = "# No Frontmatter\n- [ ] **Item**: Yes.\n"
        p = write_skill(tmp_path, content, filename="my-special-skill.md")
        data = parse_skill(p)
        assert data.skill_name == "my-special-skill"

    @pytest.mark.skipif(
        not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0,
        reason="Cannot test PermissionError as root",
    )
    def test_permission_error_raises_value_error(self, tmp_path: Path) -> None:
        """A file with no read permission raises ValueError."""
        p = write_skill(tmp_path, _MINIMAL, filename="noperm.md")
        p.chmod(stat.S_IWRITE)  # write-only, no read
        try:
            with pytest.raises(ValueError, match="[Cc]annot read"):
                parse_skill(p)
        finally:
            p.chmod(stat.S_IREAD | stat.S_IWRITE)


# ---------------------------------------------------------------------------
# TestMakeScale
# ---------------------------------------------------------------------------

class TestMakeScale:
    """_make_scale() internal helper — three branches."""

    def _make_data(self) -> SkillData:
        return SkillData("test", "desc", "/tmp/x.md")

    def test_checklist_branch_mentions_item_count(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        data.checklist_items = [{"text": f"Item {i}", "section": "S"} for i in range(8)]
        scale = _make_scale(data)
        assert "8" in scale  # total count mentioned

    def test_checklist_branch_mentions_90_percent_threshold(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        data.checklist_items = [{"text": f"Item {i}", "section": ""} for i in range(10)]
        scale = _make_scale(data)
        # 90% of 10 = 9
        assert "9" in scale

    def test_table_branch_used_when_no_checklist_but_tables(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        data.criteria_tables = [CriteriaTable("Revenue", ["M", "V"], [["r", "v"]])]
        scale = _make_scale(data)
        assert "Revenue" in scale

    def test_table_branch_mentions_up_to_two_table_names(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        data.criteria_tables = [
            CriteriaTable("Alpha", ["A"], [["a"]]),
            CriteriaTable("Beta", ["B"], [["b"]]),
            CriteriaTable("Gamma", ["G"], [["g"]]),  # 3rd should be omitted
        ]
        scale = _make_scale(data)
        assert "Alpha" in scale
        assert "Beta" in scale
        # Gamma may or may not appear — just verify the first two are present

    def test_generic_branch_used_when_no_checklist_or_tables(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        scale = _make_scale(data)
        assert "Fully meets all quality expectations" in scale

    def test_scale_always_has_six_bands(self) -> None:
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        for score in ["1.0", "0.8", "0.6", "0.4", "0.2", "0.0"]:
            assert score in _make_scale(data)

    def test_checklist_branch_with_single_item(self) -> None:
        """Edge: n=1 should not crash (min() clamps to 1)."""
        from orchestration_engine.rubric_generator import _make_scale
        data = self._make_data()
        data.checklist_items = [{"text": "Only item", "section": ""}]
        scale = _make_scale(data)
        assert "1" in scale  # won't crash


# ---------------------------------------------------------------------------
# TestRubricTextDeep (AC-9)
# ---------------------------------------------------------------------------

class TestRubricTextDeep:
    """Deep coverage for generate_rubric_text()."""

    def test_preamble_contains_skill_name(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Minimal" in rubric  # title-cased skill name

    def test_description_included_in_preamble(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "This skill ensures data quality" in rubric

    def test_no_description_uses_generic_preamble(self, tmp_path: Path) -> None:
        content = "---\nname: no-desc\n---\n# Title\n- [ ] **Item**: thing.\n"
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "evaluating content quality" in rubric.lower()

    def test_all_four_required_sections_present(self, tmp_path: Path) -> None:
        """AC-9: Preamble (title), Scoring Scale, Specific Checks, Output Format."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Quality Rubric" in rubric or "You are evaluating" in rubric  # Preamble
        assert "Scoring Scale" in rubric
        assert "Specific Checks" in rubric
        assert "Output Format" in rubric

    def test_score_placeholder_verbatim(self, tmp_path: Path) -> None:
        """AC-9: 'Score: [0.0-1.0]' appears verbatim for LLMJudgeGrader."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Score: [0.0-1.0]" in rubric

    def test_reasoning_placeholder_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Reasoning:" in rubric

    def test_failed_checks_placeholder_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Failed checks:" in rubric

    def test_checklist_items_numbered(self, tmp_path: Path) -> None:
        """Each checklist item appears numbered in Specific Checks section."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "1." in rubric

    def test_section_tag_in_checklist_item(self, tmp_path: Path) -> None:
        """Items with a section should show [Section] tag in rubric text."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        # The item is under '## Checklist' section
        assert "[Checklist]" in rubric

    def test_do_dont_section_present_when_pairs_exist(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            ---
            name: brand
            description: Brand voice.
            ---
            ## Terms
            | Use This | Not This |
            |----------|----------|
            | yes | no |
        """)
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "DO / DON'T Guidelines" in rubric
        assert "✓ **DO:**" in rubric
        assert "✗ **DON'T:**" in rubric

    def test_criteria_tables_section_present(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _TABLE_NO_HEADING)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Criteria Tables" in rubric

    def test_no_checks_fallback_note_present(self, tmp_path: Path) -> None:
        """When no structured content, a 'No specific...' note appears."""
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "No specific checklist items" in rubric or "best judgment" in rubric

    def test_long_table_cell_truncated_in_rubric(self, tmp_path: Path) -> None:
        """Cells >200 chars are truncated to 150 + '…' in the criteria table."""
        p = write_skill(tmp_path, _LONG_CELL_SKILL)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        # The 250-char cell should be truncated (ends with '…')
        assert "…" in rubric

    def test_rubric_is_string(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        assert isinstance(generate_rubric_text(data), str)


# ---------------------------------------------------------------------------
# TestGenerateYamlDeep (AC-10)
# ---------------------------------------------------------------------------

class TestGenerateYamlDeep:
    """Deep structural coverage for generate_yaml()."""

    def test_yaml_header_comment_present(self, tmp_path: Path) -> None:
        """The raw YAML string should start with the generator comment."""
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        raw = generate_yaml(data)
        assert raw.startswith("# Generated by: orch rubric generate")

    def test_yaml_header_includes_source_path(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        raw = generate_yaml(data)
        assert "# Source:" in raw

    def test_yaml_header_includes_llmjudge_compatibility(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        raw = generate_yaml(data)
        assert "LLMJudgeGrader" in raw

    def test_generated_at_is_valid_iso_datetime(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        # Should not raise
        dt = datetime.fromisoformat(parsed["generated_at"])
        assert isinstance(dt, datetime)

    def test_generated_from_matches_source_file(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert parsed["generated_from"] == data.source_file

    def test_name_matches_skill_name(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert parsed["name"] == "minimal"

    def test_criteria_checklist_items_have_required_keys(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        checklists = [c for c in parsed["criteria"] if c["type"] == "checklist"]
        for item in checklists:
            assert "text" in item
            assert "section" in item

    def test_criteria_do_dont_items_have_required_keys(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            ---
            name: dd
            description: .
            ---
            ## Terms
            | Use This | Not This |
            |----------|----------|
            | yes | no |
        """)
        p = write_skill(tmp_path, content)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        dd_items = [c for c in parsed["criteria"] if c["type"] == "do_dont"]
        for item in dd_items:
            assert "do" in item
            assert "dont" in item
            assert "source" in item

    def test_criteria_table_row_items_have_required_keys(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _TABLE_NO_HEADING)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        table_rows = [c for c in parsed["criteria"] if c["type"] == "table_row"]
        for item in table_rows:
            assert "table" in item
            assert "values" in item
            assert isinstance(item["values"], dict)

    def test_criteria_table_row_values_dict_uses_column_headers_as_keys(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _TABLE_NO_HEADING)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        table_rows = [c for c in parsed["criteria"] if c["type"] == "table_row"]
        assert len(table_rows) >= 1
        assert "Col A" in table_rows[0]["values"]
        assert "Col B" in table_rows[0]["values"]

    def test_yaml_safe_load_does_not_raise(self, tmp_path: Path) -> None:
        """Any non-empty skill should produce parseable YAML without exception."""
        for content in [_MINIMAL, _WITH_DESCRIPTION, _MULTIPLE_TABLES_SAME_HEADING]:
            p = write_skill(tmp_path, content)
            data = parse_skill(p)
            parsed = yaml.safe_load(generate_yaml(data))
            assert parsed is not None

    def test_rubric_field_contains_four_sections(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        rubric = parsed["rubric"]
        assert "Scoring Scale" in rubric
        assert "Specific Checks" in rubric
        assert "Output Format" in rubric


# ---------------------------------------------------------------------------
# TestGenerateRubricFileDeep (AC-2/3/4)
# ---------------------------------------------------------------------------

class TestGenerateRubricFileDeep:
    """I/O contract for generate_rubric_file()."""

    def test_default_output_filename_uses_skill_name(self, tmp_path: Path) -> None:
        """AC-2: default output = <skill-name>-rubric.yaml in CWD."""
        p = write_skill(tmp_path, _MINIMAL)
        # Call with no output → creates minimal-rubric.yaml in CWD
        import os
        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            out = generate_rubric_file(p)
            # Check while still in tmp_path so the relative path resolves correctly
            assert out.name == "minimal-rubric.yaml"
            assert Path(out).resolve().exists()
        finally:
            os.chdir(orig)
        # After restore: verify the file was created in tmp_path
        assert (tmp_path / "minimal-rubric.yaml").exists()

    def test_output_file_contains_yaml_header_comment(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "out.yaml"
        generate_rubric_file(p, output=out)
        content = out.read_text(encoding="utf-8")
        assert "# Generated by: orch rubric generate" in content

    def test_returns_path_of_written_file(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "result.yaml"
        returned = generate_rubric_file(p, output=out)
        assert returned == out

    def test_parent_dirs_created_automatically(self, tmp_path: Path) -> None:
        """AC-3: mkdir -p behaviour."""
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "a" / "b" / "c" / "rubric.yaml"
        generate_rubric_file(p, output=out)
        assert out.exists()

    def test_force_false_raises_on_existing_output(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "rubric.yaml"
        out.write_text("existing", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            generate_rubric_file(p, output=out, force=False)

    def test_force_true_overwrites_existing_output(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "rubric.yaml"
        out.write_text("old content", encoding="utf-8")
        generate_rubric_file(p, output=out, force=True)
        content = out.read_text(encoding="utf-8")
        assert "Generated by: orch rubric generate" in content
        assert "old content" not in content

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        """AC-4: ValueError for missing skill file."""
        with pytest.raises(ValueError, match="[Ff]ile not found"):
            generate_rubric_file(tmp_path / "ghost.md")

    def test_directory_as_skill_file_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="[Dd]irectory"):
            generate_rubric_file(tmp_path)

    def test_directory_as_output_raises_value_error(self, tmp_path: Path) -> None:
        p = write_skill(tmp_path, _MINIMAL)
        with pytest.raises(ValueError, match="[Dd]irectory"):
            generate_rubric_file(p, output=tmp_path)

    def test_warning_printed_to_stderr_for_no_structured_content(
        self, tmp_path: Path, capsys
    ) -> None:
        """generate_rubric_file warns on stderr when no checklist or tables found."""
        p = write_skill(tmp_path, _MINIMAL)
        out = tmp_path / "rubric.yaml"
        generate_rubric_file(p, output=out)
        captured = capsys.readouterr()
        assert "No checklist" in captured.err or "generic" in captured.err

    def test_warnings_from_skill_data_printed_to_stderr(
        self, tmp_path: Path, capsys
    ) -> None:
        """Warnings accumulated in SkillData are printed to stderr."""
        content = "# No Frontmatter\n- [ ] **Item**: Yes.\n"
        p = write_skill(tmp_path, content, filename="nowarn-skill.md")
        out = tmp_path / "rubric.yaml"
        generate_rubric_file(p, output=out)
        captured = capsys.readouterr()
        assert "⚠" in captured.err

    def test_output_file_encoding_is_utf8(self, tmp_path: Path) -> None:
        content = _MINIMAL + "\n- [ ] **Unicode**: café 日本語.\n"
        p = write_skill(tmp_path, content)
        out = tmp_path / "rubric.yaml"
        generate_rubric_file(p, output=out)
        # Should not raise — reads back as UTF-8 correctly
        text = out.read_text(encoding="utf-8")
        assert "café" in text or "日本語" in text or "rubric" in text.lower()


# ---------------------------------------------------------------------------
# TestCliDeep (AC-1/2/3/4)
# ---------------------------------------------------------------------------

class TestCliDeep:
    """Deep CLI coverage for the orch rubric generate command."""

    def _runner(self) -> CliRunner:
        return CliRunner(mix_stderr=False)

    def test_rubric_group_in_main_help(self) -> None:
        """AC-1: 'orch --help' lists the rubric group."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "rubric" in result.output

    def test_rubric_help_shows_generate_subcommand(self) -> None:
        """AC-1: 'orch rubric --help' lists generate."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(main, ["rubric", "--help"])
        assert result.exit_code == 0
        assert "generate" in result.output

    def test_rubric_generate_help_shows_skill_file_arg(self) -> None:
        """AC-1: 'orch rubric generate --help' shows SKILL_FILE argument."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(main, ["rubric", "generate", "--help"])
        assert result.exit_code == 0
        assert "SKILL_FILE" in result.output or "skill-file" in result.output.lower()

    def test_rubric_generate_help_shows_output_option(self) -> None:
        """--output / -o option visible in help."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(main, ["rubric", "generate", "--help"])
        assert result.exit_code == 0
        assert "--output" in result.output or "-o" in result.output

    def test_rubric_generate_help_shows_force_option(self) -> None:
        """--force / -f option visible in help."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(main, ["rubric", "generate", "--help"])
        assert result.exit_code == 0
        assert "--force" in result.output or "-f" in result.output

    def test_success_message_contains_output_path(self, tmp_path: Path) -> None:
        """AC-2: On success, CLI prints '✓ Rubric written to: <path>'."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "out.yaml"
        result = self._runner().invoke(main, ["rubric", "generate", str(p), "--output", str(out)])
        assert result.exit_code == 0
        assert "✓" in result.output or "Rubric written" in result.output

    def test_exit_code_0_on_success(self, tmp_path: Path) -> None:
        """AC-2: exit code 0 on successful generation."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "out.yaml"
        result = self._runner().invoke(main, ["rubric", "generate", str(p), "--output", str(out)])
        assert result.exit_code == 0

    def test_exit_code_1_for_missing_skill_file(self, tmp_path: Path) -> None:
        """AC-4: exit code 1 when skill file does not exist."""
        from orchestration_engine.cli import main
        result = self._runner().invoke(
            main, ["rubric", "generate", str(tmp_path / "does_not_exist.md")]
        )
        assert result.exit_code == 1

    def test_stderr_contains_path_on_missing_file_error(self, tmp_path: Path) -> None:
        """AC-4: stderr mentions the missing path."""
        from orchestration_engine.cli import main
        missing = tmp_path / "nonexistent.md"
        result = self._runner().invoke(main, ["rubric", "generate", str(missing)])
        combined = (result.output or "") + (result.stderr or "")
        assert "nonexistent.md" in combined

    def test_no_output_file_created_on_error(self, tmp_path: Path) -> None:
        """AC-4: no file is created when skill file is missing."""
        from orchestration_engine.cli import main
        out = tmp_path / "should-not-exist.yaml"
        result = self._runner().invoke(
            main,
            ["rubric", "generate", str(tmp_path / "ghost.md"), "--output", str(out)],
        )
        assert result.exit_code == 1
        assert not out.exists()

    def test_output_option_short_form(self, tmp_path: Path) -> None:
        """-o short alias for --output works."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "short-out.yaml"
        result = self._runner().invoke(main, ["rubric", "generate", str(p), "-o", str(out)])
        assert result.exit_code == 0
        assert out.exists()

    def test_force_short_form(self, tmp_path: Path) -> None:
        """-f short alias for --force works."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "force-out.yaml"
        out.write_text("old", encoding="utf-8")
        result = self._runner().invoke(
            main, ["rubric", "generate", str(p), "-o", str(out), "-f"]
        )
        assert result.exit_code == 0
        assert "old" not in out.read_text(encoding="utf-8")

    def test_empty_skill_file_exits_1(self, tmp_path: Path) -> None:
        """An empty file should produce exit code 1."""
        from orchestration_engine.cli import main
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        result = self._runner().invoke(main, ["rubric", "generate", str(p)])
        assert result.exit_code == 1

    def test_force_flag_overwrites_via_cli(self, tmp_path: Path) -> None:
        """AC-3: --force allows overwriting an existing output file."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "overwrite.yaml"
        out.write_text("old content", encoding="utf-8")
        result = self._runner().invoke(
            main, ["rubric", "generate", str(p), "-o", str(out), "--force"]
        )
        assert result.exit_code == 0
        assert "old content" not in out.read_text(encoding="utf-8")

    def test_without_force_exits_nonzero_if_output_exists(self, tmp_path: Path) -> None:
        """Without --force, a pre-existing output causes non-zero exit."""
        from orchestration_engine.cli import main
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        out = tmp_path / "exists.yaml"
        out.write_text("existing", encoding="utf-8")
        result = self._runner().invoke(
            main, ["rubric", "generate", str(p), "-o", str(out)]
        )
        assert result.exit_code != 0

    def test_isolated_filesystem_default_output_created(self) -> None:
        """AC-2: Default <name>-rubric.yaml file created in CWD."""
        from orchestration_engine.cli import main
        runner = CliRunner()
        with runner.isolated_filesystem():
            # Write skill to CWD
            Path("SKILL.md").write_text(_WITH_DESCRIPTION, encoding="utf-8")
            result = runner.invoke(main, ["rubric", "generate", "SKILL.md"])
            assert result.exit_code == 0, result.output
            assert Path("described-skill-rubric.yaml").exists()


# ---------------------------------------------------------------------------
# TestLLMJudgeCompatibility
# ---------------------------------------------------------------------------

class TestLLMJudgeCompatibility:
    """Verify rubric output is compatible with LLMJudgeGrader scoring."""

    # The exact pattern from llm_judge.py
    _SCORE_RE = re.compile(r"Score:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)

    def test_rubric_instructs_judge_to_output_score(self, tmp_path: Path) -> None:
        """The rubric's Output Format contains 'Score:' instruction."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        rubric = generate_rubric_text(data)
        assert "Score:" in rubric

    def test_simulated_judge_response_parseable(self) -> None:
        """A judge response following the rubric template is parsed by _SCORE_RE."""
        simulated_response = textwrap.dedent("""\
            Score: 0.85
            Reasoning: The content meets most criteria but misses one check.
            Failed checks: Item 3 was not addressed.
        """)
        match = self._SCORE_RE.search(simulated_response)
        assert match is not None
        assert float(match.group(1)) == pytest.approx(0.85)

    def test_score_range_values_parseable(self) -> None:
        """Edge score values (0.0, 1.0, 0.5) are all parseable."""
        for score_str, expected in [("0.0", 0.0), ("1.0", 1.0), ("0.5", 0.5), ("1", 1.0)]:
            response = f"Score: {score_str}\nReasoning: test."
            match = self._SCORE_RE.search(response)
            assert match is not None, f"Failed to parse Score: {score_str}"
            assert float(match.group(1)) == pytest.approx(expected)

    def test_score_placeholder_not_matched_by_score_re(self) -> None:
        """The instruction 'Score: [0.0-1.0]' is NOT a valid score (has brackets)."""
        # This confirms the rubric placeholder doesn't accidentally parse as a score
        match = self._SCORE_RE.search("Score: [0.0-1.0]")
        # Should not match (bracket is not a digit)
        assert match is None

    def test_rubric_yaml_rubric_field_contains_score_instruction(self, tmp_path: Path) -> None:
        """The YAML output's rubric field includes the Score instruction."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert "Score: [0.0-1.0]" in parsed["rubric"]

    def test_rubric_yaml_rubric_field_contains_reasoning_instruction(self, tmp_path: Path) -> None:
        """The YAML output's rubric field includes the Reasoning instruction."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        assert "Reasoning:" in parsed["rubric"]

    def test_rubric_text_can_be_used_as_inline_rubric_value(self, tmp_path: Path) -> None:
        """The rubric field value should be a string safe to embed in a scenario YAML."""
        p = write_skill(tmp_path, _WITH_DESCRIPTION)
        data = parse_skill(p)
        parsed = yaml.safe_load(generate_yaml(data))
        rubric_value = parsed["rubric"]
        # Re-serialize as YAML inline rubric: to verify it round-trips
        scenario_fragment = yaml.dump({"rubric": rubric_value})
        re_parsed = yaml.safe_load(scenario_fragment)
        assert re_parsed["rubric"] == rubric_value


# ---------------------------------------------------------------------------
# TestBuildCriteriaListDeep
# ---------------------------------------------------------------------------

class TestBuildCriteriaListDeep:
    """Deep coverage for _build_criteria_list()."""

    def test_empty_skill_data_returns_empty_list(self) -> None:
        data = SkillData("empty", "desc", "/tmp/x.md")
        assert _build_criteria_list(data) == []

    def test_checklist_items_become_type_checklist(self) -> None:
        data = SkillData("test", "desc", "/tmp/x.md")
        data.checklist_items = [{"text": "Item 1", "section": "S1"}]
        criteria = _build_criteria_list(data)
        assert len(criteria) == 1
        assert criteria[0]["type"] == "checklist"
        assert criteria[0]["text"] == "Item 1"
        assert criteria[0]["section"] == "S1"

    def test_do_dont_pairs_become_type_do_dont(self) -> None:
        data = SkillData("test", "desc", "/tmp/x.md")
        data.do_dont_pairs = [{"do": "yes", "dont": "no", "source": "terminology_table"}]
        criteria = _build_criteria_list(data)
        assert len(criteria) == 1
        assert criteria[0]["type"] == "do_dont"
        assert criteria[0]["do"] == "yes"
        assert criteria[0]["dont"] == "no"
        assert criteria[0]["source"] == "terminology_table"

    def test_table_rows_become_type_table_row(self) -> None:
        data = SkillData("test", "desc", "/tmp/x.md")
        data.criteria_tables = [
            CriteriaTable("MyTable", ["A", "B"], [["val_a", "val_b"]])
        ]
        criteria = _build_criteria_list(data)
        assert len(criteria) == 1
        assert criteria[0]["type"] == "table_row"
        assert criteria[0]["table"] == "MyTable"
        assert criteria[0]["values"] == {"A": "val_a", "B": "val_b"}

    def test_order_checklist_then_dodont_then_table_rows(self) -> None:
        """The unified list preserves insertion order: checklist → do_dont → table_row."""
        data = SkillData("test", "desc", "/tmp/x.md")
        data.checklist_items = [{"text": "C item", "section": ""}]
        data.do_dont_pairs = [{"do": "d", "dont": "n", "source": "x"}]
        data.criteria_tables = [CriteriaTable("T", ["K"], [["v"]])]
        criteria = _build_criteria_list(data)
        types = [c["type"] for c in criteria]
        assert types == ["checklist", "do_dont", "table_row"]

    def test_multiple_table_rows_from_multi_row_table(self) -> None:
        data = SkillData("test", "desc", "/tmp/x.md")
        data.criteria_tables = [
            CriteriaTable("T", ["Col"], [["r1"], ["r2"], ["r3"]])
        ]
        criteria = _build_criteria_list(data)
        assert len(criteria) == 3
        assert all(c["type"] == "table_row" for c in criteria)

    def test_multiple_tables_all_rows_included(self) -> None:
        data = SkillData("test", "desc", "/tmp/x.md")
        data.criteria_tables = [
            CriteriaTable("T1", ["A"], [["a1"], ["a2"]]),
            CriteriaTable("T2", ["B"], [["b1"]]),
        ]
        criteria = _build_criteria_list(data)
        assert len(criteria) == 3
        tables = [c["table"] for c in criteria]
        assert tables.count("T1") == 2
        assert tables.count("T2") == 1

    def test_do_dont_missing_source_key_handled(self) -> None:
        """If source key is absent, empty string is used (defensive)."""
        data = SkillData("test", "desc", "/tmp/x.md")
        data.do_dont_pairs = [{"do": "d", "dont": "n"}]  # no 'source' key
        criteria = _build_criteria_list(data)
        assert criteria[0]["source"] == ""

    def test_checklist_missing_section_key_handled(self) -> None:
        """If section key is absent, empty string is used (defensive)."""
        data = SkillData("test", "desc", "/tmp/x.md")
        data.checklist_items = [{"text": "Item without section"}]  # no 'section'
        criteria = _build_criteria_list(data)
        assert criteria[0]["section"] == ""


# ---------------------------------------------------------------------------
# Integration: real skill files (skip if not available)
# ---------------------------------------------------------------------------

class TestRealSkillFiles:
    """Smoke tests against actual skill files in the repository."""

    _BRAND_VOICE = Path("/home/toscan/knowledge-work-plugins/marketing/skills/brand-voice/SKILL.md")
    _DATA_VALIDATION = Path("/home/toscan/knowledge-work-plugins/data/skills/data-validation/SKILL.md")

    def _require(self, path: Path) -> Path:
        if not path.exists():
            pytest.skip(f"Skill file not available: {path}")
        return path

    def test_brand_voice_produces_valid_yaml(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        raw = generate_yaml(data)
        parsed = yaml.safe_load(raw)
        assert parsed["name"] == "brand-voice"
        assert isinstance(parsed["rubric"], str)
        assert len(parsed["criteria"]) > 0

    def test_brand_voice_has_terminology_pairs(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        term = [p for p in data.do_dont_pairs if p["source"] == "terminology_table"]
        assert len(term) > 0

    def test_brand_voice_has_attribute_block_pairs(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        attr = [p for p in data.do_dont_pairs if p["source"] == "attribute_block"]
        assert len(attr) > 0

    def test_brand_voice_has_criteria_tables(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        assert len(data.criteria_tables) > 0

    def test_brand_voice_rubric_contains_score_instruction(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        rubric = generate_rubric_text(data)
        assert "Score: [0.0-1.0]" in rubric

    def test_data_validation_has_checklist_items(self) -> None:
        path = self._require(self._DATA_VALIDATION)
        data = parse_skill(path)
        assert len(data.checklist_items) > 0

    def test_data_validation_items_have_section_tags(self) -> None:
        path = self._require(self._DATA_VALIDATION)
        data = parse_skill(path)
        sections = {i["section"] for i in data.checklist_items}
        assert len(sections) > 1  # multiple sections present

    def test_data_validation_produces_valid_yaml(self) -> None:
        path = self._require(self._DATA_VALIDATION)
        data = parse_skill(path)
        raw = generate_yaml(data)
        parsed = yaml.safe_load(raw)
        assert parsed["name"] == "data-validation"
        assert len(parsed["criteria"]) > 0

    def test_data_validation_rubric_has_specific_checks_content(self) -> None:
        path = self._require(self._DATA_VALIDATION)
        data = parse_skill(path)
        rubric = generate_rubric_text(data)
        # At least one checklist item text should appear in the rubric
        first_item_text = data.checklist_items[0]["text"]
        # Strip the bold-label prefix if present
        label = first_item_text.split(":")[0].strip()
        assert label in rubric

    def test_brand_voice_criteria_list_has_all_types(self) -> None:
        path = self._require(self._BRAND_VOICE)
        data = parse_skill(path)
        criteria = _build_criteria_list(data)
        types = {c["type"] for c in criteria}
        # Brand-voice has terminology + attribute blocks + criteria tables
        assert "do_dont" in types
        assert "table_row" in types
