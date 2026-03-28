"""
Unit tests for scripts/extract_issue.py

Tests cover:
  - validate_issue_number
  - validate_repo_format
  - parse_sections (code-fence-aware)
  - _strip_comments_preserving_fences
  - extract_and_write (integration-style with in-memory body)
  - route_section
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure script directory is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from extract_issue import (
    validate_issue_number,
    validate_repo_format,
    parse_sections,
    route_section,
    _strip_comments_preserving_fences,
    extract_and_write,
)


# ─────────────────────────────────────────────
# validate_issue_number
# ─────────────────────────────────────────────

class TestValidateIssueNumber:

    def test_valid_positive_integers(self):
        assert validate_issue_number("1") == 1
        assert validate_issue_number("42") == 42
        assert validate_issue_number("1000") == 1000

    def test_zero_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_issue_number("0")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid issue number" in captured.err.lower()

    def test_negative_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_issue_number("-5")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid issue number" in captured.err.lower()

    def test_float_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_issue_number("1.5")
        assert exc.value.code == 1

    def test_non_numeric_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_issue_number("abc")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid issue number" in captured.err.lower()


# ─────────────────────────────────────────────
# validate_repo_format
# ─────────────────────────────────────────────

class TestValidateRepoFormat:

    def test_valid_repo_formats(self):
        assert validate_repo_format("owner/repo") == "owner/repo"
        assert validate_repo_format("Org-Name/my_repo.git") == "Org-Name/my_repo.git"
        assert validate_repo_format("A/B") == "A/B"

    def test_no_slash_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_repo_format("noslash")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid repo format" in captured.err.lower()

    def test_empty_owner_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_repo_format("/repo")
        assert exc.value.code == 1

    def test_empty_repo_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_repo_format("owner/")
        assert exc.value.code == 1

    def test_extra_slash_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_repo_format("owner/repo/extra")
        assert exc.value.code == 1

    def test_space_is_invalid(self, capsys):
        with pytest.raises(SystemExit) as exc:
            validate_repo_format("owner repo")
        assert exc.value.code == 1


# ─────────────────────────────────────────────
# parse_sections
# ─────────────────────────────────────────────

class TestParseSections:

    def test_simple_sections(self):
        text = "## User Story\nAs a dev.\n\n## Context\nSome context.\n"
        sections = parse_sections(text)
        # Preamble (empty) + two sections
        assert sections[0][0] is None  # preamble
        assert sections[1][0] == "## User Story"
        assert sections[2][0] == "## Context"

    def test_preamble_captured(self):
        text = "Preamble text.\n\n## User Story\nAs a dev.\n"
        sections = parse_sections(text)
        assert sections[0][0] is None
        assert "Preamble text." in sections[0][1]

    def test_headers_inside_fence_not_split(self):
        text = "## Context\nHere:\n```\n## Not a header\n```\n\n## Behavioral Contracts\nContracts.\n"
        sections = parse_sections(text)
        # Should only split on actual ## headers: preamble + Context + Behavioral Contracts
        assert len(sections) == 3
        headers = [s[0] for s in sections]
        assert "## Context" in headers
        assert "## Behavioral Contracts" in headers

    def test_longer_fence_not_closed_by_shorter(self):
        text = "## Context\n````\n```\n## not header\n```\n````\n\n## Behavioral Contracts\nContent.\n"
        sections = parse_sections(text)
        headers = [s[0] for s in sections]
        assert "## Context" in headers
        assert "## Behavioral Contracts" in headers
        # The "## not header" should be inside Context's content
        context_content = [s[1] for s in sections if s[0] == "## Context"][0]
        assert "## not header" in context_content

    def test_subsections_included_in_parent(self):
        text = "## Context\nIntro.\n\n### Sub\nSub content.\n\n## Behavioral Contracts\nContracts.\n"
        sections = parse_sections(text)
        context = [s[1] for s in sections if s[0] == "## Context"][0]
        assert "### Sub" in context
        assert "Sub content." in context

    def test_empty_body_returns_preamble_only(self):
        sections = parse_sections("")
        assert len(sections) == 1
        assert sections[0][0] is None


# ─────────────────────────────────────────────
# route_section
# ─────────────────────────────────────────────

class TestRouteSection:

    def test_known_sections_routed_correctly(self):
        assert route_section("User Story") == "spec.md"
        assert route_section("Context") == "spec.md"
        assert route_section("Integration points") == "spec.md"
        assert route_section("Behavioral Contracts") == "behavioral.md"
        assert route_section("Acceptance Criteria") == "behavioral.md"

    def test_case_insensitive_routing(self):
        assert route_section("user story") == "spec.md"
        assert route_section("BEHAVIORAL CONTRACTS") == "behavioral.md"
        assert route_section("Acceptance Criteria".lower()) == "behavioral.md"

    def test_unknown_section_defaults_to_spec(self):
        assert route_section("Design Notes") == "spec.md"
        assert route_section("Whatever") == "spec.md"


# ─────────────────────────────────────────────
# _strip_comments_preserving_fences
# ─────────────────────────────────────────────

class TestStripHtmlComments:

    def test_simple_inline_comment_stripped(self):
        text = "Before <!-- comment --> after\n"
        result = _strip_comments_preserving_fences(text)
        assert "<!-- comment -->" not in result
        assert "Before" in result
        assert "after" in result

    def test_multiline_comment_stripped(self):
        text = "Line 1\n<!--\nMulti-line\ncomment\n-->\nLine 2\n"
        result = _strip_comments_preserving_fences(text)
        assert "Multi-line" not in result
        assert "Line 1" in result
        assert "Line 2" in result

    def test_comment_inside_fence_preserved(self):
        text = "```\n<!-- not stripped -->\n```\n"
        result = _strip_comments_preserving_fences(text)
        assert "<!-- not stripped -->" in result

    def test_comment_inside_longer_fence_preserved(self):
        text = "````\n<!-- also not stripped -->\n````\n"
        result = _strip_comments_preserving_fences(text)
        assert "<!-- also not stripped -->" in result

    def test_no_comments_unchanged(self):
        text = "No comments here.\nJust text.\n"
        result = _strip_comments_preserving_fences(text)
        assert result == text


# ─────────────────────────────────────────────
# extract_and_write (integration)
# ─────────────────────────────────────────────

WELL_FORMED_BODY = """\
## User Story
As a developer, I want to extract issue data.

## Context
This is the context.

### Exception Design
Handle edge cases.

## Integration points
Connect via GitHub API.

## Behavioral Contracts
### Happy path
- Given a valid issue, the script extracts data.
- Given another condition, something else happens.
- Given a third condition, a third thing happens.

## Acceptance Criteria
- [ ] Script extracts behavioral contracts
- [ ] Script writes spec.md
"""


class TestExtractAndWrite:

    def test_writes_both_files(self, tmp_path):
        code = extract_and_write(WELL_FORMED_BODY, tmp_path)
        assert code == 0
        assert (tmp_path / "spec.md").exists()
        assert (tmp_path / "behavioral.md").exists()

    def test_spec_contains_correct_sections(self, tmp_path):
        extract_and_write(WELL_FORMED_BODY, tmp_path)
        spec = (tmp_path / "spec.md").read_text()
        assert "## User Story" in spec
        assert "## Context" in spec
        assert "## Integration points" in spec
        assert "## Behavioral Contracts" not in spec

    def test_behavioral_contains_correct_sections(self, tmp_path):
        extract_and_write(WELL_FORMED_BODY, tmp_path)
        behavioral = (tmp_path / "behavioral.md").read_text()
        assert "## Behavioral Contracts" in behavioral
        assert "## Acceptance Criteria" in behavioral
        assert "## User Story" not in behavioral

    def test_missing_behavioral_contracts_returns_1(self, tmp_path, capsys):
        body = "## User Story\nAs a dev.\n\n## Context\nSome context.\n"
        code = extract_and_write(body, tmp_path)
        assert code == 1
        captured = capsys.readouterr()
        assert "missing behavioral contracts section" in captured.err.lower()

    def test_placeholder_behavioral_returns_1(self, tmp_path, capsys):
        body = "## User Story\nAs a dev.\n\n## Behavioral Contracts\nplaceholder\n\n## Acceptance Criteria\n- [ ] one\n"
        code = extract_and_write(body, tmp_path)
        assert code == 1
        captured = capsys.readouterr()
        assert "placeholder" in captured.err.lower()

    def test_trailing_newline(self, tmp_path):
        extract_and_write(WELL_FORMED_BODY, tmp_path)
        for fname in ["spec.md", "behavioral.md"]:
            content = (tmp_path / fname).read_bytes()
            assert content.endswith(b"\n")
            assert not content.endswith(b"\n\n")

    def test_crlf_normalized(self, tmp_path):
        crlf_body = WELL_FORMED_BODY.replace("\n", "\r\n")
        extract_and_write(crlf_body, tmp_path)
        for fname in ["spec.md", "behavioral.md"]:
            content = (tmp_path / fname).read_bytes()
            assert b"\r\n" not in content

    def test_missing_context_warns_exits_0(self, tmp_path, capsys):
        body = (
            "## User Story\nAs a dev.\n\n"
            "## Behavioral Contracts\n"
            "### Happy path\n"
            "- Contract one.\n"
            "- Contract two.\n"
            "- Contract three.\n\n"
            "## Acceptance Criteria\n- [ ] Item one\n"
        )
        code = extract_and_write(body, tmp_path)
        assert code == 0
        captured = capsys.readouterr()
        assert "missing context section" in captured.err.lower()

    def test_unknown_section_goes_to_spec_with_warning(self, tmp_path, capsys):
        body = (
            "## User Story\nAs a dev.\n\n"
            "## Design Notes\nSome thoughts.\n\n"
            "## Behavioral Contracts\n"
            "- Contract one.\n"
            "- Contract two.\n"
            "- Contract three.\n\n"
            "## Acceptance Criteria\n- [ ] Item one\n"
        )
        code = extract_and_write(body, tmp_path)
        assert code == 0
        spec = (tmp_path / "spec.md").read_text()
        assert "Design Notes" in spec
        captured = capsys.readouterr()
        assert "unknown section" in captured.err.lower()

    def test_duplicate_section_concatenated_with_warning(self, tmp_path, capsys):
        body = (
            "## Behavioral Contracts\n"
            "### First\n"
            "- Contract one.\n"
            "- Contract two.\n"
            "- Contract three.\n\n"
            "## Behavioral Contracts\n"
            "### Second\n"
            "- Contract four.\n"
            "- Contract five.\n"
            "- Contract six.\n\n"
            "## Acceptance Criteria\n- [ ] Item one\n"
        )
        code = extract_and_write(body, tmp_path)
        assert code == 0
        behavioral = (tmp_path / "behavioral.md").read_text()
        assert "First" in behavioral
        assert "Second" in behavioral
        captured = capsys.readouterr()
        assert "duplicate section" in captured.err.lower()
