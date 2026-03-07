"""
tests/test_review_parser.py — Comprehensive tests for review_parser.py

Mirrors the structure and coverage depth of test_output_parser.py.

Test classes:
    TestModuleImport         — module exists, exports symbols, no third-party deps
    TestDataStructures       — Severity, ReviewIssue, ReviewResult shapes/fields
    TestParserSignature      — parse_review_output signature and graceful degradation
    TestVerdictParsing       — APPROVE / REQUEST_CHANGES detection; malformed line 1
    TestIssueLineParsing     — tag format recognition; all four Severity values
    TestHasIssuesFlag        — has_issues is always consistent with bool(issues)
    TestGracefulDegradation  — empty/whitespace/non-string inputs never raise
    TestSeverityOrdering     — Severity enum values match expected names
    TestEdgeCases            — blank lines, mixed content, LLM prose commentary
    TestRawPreservation      — raw_text and raw fields preserved verbatim

All tests are independent — no shared mutable state.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — AC-1 equivalent
# ---------------------------------------------------------------------------
from orchestration_engine.review_parser import (
    ReviewIssue,
    ReviewResult,
    Severity,
    parse_review_output,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _review(verdict: str, *issues: str) -> str:
    """Build a well-formed review string."""
    lines = [verdict] + list(issues)
    return "\n".join(lines) + "\n"


# ===========================================================================
# TestModuleImport
# ===========================================================================


class TestModuleImport(unittest.TestCase):
    """Module-level checks: symbols exported, no third-party deps."""

    def test_symbols_exported(self) -> None:
        """All public names are importable and of the expected type."""
        self.assertTrue(callable(parse_review_output))
        self.assertIsInstance(Severity, type)
        self.assertIsInstance(ReviewIssue, type)
        self.assertIsInstance(ReviewResult, type)

    def test_no_third_party_imports(self) -> None:
        """Module must not introduce non-stdlib dependencies.

        Checks for actual import statements (``import X`` / ``from X``) rather
        than bare substring presence, because the module docstring may reference
        third-party names in text (e.g. referencing a ``.yaml`` template file).
        """
        import orchestration_engine.review_parser as _mod

        source = Path(_mod.__file__).read_text()
        for lib in ["pydantic", "requests", "attrs", "numpy", "click"]:
            self.assertNotIn(
                f"import {lib}", source, f"Unexpected third-party import: {lib}"
            )
            self.assertNotIn(
                f"from {lib}", source, f"Unexpected third-party import: {lib}"
            )
        # yaml appears as part of the .yaml file extension in the docstring — check
        # only actual import statements, not substring occurrence.
        self.assertNotIn("import yaml", source, "Unexpected third-party import: yaml")
        self.assertNotIn("from yaml", source, "Unexpected third-party import: yaml")

    def test_all_exported_in_dunder_all(self) -> None:
        """All public names are listed in __all__."""
        import orchestration_engine.review_parser as _mod

        for name in ("Severity", "ReviewIssue", "ReviewResult", "parse_review_output"):
            self.assertIn(name, _mod.__all__)


# ===========================================================================
# TestDataStructures
# ===========================================================================


class TestDataStructures(unittest.TestCase):
    """ReviewIssue and ReviewResult shapes and field types."""

    def test_severity_members(self) -> None:
        """Severity has exactly the four expected members."""
        members = {m.name for m in Severity}
        self.assertEqual(members, {"BLOCKER", "MAJOR", "MINOR", "NITPICK"})

    def test_review_issue_fields(self) -> None:
        """ReviewIssue can be constructed with expected fields."""
        issue = ReviewIssue(
            severity=Severity.MAJOR,
            category="correctness",
            description="Something is wrong",
            raw="[MAJOR][correctness] Something is wrong",
        )
        self.assertIs(issue.severity, Severity.MAJOR)
        self.assertEqual(issue.category, "correctness")
        self.assertEqual(issue.description, "Something is wrong")
        self.assertEqual(issue.raw, "[MAJOR][correctness] Something is wrong")

    def test_review_result_has_issues_true(self) -> None:
        """has_issues is True when issues list is non-empty."""
        issue = ReviewIssue(
            severity=Severity.BLOCKER,
            category="security",
            description="Injection risk",
            raw="[BLOCKER][security] Injection risk",
        )
        result = ReviewResult(verdict="REQUEST_CHANGES", issues=[issue], raw_text="x")
        self.assertTrue(result.has_issues)

    def test_review_result_has_issues_false(self) -> None:
        """has_issues is False when issues list is empty."""
        result = ReviewResult(verdict="APPROVE", issues=[], raw_text="APPROVE\n")
        self.assertFalse(result.has_issues)

    def test_review_result_raw_text_preserved(self) -> None:
        """raw_text is stored exactly as provided."""
        raw = "APPROVE\n   extra whitespace   \n"
        result = ReviewResult(verdict="APPROVE", issues=[], raw_text=raw)
        self.assertEqual(result.raw_text, raw)


# ===========================================================================
# TestParserSignature
# ===========================================================================


class TestParserSignature(unittest.TestCase):
    """parse_review_output always returns ReviewResult, never raises."""

    def test_returns_review_result_instance(self) -> None:
        result = parse_review_output("APPROVE\n")
        self.assertIsInstance(result, ReviewResult)

    def test_empty_string(self) -> None:
        result = parse_review_output("")
        self.assertIsInstance(result, ReviewResult)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.issues, [])

    def test_whitespace_only(self) -> None:
        result = parse_review_output("   \n\t\n   ")
        self.assertIsInstance(result, ReviewResult)
        self.assertIsNone(result.verdict)

    def test_non_string_int(self) -> None:
        result = parse_review_output(42)  # type: ignore[arg-type]
        self.assertIsInstance(result, ReviewResult)

    def test_non_string_none(self) -> None:
        result = parse_review_output(None)  # type: ignore[arg-type]
        self.assertIsInstance(result, ReviewResult)

    def test_non_string_list(self) -> None:
        result = parse_review_output(["APPROVE"])  # type: ignore[arg-type]
        self.assertIsInstance(result, ReviewResult)


# ===========================================================================
# TestVerdictParsing
# ===========================================================================


class TestVerdictParsing(unittest.TestCase):
    """Verdict line extraction: APPROVE, REQUEST_CHANGES, and bad values."""

    def test_approve_verdict(self) -> None:
        result = parse_review_output("APPROVE\n")
        self.assertEqual(result.verdict, "APPROVE")

    def test_request_changes_verdict(self) -> None:
        result = parse_review_output("REQUEST_CHANGES\n")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")

    def test_approve_with_issues_is_none_issues(self) -> None:
        """APPROVE with no issue lines → has_issues is False."""
        result = parse_review_output("APPROVE\n")
        self.assertFalse(result.has_issues)

    def test_verdict_with_leading_whitespace(self) -> None:
        """Whitespace before verdict token is stripped."""
        result = parse_review_output("  APPROVE\n")
        self.assertEqual(result.verdict, "APPROVE")

    def test_verdict_with_trailing_whitespace(self) -> None:
        result = parse_review_output("REQUEST_CHANGES   \n")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")

    def test_unrecognised_verdict_returns_none(self) -> None:
        """First line with unknown token → verdict=None."""
        result = parse_review_output("LGTM\n[MINOR][style] nit\n")
        self.assertIsNone(result.verdict)

    def test_unrecognised_verdict_still_parses_issues(self) -> None:
        """Even with bad verdict, issue lines after it are parsed."""
        result = parse_review_output("LGTM\n[MINOR][style] nit here\n")
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.MINOR)

    def test_blank_leading_lines_skipped(self) -> None:
        """Blank lines before the verdict are skipped; verdict still found."""
        result = parse_review_output("\n\nAPPROVE\n")
        self.assertEqual(result.verdict, "APPROVE")

    def test_no_lines_at_all(self) -> None:
        result = parse_review_output("")
        self.assertIsNone(result.verdict)
        self.assertEqual(result.issues, [])


# ===========================================================================
# TestIssueLineParsing
# ===========================================================================


class TestIssueLineParsing(unittest.TestCase):
    """Tag format recognition and Severity mapping."""

    def _parse_single(self, tag_line: str) -> ReviewIssue:
        """Helper: parse a review with one issue line, return that issue."""
        text = f"REQUEST_CHANGES\n{tag_line}\n"
        result = parse_review_output(text)
        self.assertEqual(len(result.issues), 1, f"Expected 1 issue from: {tag_line!r}")
        return result.issues[0]

    def test_blocker_severity(self) -> None:
        issue = self._parse_single("[BLOCKER][security] SQL injection in db.py:42")
        self.assertEqual(issue.severity, Severity.BLOCKER)
        self.assertEqual(issue.category, "security")
        self.assertEqual(issue.description, "SQL injection in db.py:42")

    def test_major_severity(self) -> None:
        issue = self._parse_single("[MAJOR][correctness] Wrong return type")
        self.assertEqual(issue.severity, Severity.MAJOR)
        self.assertEqual(issue.category, "correctness")

    def test_minor_severity(self) -> None:
        issue = self._parse_single("[MINOR][style] Missing docstring")
        self.assertEqual(issue.severity, Severity.MINOR)
        self.assertEqual(issue.category, "style")

    def test_nitpick_severity(self) -> None:
        issue = self._parse_single("[NITPICK][style] Trailing whitespace")
        self.assertEqual(issue.severity, Severity.NITPICK)

    def test_lower_case_severity_normalised(self) -> None:
        """Severity token is upper-cased before lookup."""
        issue = self._parse_single("[blocker][security] Case-insensitive check")
        self.assertEqual(issue.severity, Severity.BLOCKER)

    def test_mixed_case_severity_normalised(self) -> None:
        issue = self._parse_single("[Minor][style] Mixed case severity")
        self.assertEqual(issue.severity, Severity.MINOR)

    def test_category_with_hyphen(self) -> None:
        issue = self._parse_single("[MAJOR][backward-compat] Removed public API")
        self.assertEqual(issue.category, "backward-compat")

    def test_description_preserved(self) -> None:
        desc = "uses string concatenation — parameterize queries in db.py:99"
        issue = self._parse_single(f"[BLOCKER][security] {desc}")
        self.assertEqual(issue.description, desc)

    def test_unknown_severity_skipped(self) -> None:
        """Unknown severity token → issue silently dropped."""
        text = "REQUEST_CHANGES\n[CRITICAL][security] Very bad\n"
        result = parse_review_output(text)
        self.assertEqual(result.issues, [])

    def test_multiple_issues_order_preserved(self) -> None:
        text = (
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] Issue one\n"
            "[MAJOR][correctness] Issue two\n"
            "[MINOR][style] Issue three\n"
            "[NITPICK][style] Issue four\n"
        )
        result = parse_review_output(text)
        self.assertEqual(len(result.issues), 4)
        self.assertEqual(result.issues[0].severity, Severity.BLOCKER)
        self.assertEqual(result.issues[1].severity, Severity.MAJOR)
        self.assertEqual(result.issues[2].severity, Severity.MINOR)
        self.assertEqual(result.issues[3].severity, Severity.NITPICK)

    def test_raw_field_is_original_line(self) -> None:
        """ReviewIssue.raw must be the unmodified original line."""
        line = "[MAJOR][correctness] parse_output() returns None"
        text = f"REQUEST_CHANGES\n{line}\n"
        result = parse_review_output(text)
        self.assertEqual(result.issues[0].raw, line)

    def test_indented_tag_line(self) -> None:
        """Tag lines with leading whitespace are still parsed."""
        issue = self._parse_single("  [MINOR][style] Indented tag line")
        self.assertEqual(issue.severity, Severity.MINOR)

    def test_malformed_tag_no_description_skipped(self) -> None:
        """Tag line with no description after ] → skipped."""
        text = "REQUEST_CHANGES\n[MINOR][style]\n"
        result = parse_review_output(text)
        self.assertEqual(result.issues, [])

    def test_missing_closing_bracket_skipped(self) -> None:
        """Malformed tag without closing bracket → skipped."""
        text = "REQUEST_CHANGES\n[MINOR[style] oops\n"
        result = parse_review_output(text)
        self.assertEqual(result.issues, [])


# ===========================================================================
# TestHasIssuesFlag
# ===========================================================================


class TestHasIssuesFlag(unittest.TestCase):
    """has_issues is always consistent with bool(issues)."""

    def test_approve_no_issues(self) -> None:
        result = parse_review_output("APPROVE\n")
        self.assertFalse(result.has_issues)
        self.assertFalse(bool(result.issues))

    def test_request_changes_with_issues(self) -> None:
        text = "REQUEST_CHANGES\n[BLOCKER][security] Bad thing\n"
        result = parse_review_output(text)
        self.assertTrue(result.has_issues)
        self.assertTrue(bool(result.issues))

    def test_request_changes_with_no_parseable_issues(self) -> None:
        """REQUEST_CHANGES with all-malformed issue lines → has_issues=False."""
        text = "REQUEST_CHANGES\nThis is just prose, no tags.\n"
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertFalse(result.has_issues)

    def test_has_issues_consistent_after_parse(self) -> None:
        """has_issues is always equal to bool(issues), for any input."""
        inputs = [
            "APPROVE\n",
            "REQUEST_CHANGES\n[MINOR][style] nit\n",
            "",
            "garbage\n",
        ]
        for text in inputs:
            result = parse_review_output(text)
            self.assertEqual(
                result.has_issues,
                bool(result.issues),
                f"has_issues inconsistent for input {text!r}",
            )


# ===========================================================================
# TestGracefulDegradation
# ===========================================================================


class TestGracefulDegradation(unittest.TestCase):
    """Parser never raises; handles pathological inputs gracefully."""

    def _assert_no_raise(self, text: object) -> ReviewResult:
        try:
            return parse_review_output(text)  # type: ignore[arg-type]
        except Exception as exc:
            self.fail(f"parse_review_output raised unexpectedly: {exc!r}")

    def test_empty_string(self) -> None:
        result = self._assert_no_raise("")
        self.assertIsNone(result.verdict)
        self.assertEqual(result.issues, [])

    def test_none_input(self) -> None:
        result = self._assert_no_raise(None)
        self.assertIsInstance(result, ReviewResult)

    def test_integer_input(self) -> None:
        result = self._assert_no_raise(0)
        self.assertIsInstance(result, ReviewResult)

    def test_list_input(self) -> None:
        result = self._assert_no_raise([])
        self.assertIsInstance(result, ReviewResult)

    def test_bytes_input(self) -> None:
        result = self._assert_no_raise(b"APPROVE\n")
        self.assertIsInstance(result, ReviewResult)

    def test_only_blank_lines(self) -> None:
        result = self._assert_no_raise("\n\n\n")
        self.assertIsNone(result.verdict)
        self.assertEqual(result.issues, [])

    def test_very_long_line(self) -> None:
        long_line = "[MINOR][style] " + "x" * 10_000
        text = f"REQUEST_CHANGES\n{long_line}\n"
        result = self._assert_no_raise(text)
        self.assertEqual(len(result.issues), 1)

    def test_unicode_in_description(self) -> None:
        text = "REQUEST_CHANGES\n[MINOR][style] Ünïcödé chàracters — ñoño\n"
        result = self._assert_no_raise(text)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Ünïcödé", result.issues[0].description)


# ===========================================================================
# TestSeverityOrdering
# ===========================================================================


class TestSeverityOrdering(unittest.TestCase):
    """Severity enum values match exactly the expected name strings."""

    def test_blocker_value(self) -> None:
        self.assertEqual(Severity.BLOCKER.value, "BLOCKER")

    def test_major_value(self) -> None:
        self.assertEqual(Severity.MAJOR.value, "MAJOR")

    def test_minor_value(self) -> None:
        self.assertEqual(Severity.MINOR.value, "MINOR")

    def test_nitpick_value(self) -> None:
        self.assertEqual(Severity.NITPICK.value, "NITPICK")

    def test_severity_lookup_by_name(self) -> None:
        """Severity can be looked up by name (used in parser's upper() path)."""
        self.assertIs(Severity("BLOCKER"), Severity.BLOCKER)
        self.assertIs(Severity("MAJOR"), Severity.MAJOR)
        self.assertIs(Severity("MINOR"), Severity.MINOR)
        self.assertIs(Severity("NITPICK"), Severity.NITPICK)

    def test_invalid_severity_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            Severity("CRITICAL")


# ===========================================================================
# TestEdgeCases
# ===========================================================================


class TestEdgeCases(unittest.TestCase):
    """Blank lines, mixed content, and LLM prose commentary."""

    def test_blank_lines_between_issues(self) -> None:
        text = (
            "REQUEST_CHANGES\n"
            "\n"
            "[BLOCKER][security] Issue one\n"
            "\n"
            "[MAJOR][correctness] Issue two\n"
        )
        result = parse_review_output(text)
        self.assertEqual(len(result.issues), 2)

    def test_prose_lines_between_issues_skipped(self) -> None:
        text = (
            "REQUEST_CHANGES\n"
            "Here is a summary of my findings:\n"
            "[BLOCKER][security] The real issue\n"
            "Please fix the above before merging.\n"
        )
        result = parse_review_output(text)
        # Only the properly tagged line is parsed
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.BLOCKER)

    def test_approve_with_prose_below(self) -> None:
        text = (
            "APPROVE\n"
            "Great work! The implementation looks clean and well-tested.\n"
        )
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "APPROVE")
        self.assertFalse(result.has_issues)

    def test_duplicate_tag_lines_both_included(self) -> None:
        """Two identical tag lines → two ReviewIssue objects."""
        text = (
            "REQUEST_CHANGES\n"
            "[NITPICK][style] Trailing space\n"
            "[NITPICK][style] Trailing space\n"
        )
        result = parse_review_output(text)
        self.assertEqual(len(result.issues), 2)

    def test_request_changes_with_no_issues(self) -> None:
        """REQUEST_CHANGES but no issue lines → issues=[], has_issues=False."""
        result = parse_review_output("REQUEST_CHANGES\n")
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertEqual(result.issues, [])
        self.assertFalse(result.has_issues)

    def test_windows_line_endings(self) -> None:
        """CRLF line endings do not break parsing."""
        text = "REQUEST_CHANGES\r\n[MINOR][style] CRLF line\r\n"
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "REQUEST_CHANGES")
        self.assertEqual(len(result.issues), 1)

    def test_no_newline_at_end(self) -> None:
        text = "APPROVE"
        result = parse_review_output(text)
        self.assertEqual(result.verdict, "APPROVE")

    def test_single_tag_line_only_no_verdict(self) -> None:
        """If first non-blank line is a tag (not a verdict), verdict=None.

        The implementation scans all lines for issue tags independently of the
        verdict extraction, so the tag line is still parsed as an issue even
        though it appears where the verdict should be.
        """
        text = "[BLOCKER][security] No verdict before this\n"
        result = parse_review_output(text)
        # The tag line is the first non-blank → not a valid verdict
        self.assertIsNone(result.verdict)
        # The tag line is also parsed as an issue (all lines are scanned)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.BLOCKER)


# ===========================================================================
# TestRawPreservation
# ===========================================================================


class TestRawPreservation(unittest.TestCase):
    """raw_text and ReviewIssue.raw are preserved verbatim."""

    def test_raw_text_identical_to_input(self) -> None:
        text = "REQUEST_CHANGES\n[MINOR][style] nit\n"
        result = parse_review_output(text)
        self.assertIs(result.raw_text, text)  # exact same object

    def test_raw_text_empty_string_preserved(self) -> None:
        result = parse_review_output("")
        self.assertEqual(result.raw_text, "")

    def test_issue_raw_field_is_unstripped_line(self) -> None:
        """ReviewIssue.raw must be the exact line without modification."""
        line = "  [MAJOR][correctness] parse_output() returns None"
        text = f"REQUEST_CHANGES\n{line}\n"
        result = parse_review_output(text)
        self.assertEqual(len(result.issues), 1)
        # raw should be the original line (without trailing newline from splitlines)
        self.assertEqual(result.issues[0].raw, line)

    def test_raw_text_unicode_preserved(self) -> None:
        text = "APPROVE\n# 日本語コメント\n"
        result = parse_review_output(text)
        self.assertEqual(result.raw_text, text)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
