"""Sealed acceptance tests for orchestration_engine.verdict_parser.

These tests define the behavioral contracts for the shared verdict parser
introduced in issue #678. They are written BEFORE the implementation and
must not be modified to make them pass — the implementation must satisfy
every test as written.

Tests are grouped by spec section:
  - TestPass1StructuredFormat: VERDICT: prefix, last-match-wins, case-insensitive
  - TestPass2FallbackParsing: all markdown formats (backward compatibility)
  - TestPriorityOrdering: REQUEST_CHANGES > ABORT > APPROVE (Pass 2 only)
  - TestShortCircuiting: Pass 1 isolation prevents Pass 2 override
  - TestAllowedVerdicts: allowed_verdicts parameter filtering
  - TestEdgeCases: None, empty, CRLF, snake_case false positive, APPROVED rejection, etc.
  - TestAdversaryIntegration: .upper() contract for AdversaryVerdict compatibility
  - TestFileBased: file_path parameter, fallback to text when file missing/empty
"""

import os

import pytest

from orchestration_engine.verdict_parser import extract_verdict


# ---------------------------------------------------------------------------
# Pass 1 — Structured VERDICT: prefix (last-match-wins)
# ---------------------------------------------------------------------------


class TestPass1StructuredFormat:
    """VERDICT: prefix lines — last match wins, case-insensitive."""

    def test_verdict_request_changes(self):
        text = "Some analysis.\nVERDICT: REQUEST_CHANGES\nCOMMENT: Found issues"
        assert extract_verdict(text=text) == "request_changes"

    def test_verdict_approve(self):
        text = "Looks good.\nVERDICT: APPROVE\nCOMMENT: Looks good"
        assert extract_verdict(text=text) == "approve"

    def test_verdict_abort(self):
        text = "Cannot proceed.\nVERDICT: ABORT\nCOMMENT: Fatal error"
        assert extract_verdict(text=text) == "abort"

    def test_last_match_wins(self):
        """Mid-reasoning APPROVE overridden by final REQUEST_CHANGES."""
        text = (
            "Initial look is fine.\n"
            "VERDICT: APPROVE\n"
            "Wait, found more issues.\n"
            "VERDICT: REQUEST_CHANGES\n"
            "COMMENT: 4 findings\n"
        )
        assert extract_verdict(text=text) == "request_changes"

    def test_last_match_wins_reverse(self):
        """Mid-reasoning REQUEST_CHANGES overridden by final APPROVE."""
        text = (
            "Looks problematic.\n"
            "VERDICT: REQUEST_CHANGES\n"
            "Actually, the issues are cosmetic.\n"
            "VERDICT: APPROVE\n"
            "COMMENT: Minor only\n"
        )
        assert extract_verdict(text=text) == "approve"

    def test_case_insensitive_prefix(self):
        text = "verdict: request_changes\nCOMMENT: issues"
        assert extract_verdict(text=text) == "request_changes"

    def test_case_insensitive_keyword(self):
        text = "Verdict: approve\nCOMMENT: ok"
        assert extract_verdict(text=text) == "approve"

    def test_mixed_case(self):
        text = "VERDICT: Approve\nCOMMENT: ok"
        assert extract_verdict(text=text) == "approve"

    def test_markdown_in_verdict_line(self):
        """Verdict line itself may contain markdown: Verdict: **APPROVE**"""
        text = "Analysis done.\nVerdict: **APPROVE**\nCOMMENT: all good"
        assert extract_verdict(text=text) == "approve"

    def test_returns_lowercase(self):
        text = "VERDICT: REQUEST_CHANGES"
        result = extract_verdict(text=text)
        assert result == "request_changes"
        assert result == result.lower()


# ---------------------------------------------------------------------------
# Pass 2 — Fallback parsing (backward compatibility, all markdown formats)
# ---------------------------------------------------------------------------


class TestPass2FallbackParsing:
    """No VERDICT: line present — falls through to regex scan."""

    def test_plain_text(self):
        assert extract_verdict(text="REQUEST_CHANGES") == "request_changes"

    def test_plain_text_approve(self):
        assert extract_verdict(text="APPROVE") == "approve"

    def test_plain_text_abort(self):
        assert extract_verdict(text="ABORT") == "abort"

    def test_markdown_bold(self):
        assert extract_verdict(text="**REQUEST_CHANGES**") == "request_changes"

    def test_markdown_italic(self):
        assert extract_verdict(text="*APPROVE*") == "approve"

    def test_markdown_bold_italic(self):
        assert extract_verdict(text="***REQUEST_CHANGES***") == "request_changes"

    def test_underline_markdown(self):
        assert extract_verdict(text="__APPROVE__") == "approve"

    def test_heading(self):
        assert extract_verdict(text="# REQUEST_CHANGES") == "request_changes"

    def test_heading_h2(self):
        assert extract_verdict(text="## APPROVE") == "approve"

    def test_blockquote(self):
        assert extract_verdict(text="> REQUEST_CHANGES") == "request_changes"

    def test_dash_list(self):
        assert extract_verdict(text="- REQUEST_CHANGES") == "request_changes"

    def test_backtick(self):
        assert extract_verdict(text="`REQUEST_CHANGES`") == "request_changes"

    def test_bullet(self):
        assert extract_verdict(text="* APPROVE") == "approve"

    def test_numbered_list(self):
        assert extract_verdict(text="1. **REQUEST_CHANGES**") == "request_changes"

    def test_trailing_colon(self):
        assert extract_verdict(text="REQUEST_CHANGES:") == "request_changes"

    def test_trailing_period(self):
        assert extract_verdict(text="APPROVE.") == "approve"

    def test_trailing_dash_details(self):
        assert extract_verdict(text="REQUEST_CHANGES — details follow") == "request_changes"

    def test_mixed_formatting(self):
        assert extract_verdict(text="**_APPROVE_**") == "approve"

    def test_leading_whitespace(self):
        assert extract_verdict(text="   **APPROVE**") == "approve"

    def test_conversational_prefix_verdict(self):
        """Conversational: 'Verdict: REQUEST_CHANGES' (no structured VERDICT: pass1 match
        because it's also a valid pass1 match — but either way it works)."""
        assert extract_verdict(text="Verdict: REQUEST_CHANGES") == "request_changes"

    def test_conversational_prefix_decision(self):
        assert extract_verdict(text="Decision: APPROVE") == "approve"

    def test_bold_verdict_from_issue_evidence(self):
        """The exact format from the bug report evidence."""
        text = (
            "**REQUEST_CHANGES** — 4 findings written to "
            "`/tmp/output/spec_adversary.md`:\n\n"
            "1. **[leakage]** BC-10.1 exposes `ValueError`\n"
        )
        assert extract_verdict(text=text) == "request_changes"


# ---------------------------------------------------------------------------
# Priority ordering (Pass 2 only — REQUEST_CHANGES > ABORT > APPROVE)
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    """Pass 2 collects all keywords across all lines, applies priority."""

    def test_request_changes_over_approve(self):
        text = "APPROVE would be premature\nREQUEST_CHANGES"
        assert extract_verdict(text=text) == "request_changes"

    def test_abort_over_approve(self):
        text = "APPROVE\nSome text\nABORT"
        assert extract_verdict(text=text) == "abort"

    def test_request_changes_over_abort(self):
        text = "ABORT\nREQUEST_CHANGES"
        assert extract_verdict(text=text) == "request_changes"

    def test_all_three_present(self):
        text = "APPROVE\nABORT\nREQUEST_CHANGES"
        assert extract_verdict(text=text) == "request_changes"

    def test_priority_regardless_of_order(self):
        text = "REQUEST_CHANGES\nAPPROVE\nABORT"
        assert extract_verdict(text=text) == "request_changes"


# ---------------------------------------------------------------------------
# Short-circuiting — Pass 1 isolation prevents Pass 2 override
# ---------------------------------------------------------------------------


class TestShortCircuiting:
    """Pass 1 match must prevent Pass 2 from overriding it."""

    def test_pass1_approve_not_overridden_by_pass2_request_changes(self):
        """VERDICT: APPROVE in structured line + **REQUEST_CHANGES** in body.
        Pass 1 wins; Pass 2 never evaluated."""
        text = (
            "Found some issues with **REQUEST_CHANGES** recommended.\n"
            "But actually, on reflection:\n"
            "VERDICT: APPROVE\n"
            "COMMENT: Issues are cosmetic only\n"
        )
        assert extract_verdict(text=text) == "approve"

    def test_pass1_request_changes_not_overridden_by_pass2_approve(self):
        text = (
            "APPROVE seems tempting but:\n"
            "VERDICT: REQUEST_CHANGES\n"
            "COMMENT: Critical issues found\n"
        )
        assert extract_verdict(text=text) == "request_changes"

    def test_no_pass1_falls_through_to_pass2(self):
        """No VERDICT: line → Pass 2 kicks in."""
        text = "**REQUEST_CHANGES**\nSome explanation here."
        assert extract_verdict(text=text) == "request_changes"

    def test_pass1_abort_not_overridden(self):
        """VERDICT: ABORT with REQUEST_CHANGES in body text."""
        text = (
            "REQUEST_CHANGES could be an option but this is fatal.\n"
            "VERDICT: ABORT\n"
            "COMMENT: Cannot proceed\n"
        )
        assert extract_verdict(text=text) == "abort"


# ---------------------------------------------------------------------------
# allowed_verdicts filtering
# ---------------------------------------------------------------------------


class TestAllowedVerdicts:
    """allowed_verdicts parameter restricts which verdicts are returned."""

    def test_abort_filtered_for_adversary(self):
        text = "VERDICT: ABORT"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result is None

    def test_approve_allowed(self):
        text = "VERDICT: APPROVE"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result == "approve"

    def test_request_changes_allowed(self):
        text = "VERDICT: REQUEST_CHANGES"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result == "request_changes"

    def test_no_restriction_allows_abort(self):
        text = "VERDICT: ABORT"
        assert extract_verdict(text=text) == "abort"

    def test_no_restriction_default_none(self):
        text = "VERDICT: APPROVE"
        assert extract_verdict(text=text, allowed_verdicts=None) == "approve"

    def test_pass2_abort_filtered(self):
        """Pass 2 fallback also respects allowed_verdicts."""
        text = "ABORT"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result is None

    def test_pass2_priority_with_filter(self):
        """Pass 2 with ABORT filtered: next priority is APPROVE if no REQUEST_CHANGES."""
        text = "ABORT\nAPPROVE"
        result = extract_verdict(
            text=text, allowed_verdicts={"approve", "request_changes"}
        )
        assert result == "approve"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions, malformed input, non-matching patterns."""

    def test_none_input(self):
        assert extract_verdict(text=None) is None

    def test_empty_string(self):
        assert extract_verdict(text="") is None

    def test_no_verdict_in_text(self):
        assert extract_verdict(text="This is just regular text.") is None

    def test_crlf_line_endings(self):
        text = "VERDICT: APPROVE\r\nCOMMENT: ok\r\n"
        assert extract_verdict(text=text) == "approve"

    def test_crlf_plain_verdict(self):
        assert extract_verdict(text="APPROVE\r\n") == "approve"

    def test_mid_sentence_keyword_not_matched(self):
        """'The team should APPROVE this' — keyword not at line start."""
        assert extract_verdict(text="The team should APPROVE this") is None

    def test_snake_case_suffix_rejected(self):
        """REQUEST_CHANGES_ARE_BAD must not match."""
        assert extract_verdict(text="REQUEST_CHANGES_ARE_BAD") is None

    def test_approved_rejected(self):
        """APPROVED must not match as APPROVE."""
        assert extract_verdict(text="APPROVED") is None

    def test_approving_rejected(self):
        assert extract_verdict(text="APPROVING") is None

    def test_requestchanges_no_underscore_rejected(self):
        """REQUESTCHANGES (no underscore) must not match."""
        assert extract_verdict(text="REQUESTCHANGES") is None

    def test_underline_markdown_preserves_keyword_underscore(self):
        """__REQUEST_CHANGES__ — strips surrounding __ without destroying keyword."""
        assert extract_verdict(text="__REQUEST_CHANGES__") == "request_changes"

    def test_whitespace_only(self):
        assert extract_verdict(text="   \n\n  \t  ") is None

    def test_multiline_no_verdict(self):
        text = "Line 1\nLine 2\nLine 3\nNo verdict here."
        assert extract_verdict(text=text) is None

    def test_verdict_prefix_without_keyword(self):
        """VERDICT: followed by nonsense should not match."""
        assert extract_verdict(text="VERDICT: FOOBAR") is None

    def test_both_params_none(self):
        assert extract_verdict() is None

    def test_verdict_with_extra_spaces(self):
        """VERDICT:   APPROVE  (extra spaces around keyword)."""
        assert extract_verdict(text="VERDICT:   APPROVE  ") == "approve"


# ---------------------------------------------------------------------------
# File-based verdict reading
# ---------------------------------------------------------------------------


class TestFileBased:
    """file_path parameter: read verdict from output file."""

    def test_file_verdict_structured(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("Analysis here.\nVERDICT: REQUEST_CHANGES\nCOMMENT: issues\n")
        assert extract_verdict(file_path=str(f)) == "request_changes"

    def test_file_verdict_approve(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("All good.\nVERDICT: APPROVE\nCOMMENT: clean\n")
        assert extract_verdict(file_path=str(f)) == "approve"

    def test_file_priority_over_text(self, tmp_path):
        """file_path takes priority over text when both provided."""
        f = tmp_path / "output.md"
        f.write_text("VERDICT: APPROVE\nCOMMENT: ok\n")
        result = extract_verdict(text="VERDICT: REQUEST_CHANGES", file_path=str(f))
        assert result == "approve"

    def test_file_missing_falls_back_to_text(self):
        """When file doesn't exist, fall back to text param."""
        result = extract_verdict(
            text="VERDICT: REQUEST_CHANGES",
            file_path="/nonexistent/path/output.md",
        )
        assert result == "request_changes"

    def test_file_empty_falls_back_to_text(self, tmp_path):
        """When file is empty (0 bytes), fall back to text param."""
        f = tmp_path / "empty.md"
        f.write_text("")
        result = extract_verdict(text="VERDICT: APPROVE", file_path=str(f))
        assert result == "approve"

    def test_file_none_falls_back_to_text(self):
        """When file_path is None, use text param."""
        result = extract_verdict(text="VERDICT: ABORT", file_path=None)
        assert result == "abort"

    def test_file_with_allowed_verdicts(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("VERDICT: ABORT\nCOMMENT: fatal\n")
        result = extract_verdict(
            file_path=str(f), allowed_verdicts={"approve", "request_changes"}
        )
        assert result is None

    def test_file_last_match_wins(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text(
            "VERDICT: APPROVE\n"
            "More analysis...\n"
            "VERDICT: REQUEST_CHANGES\n"
            "COMMENT: changed mind\n"
        )
        assert extract_verdict(file_path=str(f)) == "request_changes"

    def test_file_pass2_fallback(self, tmp_path):
        """File with no VERDICT: line falls through to Pass 2."""
        f = tmp_path / "output.md"
        f.write_text("**REQUEST_CHANGES** — found 3 issues\n")
        assert extract_verdict(file_path=str(f)) == "request_changes"

    def test_file_with_markdown_bold_verdict_line(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("Verdict: **APPROVE**\nCOMMENT: clean\n")
        assert extract_verdict(file_path=str(f)) == "approve"


# ---------------------------------------------------------------------------
# Adversary integration — .upper() contract
# ---------------------------------------------------------------------------


class TestAdversaryIntegration:
    """Verify extract_verdict() returns lowercase so adversary can .upper()."""

    def test_return_value_is_lowercase(self):
        result = extract_verdict(text="VERDICT: REQUEST_CHANGES")
        assert result == "request_changes"
        assert result == result.lower()

    def test_upper_produces_adversary_contract(self):
        """AdversaryVerdict.verdict stores UPPERCASE — parser returns lowercase,
        caller does .upper()."""
        result = extract_verdict(text="VERDICT: REQUEST_CHANGES")
        assert result is not None
        assert result.upper() == "REQUEST_CHANGES"

    def test_approve_upper_contract(self):
        result = extract_verdict(text="VERDICT: APPROVE")
        assert result is not None
        assert result.upper() == "APPROVE"

    def test_adversary_bold_verdict_from_evidence(self):
        """The exact format from the bug report: **REQUEST_CHANGES** — 4 findings."""
        text = (
            "**REQUEST_CHANGES** — 4 findings written to "
            "`/tmp/output/spec_adversary.md`:\n\n"
            "1. **[leakage]** BC-10.1 exposes `ValueError`\n"
            "2. **[vague]** BC-3.2 lacks specificity\n"
            "3. **[missing_edge_case]** No test for empty input\n"
            "4. **[divergence]** Implementation diverges from spec\n"
        )
        result = extract_verdict(text=text)
        assert result == "request_changes"
        assert result.upper() == "REQUEST_CHANGES"

    def test_adversary_allowed_verdicts(self):
        """spec_adversary passes allowed_verdicts={"approve", "request_changes"}."""
        result = extract_verdict(
            text="VERDICT: APPROVE",
            allowed_verdicts={"approve", "request_changes"},
        )
        assert result == "approve"

    def test_adversary_abort_filtered(self):
        result = extract_verdict(
            text="VERDICT: ABORT",
            allowed_verdicts={"approve", "request_changes"},
        )
        assert result is None

    def test_no_verdict_returns_none_for_safe_default(self):
        """parse_adversary_output() checks for None and defaults to REQUEST_CHANGES.
        The parser itself must return None, not a default."""
        assert extract_verdict(text="No verdict here at all.") is None
        assert extract_verdict(text=None) is None
        assert extract_verdict(text="") is None
