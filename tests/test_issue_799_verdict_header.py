"""Regression tests for issue #799 — spec_adversary verdict extraction must
skip leading markdown headers.

Background
----------
Production run ``6bb0349c`` showed ``spec_adversary`` failing verdict
extraction in rounds 1 and 2 because the model started its response with
``# Adversary Review: ...`` instead of the verdict word on line 1. The
pipeline fell through to the ``success`` fallback (which maps to ``spec``,
looping), wasting ~$3.80.

These tests assert the behavioral contracts from issue #799 against the
canonical ``extract_verdict()`` API in
``src/orchestration_engine/verdict_parser.py`` (single source-of-truth per
issue #836). They are written BEFORE the implementation pass and MUST NOT be
modified to make them pass — any implementation change must satisfy every
test as written.

Tests are grouped by behavioral contract from
``.orchemist/runs/issue-799/behavioral.md``:

  - TestBC_799_1_H1HeaderPrefix:     H1 prefix + verdict on later own-line
  - TestBC_799_2_H2HeaderPrefix:     ## Round N prefix + verdict on later own-line
  - TestBC_799_3_NestedHeaders:      H1 + H2 nested + verdict at end
  - TestBC_799_4_Line1HappyPath:     Line-1 verdict path unchanged
  - TestBC_799_5_NoVerdictReturnsNone: Header-only-no-verdict returns None
  - TestBC_799_6_AllowedVerdictsFilter: filter honored on header-prefixed input
  - TestBC_799_6a_HeaderPlusPass1Structured: header + ``VERDICT: X`` line
  - TestBC_799_6b_HeaderPlusCRLF:    header + CRLF line endings
  - TestBC_799_6c_HeaderViaFilePath: header-prefixed input via file_path
  - TestBC_799_7_CasingContract:     return value is lowercase
"""

from __future__ import annotations

import pytest

from orchestration_engine.verdict_parser import extract_verdict


# ---------------------------------------------------------------------------
# BC-799.1 — Markdown H1 prefix with verdict on later own-line
# ---------------------------------------------------------------------------


class TestBC_799_1_H1HeaderPrefix:
    """Input begins with ``# <heading>`` (no verdict in heading text),
    blank line(s), then a line whose only non-whitespace content is the
    verdict word -> extractor returns the lowercase verdict."""

    def test_h1_then_approve_on_own_line(self):
        text = "# Adversary Review: Foo\n\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"

    def test_h1_then_request_changes_on_own_line(self):
        text = "# Adversary Review: Some Long Title\n\nREQUEST_CHANGES\n"
        assert extract_verdict(text=text) == "request_changes"

    def test_h1_then_abort_on_own_line(self):
        text = "# Adversary Review: Foo\n\nABORT\n"
        assert extract_verdict(text=text) == "abort"

    def test_h1_with_long_title_then_verdict(self):
        """Exact pattern reported in issue #799 production logs."""
        text = (
            "# Adversary Review: Companion Post Editor for Article "
            "Preview Modal\n\nAPPROVE\n"
        )
        assert extract_verdict(text=text) == "approve"

    def test_h1_then_multiple_blank_lines_then_verdict(self):
        """Multiple blank lines between heading and verdict still works."""
        text = "# Adversary Review: Foo\n\n\n\nREQUEST_CHANGES\n"
        assert extract_verdict(text=text) == "request_changes"


# ---------------------------------------------------------------------------
# BC-799.2 — Markdown H2 section prefix (## Round N) with verdict
# ---------------------------------------------------------------------------


class TestBC_799_2_H2HeaderPrefix:
    """Input begins with a level-2 markdown header (no verdict in heading
    text), optionally followed by prose, then a line whose only non-whitespace
    content is the verdict word -> extractor returns the lowercase verdict."""

    def test_h2_round_1_then_approve(self):
        text = "## Round 1\n\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"

    def test_h2_round_1_then_prose_then_request_changes(self):
        text = "## Round 1\n\nSome prose here.\n\nREQUEST_CHANGES\n"
        assert extract_verdict(text=text) == "request_changes"

    def test_h2_verdict_section_then_verdict(self):
        text = "## Verdict\n\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"

    def test_h2_findings_section_then_verdict(self):
        text = "## Findings\n\nNo blocking findings.\n\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"

    def test_h3_then_verdict(self):
        """Level-3 headers (`### Final Verdict`) also skipped — covers
        issue acceptance criterion: 'scans past markdown headers #, ##, ###'."""
        text = "### Final Verdict\n\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"


# ---------------------------------------------------------------------------
# BC-799.3 — Combined H1 + H2 prefix with verdict at end
# ---------------------------------------------------------------------------


class TestBC_799_3_NestedHeaders:
    """Input begins with H1, then H2 (nested headers), then prose with no
    verdict word, ending with a verdict word on its own line. Extractor
    returns the lowercase verdict."""

    def test_h1_then_h2_then_prose_then_request_changes(self):
        text = (
            "# Adversary Review: Foo\n"
            "\n"
            "## Round 1\n"
            "\n"
            "Reviewed the spec.\n"
            "\n"
            "REQUEST_CHANGES\n"
        )
        assert extract_verdict(text=text) == "request_changes"

    def test_h1_then_h2_then_long_prose_then_approve(self):
        text = (
            "# Adversary Review: Companion Post Editor\n"
            "\n"
            "## Round 2\n"
            "\n"
            "Reviewed all contracts. Specificity is tight, no leakage detected,\n"
            "trivial satisfaction guarded, and edge cases covered.\n"
            "\n"
            "APPROVE\n"
        )
        assert extract_verdict(text=text) == "approve"

    def test_h1_then_h2_then_abort(self):
        text = (
            "# Adversary Review: Foo\n"
            "\n"
            "## Round 1\n"
            "\n"
            "Cannot proceed for the following reasons.\n"
            "\n"
            "ABORT\n"
        )
        assert extract_verdict(text=text) == "abort"


# ---------------------------------------------------------------------------
# BC-799.4 — Line-1 happy path is preserved
# ---------------------------------------------------------------------------


class TestBC_799_4_Line1HappyPath:
    """Input begins with the verdict word as the first non-blank line.
    Behavior must be identical to current parser output for this case —
    no regression introduced by the fix."""

    def test_line1_approve_alone(self):
        assert extract_verdict(text="APPROVE\n") == "approve"

    def test_line1_approve_with_body(self):
        assert extract_verdict(text="APPROVE\nLooks good.\n") == "approve"

    def test_line1_request_changes_with_findings(self):
        text = "REQUEST_CHANGES\n[BLOCKER] x\n"
        assert extract_verdict(text=text) == "request_changes"

    def test_line1_abort_with_explanation(self):
        text = "ABORT\nCannot proceed.\n"
        assert extract_verdict(text=text) == "abort"

    def test_line1_with_leading_blank(self):
        """Leading blank line before line-1 verdict still works."""
        text = "\nAPPROVE\n"
        assert extract_verdict(text=text) == "approve"


# ---------------------------------------------------------------------------
# BC-799.5 — No verdict anywhere returns None
# ---------------------------------------------------------------------------


class TestBC_799_5_NoVerdictReturnsNone:
    """Input contains NO standalone verdict word at start of any meaningful
    line (after stripping markdown leaders). Extractor returns None — the
    existing safe-fallback contract is preserved."""

    def test_h1_then_only_findings_no_verdict(self):
        text = (
            "# Adversary Review: Foo\n"
            "\n"
            "[vague] X is too broad\n"
            "[trivial] Y\n"
        )
        assert extract_verdict(text=text) is None

    def test_h1_then_prose_no_verdict(self):
        text = "# Some output\n\nNo verdict here.\n"
        assert extract_verdict(text=text) is None

    def test_h2_then_prose_no_verdict(self):
        text = "## Round 1\n\nThe spec looks fine but I have concerns.\n"
        assert extract_verdict(text=text) is None

    def test_h1_then_h2_then_prose_only(self):
        text = (
            "# Adversary Review: Foo\n"
            "\n"
            "## Round 1\n"
            "\n"
            "I have no strong opinion either way.\n"
        )
        assert extract_verdict(text=text) is None


# ---------------------------------------------------------------------------
# BC-799.6 — allowed_verdicts filter honored on header-prefixed inputs
# ---------------------------------------------------------------------------


class TestBC_799_6_AllowedVerdictsFilter:
    """allowed_verdicts filter excludes verdicts not in the set, even when
    the verdict appears after a markdown header. Mirrors existing filter
    contract for non-header-prefixed inputs."""

    def test_abort_filtered_after_h1_header(self):
        text = "# Adversary Review: Foo\n\nABORT\n"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result is None

    def test_approve_allowed_after_h1_header(self):
        text = "# Adversary Review: Foo\n\nAPPROVE\n"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result == "approve"

    def test_request_changes_allowed_after_h1_header(self):
        text = "# Adversary Review: Foo\n\nREQUEST_CHANGES\n"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result == "request_changes"


# ---------------------------------------------------------------------------
# BC-799.6a — Markdown header + Pass-1 structured VERDICT line
# ---------------------------------------------------------------------------


class TestBC_799_6a_HeaderPlusPass1Structured:
    """Markdown header prefix does not block the structured Pass-1
    'VERDICT: <keyword>' extraction path."""

    def test_h1_then_structured_verdict_approve(self):
        text = "# Adversary Review: Foo\n\nVERDICT: APPROVE\n"
        assert extract_verdict(text=text) == "approve"

    def test_h1_then_h2_then_structured_verdict_request_changes(self):
        text = (
            "# Adversary Review: Foo\n"
            "\n"
            "## Round 1\n"
            "\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        assert extract_verdict(text=text) == "request_changes"

    def test_h1_then_structured_verdict_with_markdown_styling(self):
        text = "# Foo\n\nVerdict: **APPROVE**\n"
        assert extract_verdict(text=text) == "approve"


# ---------------------------------------------------------------------------
# BC-799.6b — Markdown header + CRLF line endings
# ---------------------------------------------------------------------------


class TestBC_799_6b_HeaderPlusCRLF:
    """CRLF line endings do not break header-prefix handling."""

    def test_h1_crlf_then_approve(self):
        text = "# Adversary Review: Foo\r\n\r\nAPPROVE\r\n"
        assert extract_verdict(text=text) == "approve"

    def test_h1_crlf_then_request_changes(self):
        text = "# Adversary Review: Foo\r\n\r\nREQUEST_CHANGES\r\n"
        assert extract_verdict(text=text) == "request_changes"


# ---------------------------------------------------------------------------
# BC-799.6c — file_path with header-prefixed content
# ---------------------------------------------------------------------------


class TestBC_799_6c_HeaderViaFilePath:
    """Header-prefix handling is content-level, not call-shape-level —
    extract_verdict(file_path=...) on header-prefixed content returns the
    same lowercase verdict as the text= form."""

    def test_file_with_h1_header_then_approve(self, tmp_path):
        f = tmp_path / "spec_adversary.md"
        f.write_text("# Adversary Review: Foo\n\nAPPROVE\n")
        assert extract_verdict(file_path=str(f)) == "approve"

    def test_file_with_h1_then_h2_then_verdict(self, tmp_path):
        f = tmp_path / "spec_adversary.md"
        f.write_text(
            "# Adversary Review: Foo\n"
            "\n"
            "## Round 1\n"
            "\n"
            "REQUEST_CHANGES\n"
        )
        assert extract_verdict(file_path=str(f)) == "request_changes"

    def test_file_with_no_verdict_returns_none(self, tmp_path):
        f = tmp_path / "spec_adversary.md"
        f.write_text("# Adversary Review: Foo\n\n[vague] X\n[trivial] Y\n")
        assert extract_verdict(file_path=str(f)) is None


# ---------------------------------------------------------------------------
# BC-799.7 — Return value casing contract is preserved
# ---------------------------------------------------------------------------


class TestBC_799_7_CasingContract:
    """Return value is always lowercase — matches _VERDICT_KEYWORDS set."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("# Foo\n\nAPPROVE\n", "approve"),
            ("# Foo\n\nREQUEST_CHANGES\n", "request_changes"),
            ("# Foo\n\nABORT\n", "abort"),
            ("## Round 1\n\nAPPROVE\n", "approve"),
            ("APPROVE\n", "approve"),
        ],
    )
    def test_return_value_is_lowercase(self, text, expected):
        result = extract_verdict(text=text)
        assert result == expected
        assert result is not None
        assert result == result.lower()
