"""Acceptance tests for Issue #701 — Generic Adversary Parser.

These tests are written from behavioral contracts ONLY (behavioral.md).
They do NOT assume implementation details. They are intended to FAIL
until the implementation ships, and then PASS without modification.

All tests are organized by section from behavioral.md:
  - Section 1: Generic Parser — Verdict Extraction
  - Section 2: Generic Parser — Category Filtering
  - Section 3: Generic Parser — Input Coercion
  - Section 4: Generic Parser — Data Structures
  - Section 5: Generic Parser — Config-Driven Behavior
  - Section 6: verdict_parser Enhancement — scan_order
  - Section 7: PhaseDefinition Parsing
  - Section 8: Validation
"""

import sys
import io
import logging
import importlib
import textwrap
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, '/home/toscan/orchestration-engine/src')

# ---------------------------------------------------------------------------
# Discovery-based import for the generic parser (Sections 1-5)
# We do NOT hardcode internal class names — we discover them by the public API
# described in the spec (parse_adversary_output + AdversaryConfig).
# ---------------------------------------------------------------------------

def _import_adversary_parser():
    """Import the adversary_parser module using discovery. Returns the module."""
    return importlib.import_module("orchestration_engine.adversary_parser")


def _get_parser_api():
    """Return (parse_adversary_output, AdversaryConfig) via discovery."""
    mod = _import_adversary_parser()
    parse_fn = getattr(mod, "parse_adversary_output")
    config_cls = getattr(mod, "AdversaryConfig")
    return parse_fn, config_cls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(valid_categories=None, fallback_category=None,
                 verdict_scan="last", **kwargs):
    """Construct an AdversaryConfig using discovered class."""
    _, config_cls = _get_parser_api()
    if valid_categories is None:
        valid_categories = ["coverage", "trivial", "leakage", "specificity"]
    return config_cls(
        valid_categories=valid_categories,
        fallback_category=fallback_category,
        verdict_scan=verdict_scan,
        **kwargs,
    )


# ===========================================================================
# Section 1: Generic Parser — Verdict Extraction
# ===========================================================================

class TestVerdictExtraction:
    """Behavioral contracts from Section 1 of behavioral.md."""

    # Contract: "When parse_adversary_output(text, config) receives text containing
    # `VERDICT: APPROVE` and config has `verdict_scan: "last"`, the system returns
    # a verdict with verdict="APPROVE" and an empty findings list (if no finding
    # lines present)"
    def test_approve_verdict_no_findings(self):
        """Section 1: VERDICT: APPROVE with verdict_scan=last returns APPROVE, empty findings."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(verdict_scan="last")
        text = "The implementation looks solid.\nVERDICT: APPROVE\nAll contracts satisfied."
        result = parse_fn(text, config)
        assert result.verdict == "APPROVE"
        assert isinstance(result.findings, list)
        assert len(result.findings) == 0

    # Contract: "When parse_adversary_output(text, config) receives text containing
    # `VERDICT: REQUEST_CHANGES` followed by `[coverage] Missing test for contract X`,
    # and "coverage" is in config.valid_categories, the system returns
    # verdict="REQUEST_CHANGES" with one finding having category="coverage" and
    # description containing "Missing test for contract X""
    def test_request_changes_with_valid_finding(self):
        """Section 1: REQUEST_CHANGES with valid [coverage] finding is fully parsed."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage", "trivial"])
        text = (
            "Review complete.\n"
            "VERDICT: REQUEST_CHANGES\n"
            "[coverage] Missing test for contract X\n"
        )
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].category == "coverage"
        assert "Missing test for contract X" in result.findings[0].description

    # Contract: "When text contains multiple tagged finding lines across categories
    # all in config.valid_categories, the system returns all findings with their
    # respective categories preserved in order"
    def test_multiple_findings_all_valid_categories_preserved_in_order(self):
        """Section 1: Multiple findings from valid categories returned in order."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage", "trivial", "leakage"])
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "[coverage] First finding\n"
            "[trivial] Second finding\n"
            "[leakage] Third finding\n"
        )
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 3
        assert result.findings[0].category == "coverage"
        assert result.findings[1].category == "trivial"
        assert result.findings[2].category == "leakage"

    # Contract: "When text has no recognizable verdict (no APPROVE or REQUEST_CHANGES
    # token), the system returns verdict="REQUEST_CHANGES" with one finding using
    # config.fallback_category as its category"
    def test_no_verdict_uses_fallback_category(self):
        """Section 1: No recognizable verdict → REQUEST_CHANGES with fallback_category finding."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(
            valid_categories=["coverage", "trivial"],
            fallback_category="coverage",
        )
        text = "This output does not contain any verdict token at all."
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].category == "coverage"

    # Contract: "When config.fallback_category is None and no verdict is found,
    # the system uses the first entry in config.valid_categories as the fallback
    # category"
    def test_no_verdict_fallback_category_none_uses_first_valid(self):
        """Section 1: fallback_category=None + no verdict → first valid_category used."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(
            valid_categories=["specificity", "coverage", "trivial"],
            fallback_category=None,
        )
        text = "No verdict token here whatsoever."
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].category == "specificity"


# ===========================================================================
# Section 2: Generic Parser — Category Filtering
# ===========================================================================

class TestCategoryFiltering:
    """Behavioral contracts from Section 2 of behavioral.md."""

    # Contract: "When text contains [coverage] description and "coverage" is in
    # config.valid_categories, the system includes it in findings with
    # category="coverage""
    def test_valid_category_included(self):
        """Section 2: [coverage] finding included when coverage in valid_categories."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Missing branch test\n"
        result = parse_fn(text, config)
        assert any(f.category == "coverage" for f in result.findings)

    # Contract: "When text contains [unknown_cat] description and "unknown_cat"
    # is NOT in config.valid_categories, the system silently skips it — no
    # exception, finding does not appear in results"
    def test_invalid_category_silently_skipped(self):
        """Section 2: [unknown_cat] silently skipped when not in valid_categories."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage", "trivial"])
        text = "VERDICT: REQUEST_CHANGES\n[unknown_cat] Some finding\n"
        result = parse_fn(text, config)
        # Must not raise and must not include the invalid-category finding
        assert not any(f.category == "unknown_cat" for f in result.findings)

    def test_invalid_category_no_exception(self):
        """Section 2: Invalid category does not raise any exception."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: APPROVE\n[badcat] Unexpected category\n"
        # Must not raise
        result = parse_fn(text, config)
        assert result is not None  # but we must also check something meaningful
        assert result.verdict == "APPROVE"

    # Contract: "When config has valid_categories: ["vague", "trivial"] and text
    # contains [coverage] desc, the system skips [coverage] since it's not in
    # this config's valid set"
    def test_category_valid_only_for_this_config(self):
        """Section 2: [coverage] skipped when config only has ["vague", "trivial"]."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["vague", "trivial"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Something valid elsewhere\n"
        result = parse_fn(text, config)
        assert not any(f.category == "coverage" for f in result.findings)

    # Contract: "When text contains findings in both valid and invalid categories,
    # the system returns only valid-category findings and silently skips the rest"
    def test_mixed_valid_invalid_categories(self):
        """Section 2: Mixed categories → only valid ones returned, others silently dropped."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage", "trivial"])
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "[coverage] Valid finding\n"
            "[leakage] Not in this config's valid set\n"
            "[trivial] Also valid\n"
            "[badcat] Completely unknown\n"
        )
        result = parse_fn(text, config)
        categories = [f.category for f in result.findings]
        assert "coverage" in categories
        assert "trivial" in categories
        assert "leakage" not in categories
        assert "badcat" not in categories
        assert len([f for f in result.findings if f.category in ("coverage", "trivial")]) == 2


# ===========================================================================
# Section 3: Generic Parser — Input Coercion
# ===========================================================================

class TestInputCoercion:
    """Behavioral contracts from Section 3 of behavioral.md."""

    # Contract: "When parse_adversary_output receives None, the system returns
    # verdict="REQUEST_CHANGES" with an explanatory finding — never raises"
    def test_none_input_does_not_raise(self):
        """Section 3: None input → REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(None, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) >= 1

    def test_none_input_has_explanatory_finding(self):
        """Section 3: None input returns at least one explanatory finding."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(None, config)
        assert len(result.findings) >= 1
        # Finding must have a non-empty description
        assert all(len(f.description) > 0 for f in result.findings)

    # Contract: "When it receives an empty string, the system returns
    # verdict="REQUEST_CHANGES" with an explanatory finding"
    def test_empty_string_input(self):
        """Section 3: Empty string → REQUEST_CHANGES with explanatory finding."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn("", config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) >= 1

    # Contract: "When it receives a non-string (int, dict, list, bool, float),
    # the system coerces via str() and returns verdict="REQUEST_CHANGES" — never
    # raises regardless of input type"
    def test_int_input_coerced_no_raise(self):
        """Section 3: int input coerced via str(), returns REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(42, config)
        assert result.verdict == "REQUEST_CHANGES"

    def test_dict_input_coerced_no_raise(self):
        """Section 3: dict input coerced via str(), returns REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn({"key": "value"}, config)
        assert result.verdict == "REQUEST_CHANGES"

    def test_list_input_coerced_no_raise(self):
        """Section 3: list input coerced via str(), returns REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn([1, 2, 3], config)
        assert result.verdict == "REQUEST_CHANGES"

    def test_bool_input_coerced_no_raise(self):
        """Section 3: bool input coerced via str(), returns REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(True, config)
        assert result.verdict == "REQUEST_CHANGES"

    def test_float_input_coerced_no_raise(self):
        """Section 3: float input coerced via str(), returns REQUEST_CHANGES, never raises."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(3.14, config)
        assert result.verdict == "REQUEST_CHANGES"

    def test_all_non_string_types_do_not_raise(self):
        """Section 3: All non-string types coerced, never raise."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        for value in [0, -1, {}, [], False, 0.0, object()]:
            # Must not raise
            result = parse_fn(value, config)
            assert result is not None
            assert result.verdict == "REQUEST_CHANGES"


# ===========================================================================
# Section 4: Generic Parser — Data Structures
# ===========================================================================

class TestDataStructures:
    """Behavioral contracts from Section 4 of behavioral.md."""

    # Contract: "When a verdict is parsed, the verdict object exposes: verdict
    # (str, "APPROVE" or "REQUEST_CHANGES"), findings (list of finding objects),
    # raw_text (str, original input preserved verbatim — byte-identical to what
    # was passed in, or str(input) if coerced)"
    def test_verdict_object_exposes_verdict_field(self):
        """Section 4: Verdict object has verdict field as str."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn("VERDICT: APPROVE", config)
        assert hasattr(result, "verdict")
        assert isinstance(result.verdict, str)
        assert result.verdict in ("APPROVE", "REQUEST_CHANGES")

    def test_verdict_object_exposes_findings_field(self):
        """Section 4: Verdict object has findings field as list."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn("VERDICT: APPROVE", config)
        assert hasattr(result, "findings")
        assert isinstance(result.findings, list)

    def test_verdict_object_exposes_raw_text_field(self):
        """Section 4: Verdict object has raw_text field as str."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: APPROVE\nAll good."
        result = parse_fn(text, config)
        assert hasattr(result, "raw_text")
        assert isinstance(result.raw_text, str)

    def test_raw_text_preserved_verbatim(self):
        """Section 4: raw_text is byte-identical to input string."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: APPROVE\nAll good.\n  Extra spaces.  "
        result = parse_fn(text, config)
        assert result.raw_text == text

    def test_raw_text_coerced_for_non_string(self):
        """Section 4: raw_text is str(input) when input is coerced."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(99, config)
        assert result.raw_text == str(99)

    def test_raw_text_coerced_none_input(self):
        """Section 4: raw_text is str(None) = 'None' when None was passed."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn(None, config)
        # raw_text must be str(None) = "None" per contract ("str(input) if coerced")
        assert result.raw_text == "None"

    # Contract: "When a finding is parsed, the finding object exposes: category
    # (str, always lowercase), description (str, preserved verbatim from input
    # line after [category] prefix)"
    def test_finding_category_is_lowercase(self):
        """Section 4: Finding category is always lowercase."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Some problem\n"
        result = parse_fn(text, config)
        assert len(result.findings) >= 1
        assert result.findings[0].category == result.findings[0].category.lower()
        assert result.findings[0].category == "coverage"

    def test_finding_description_preserved_verbatim(self):
        """Section 4: Finding description is preserved verbatim after [category] prefix."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        desc = "  Extra spaces and CAPS and special chars !@# "
        text = f"VERDICT: REQUEST_CHANGES\n[coverage] {desc}\n"
        result = parse_fn(text, config)
        assert len(result.findings) >= 1
        assert result.findings[0].description == desc

    def test_finding_exposes_category_and_description(self):
        """Section 4: Finding object exposes category and description fields."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Test description\n"
        result = parse_fn(text, config)
        assert len(result.findings) >= 1
        finding = result.findings[0]
        assert hasattr(finding, "category")
        assert hasattr(finding, "description")

    # Contract: "When an APPROVE verdict has finding lines in the text, the
    # findings list is still populated (findings are parsed independently of verdict)"
    def test_approve_with_finding_lines_still_populated(self):
        """Section 4: APPROVE verdict still populates findings from finding lines."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage", "trivial"])
        text = (
            "VERDICT: APPROVE\n"
            "Minor note:\n"
            "[coverage] Could add one more edge case\n"
            "[trivial] This is trivial but worth noting\n"
        )
        result = parse_fn(text, config)
        assert result.verdict == "APPROVE"
        assert len(result.findings) >= 2
        categories = [f.category for f in result.findings]
        assert "coverage" in categories
        assert "trivial" in categories


# ===========================================================================
# Section 5: Generic Parser — Config-Driven Behavior
# ===========================================================================

class TestConfigDrivenBehavior:
    """Behavioral contracts from Section 5 of behavioral.md."""

    # Contract: "When config.verdict_scan is "first" and text contains
    # VERDICT: APPROVE before VERDICT: REQUEST_CHANGES, the parser returns
    # verdict="APPROVE" (first wins)"
    def test_verdict_scan_first_returns_first_verdict(self):
        """Section 5: verdict_scan="first" + APPROVE before REQUEST_CHANGES → APPROVE."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(verdict_scan="first")
        text = (
            "Initial analysis.\n"
            "VERDICT: APPROVE\n"
            "Wait, more issues.\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        result = parse_fn(text, config)
        assert result.verdict == "APPROVE"

    # Contract: "When config.verdict_scan is "last" and text contains
    # VERDICT: APPROVE before VERDICT: REQUEST_CHANGES, the parser returns
    # verdict="REQUEST_CHANGES" (last wins)"
    def test_verdict_scan_last_returns_last_verdict(self):
        """Section 5: verdict_scan="last" + APPROVE before REQUEST_CHANGES → REQUEST_CHANGES."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(verdict_scan="last")
        text = (
            "Initial analysis.\n"
            "VERDICT: APPROVE\n"
            "Wait, more issues.\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"

    # Contract: "When two configs have different valid_categories, the same input
    # text produces different findings for each config (only categories in that
    # config's set are included)"
    def test_different_configs_produce_different_findings(self):
        """Section 5: Two configs with different valid_categories produce different findings."""
        parse_fn, _ = _get_parser_api()
        config_a = _make_config(valid_categories=["coverage"])
        config_b = _make_config(valid_categories=["trivial"])
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "[coverage] Missing test\n"
            "[trivial] Too simple\n"
        )
        result_a = parse_fn(text, config_a)
        result_b = parse_fn(text, config_b)
        # config_a: only coverage
        categories_a = [f.category for f in result_a.findings]
        assert "coverage" in categories_a
        assert "trivial" not in categories_a
        # config_b: only trivial
        categories_b = [f.category for f in result_b.findings]
        assert "trivial" in categories_b
        assert "coverage" not in categories_b

    def test_single_verdict_line_same_regardless_of_scan_order(self):
        """Section 5: Single VERDICT line → same result for "first" and "last"."""
        parse_fn, _ = _get_parser_api()
        config_first = _make_config(verdict_scan="first")
        config_last = _make_config(verdict_scan="last")
        text = "VERDICT: APPROVE\nAll contracts satisfied."
        result_first = parse_fn(text, config_first)
        result_last = parse_fn(text, config_last)
        assert result_first.verdict == result_last.verdict == "APPROVE"


# ===========================================================================
# Section 6: verdict_parser Enhancement — scan_order
# ===========================================================================

class TestVerdictParserScanOrder:
    """Behavioral contracts from Section 6 of behavioral.md.

    Imports directly from orchestration_engine.verdict_parser as specified.
    """

    def _import_extract_verdict(self):
        from orchestration_engine.verdict_parser import extract_verdict
        return extract_verdict

    # Contract: "When extract_verdict(text, scan_order="last") is called (default),
    # behavior is identical to the current implementation — last structured
    # VERDICT: line wins (backward compatible)"
    def test_scan_order_last_matches_default_behavior(self):
        """Section 6: scan_order="last" is identical to current last-match-wins behavior."""
        extract_verdict = self._import_extract_verdict()
        text = (
            "VERDICT: APPROVE\n"
            "Wait, more issues.\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        # With explicit scan_order="last"
        result_explicit = extract_verdict(text=text, scan_order="last")
        # Without scan_order (should default to "last")
        result_default = extract_verdict(text=text)
        assert result_explicit == "request_changes"
        assert result_default == "request_changes"
        assert result_explicit == result_default

    # Contract: "When extract_verdict(text, scan_order="first") is called,
    # the first structured VERDICT: line wins"
    def test_scan_order_first_returns_first_verdict(self):
        """Section 6: scan_order="first" → first structured VERDICT: line wins."""
        extract_verdict = self._import_extract_verdict()
        text = (
            "VERDICT: APPROVE\n"
            "Wait, more issues.\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        result = extract_verdict(text=text, scan_order="first")
        assert result == "approve"

    def test_scan_order_first_single_verdict(self):
        """Section 6: scan_order="first" with single verdict returns that verdict."""
        extract_verdict = self._import_extract_verdict()
        text = "VERDICT: REQUEST_CHANGES\nSome issues."
        result = extract_verdict(text=text, scan_order="first")
        assert result == "request_changes"

    # Contract: "When extract_verdict is called without scan_order, it defaults to
    # "last" — all existing callers get identical behavior without code changes"
    def test_scan_order_defaults_to_last_backward_compat(self):
        """Section 6: No scan_order arg → defaults to "last", backward compatible."""
        extract_verdict = self._import_extract_verdict()
        # Two structured VERDICT: lines — default (last-match-wins) → APPROVE
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "VERDICT: APPROVE\n"
        )
        # Default (no scan_order) should act as last-match-wins
        result = extract_verdict(text=text)
        assert result == "approve"  # last structural match wins

    def test_scan_order_param_does_not_break_existing_signature(self):
        """Section 6: Adding scan_order does not break existing callers."""
        extract_verdict = self._import_extract_verdict()
        # Existing usage: positional text, no scan_order
        text = "VERDICT: APPROVE\n"
        result = extract_verdict(text=text)
        assert result == "approve"

    # Contract: "When Pass 1 (structured VERDICT: lines) finds no match regardless
    # of scan_order, Pass 2 (fallback regex with priority ordering) is used —
    # Pass 2 behavior is unchanged by scan_order"
    def test_pass2_used_when_pass1_finds_nothing_scan_order_last(self):
        """Section 6: Pass 2 used when no structured VERDICT: lines; scan_order=last."""
        extract_verdict = self._import_extract_verdict()
        # Text with no structured VERDICT: line — should fall through to Pass 2
        # Pass 2 requires keyword at line-boundary position (not mid-sentence)
        text = "APPROVE the review."
        result_last = extract_verdict(text=text, scan_order="last")
        result_first = extract_verdict(text=text, scan_order="first")
        # Pass 2 behavior is unchanged — both should give same result AND the expected value
        assert result_last == "approve"
        assert result_first == "approve"

    def test_pass2_priority_unchanged_by_scan_order(self):
        """Section 6: Pass 2 priority ordering (REQUEST_CHANGES > ABORT > APPROVE) unchanged."""
        extract_verdict = self._import_extract_verdict()
        # Both REQUEST_CHANGES and APPROVE present, but no structured VERDICT: line
        text = "APPROVE some stuff\nREQUEST_CHANGES more stuff"
        result_last = extract_verdict(text=text, scan_order="last")
        result_first = extract_verdict(text=text, scan_order="first")
        # Pass 2 should pick REQUEST_CHANGES (higher priority) regardless of scan_order
        assert result_last == "request_changes"
        assert result_first == "request_changes"

    def test_scan_order_first_three_verdicts(self):
        """Section 6: scan_order="first" with three VERDICT: lines returns first."""
        extract_verdict = self._import_extract_verdict()
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "VERDICT: APPROVE\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        result = extract_verdict(text=text, scan_order="first")
        assert result == "request_changes"

    def test_scan_order_last_three_verdicts(self):
        """Section 6: scan_order="last" with three VERDICT: lines returns last."""
        extract_verdict = self._import_extract_verdict()
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "VERDICT: APPROVE\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        result = extract_verdict(text=text, scan_order="last")
        assert result == "request_changes"

    def test_scan_order_last_three_verdicts_last_is_approve(self):
        """Section 6: scan_order="last" returns final APPROVE when last."""
        extract_verdict = self._import_extract_verdict()
        text = (
            "VERDICT: REQUEST_CHANGES\n"
            "VERDICT: REQUEST_CHANGES\n"
            "VERDICT: APPROVE\n"
        )
        result = extract_verdict(text=text, scan_order="last")
        assert result == "approve"


# ===========================================================================
# Section 7: PhaseDefinition Parsing
# ===========================================================================

def _load_template_from_yaml(yaml_str: str):
    """Load a PipelineTemplate from a YAML string via TemplateEngine."""
    from orchestration_engine.templates import TemplateEngine
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(yaml_str)
        f.flush()
        engine = TemplateEngine(templates_dir=Path(f.name).parent)
        return engine.load_template(Path(f.name))


def _make_minimal_phase_yaml(phase_extra: str = "") -> str:
    """Return a minimal valid pipeline YAML with one phase, with optional extra fields."""
    return textwrap.dedent(f"""\
        id: test-pipeline
        name: Test Pipeline
        version: "1.0.0"
        phases:
          - id: phase_one
            name: Phase One
            {phase_extra}
    """)


class TestPhaseDefinitionParsing:
    """Behavioral contracts from Section 7 of behavioral.md."""

    # Contract: "When a YAML phase contains adversary_config: with valid_categories,
    # fallback_category, and verdict_scan, the system populates phase.adversary_config
    # as an AdversaryConfig object with all fields set"
    def test_phase_with_full_adversary_config(self):
        """Section 7: Full adversary_config YAML → AdversaryConfig with all fields."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                    - trivial
                    - leakage
                  fallback_category: coverage
                  verdict_scan: last
        """)
        tpl = _load_template_from_yaml(yaml_str)
        phase = tpl.phases[0]
        assert phase.adversary_config is not None
        config = phase.adversary_config
        assert config.valid_categories == ["coverage", "trivial", "leakage"]
        assert config.fallback_category == "coverage"
        assert config.verdict_scan == "last"

    # Contract: "When a YAML phase has no adversary_config key,
    # phase.adversary_config is None"
    def test_phase_without_adversary_config_is_none(self):
        """Section 7: No adversary_config in YAML → phase.adversary_config is None."""
        yaml_str = _make_minimal_phase_yaml()
        tpl = _load_template_from_yaml(yaml_str)
        phase = tpl.phases[0]
        assert phase.adversary_config is None

    # Contract: "When adversary_config contains only valid_categories (other fields
    # omitted), the system uses defaults: fallback_category=None (→ first category),
    # verdict_scan="last", reward_enabled=False"
    def test_phase_adversary_config_only_valid_categories_uses_defaults(self):
        """Section 7: Only valid_categories in adversary_config → defaults for others."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                    - trivial
        """)
        tpl = _load_template_from_yaml(yaml_str)
        phase = tpl.phases[0]
        assert phase.adversary_config is not None
        config = phase.adversary_config
        assert config.valid_categories == ["coverage", "trivial"]
        assert config.fallback_category is None
        assert config.verdict_scan == "last"
        assert config.reward_enabled == False

    def test_adversary_config_verdict_scan_first_parsed(self):
        """Section 7: verdict_scan: first is correctly parsed into AdversaryConfig."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                  verdict_scan: first
        """)
        tpl = _load_template_from_yaml(yaml_str)
        phase = tpl.phases[0]
        assert phase.adversary_config is not None
        assert phase.adversary_config.verdict_scan == "first"


# ===========================================================================
# Section 8: Validation
# ===========================================================================

class TestValidation:
    """Behavioral contracts from Section 8 of behavioral.md.

    Tests use orch validate CLI or the template loading mechanism to verify
    that invalid configs are rejected with clear error messages.
    """

    def _load_and_catch(self, yaml_str: str):
        """Attempt to load template; return (template_or_None, error_or_None)."""
        try:
            tpl = _load_template_from_yaml(yaml_str)
            return tpl, None
        except Exception as e:
            return None, e

    # Contract: "When adversary_config.valid_categories is an empty list,
    # orch validate rejects the template with a clear error message"
    def test_empty_valid_categories_rejected(self):
        """Section 8: empty valid_categories → template loading raises with clear error."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories: []
                  verdict_scan: last
        """)
        tpl, err = self._load_and_catch(yaml_str)
        # Must be rejected: either raises exception OR sets validation error
        # The contract says "clear error message" — we check either approach
        if err is None:
            # If it didn't raise, the template should have a validation error field
            # or the adversary_config should be invalid — at minimum the contract
            # requires this to fail validation
            pytest.fail(
                "Template with empty valid_categories should be rejected but was loaded."
            )
        # Error message should be clear/non-empty
        assert str(err).strip() != ""

    # Contract: "When adversary_config.fallback_category is set to a value NOT in
    # valid_categories, orch validate rejects the template with a clear error message"
    def test_fallback_not_in_valid_categories_rejected(self):
        """Section 8: fallback_category not in valid_categories → rejected with clear error."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                    - trivial
                  fallback_category: leakage
                  verdict_scan: last
        """)
        tpl, err = self._load_and_catch(yaml_str)
        if err is None:
            pytest.fail(
                "Template with fallback_category not in valid_categories should be rejected."
            )
        assert str(err).strip() != ""

    # Contract: "When adversary_config.verdict_scan is set to a value other than
    # "first" or "last", orch validate rejects the template with a clear error message"
    def test_invalid_verdict_scan_value_rejected(self):
        """Section 8: verdict_scan not "first" or "last" → rejected with clear error."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                  verdict_scan: middle
        """)
        tpl, err = self._load_and_catch(yaml_str)
        if err is None:
            pytest.fail(
                "Template with invalid verdict_scan='middle' should be rejected."
            )
        assert str(err).strip() != ""

    def test_verdict_scan_first_is_valid(self):
        """Section 8: verdict_scan="first" is a valid value — not rejected."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                  verdict_scan: first
        """)
        tpl, err = self._load_and_catch(yaml_str)
        assert err is None, f"verdict_scan='first' should be valid but got error: {err}"
        assert tpl is not None

    def test_verdict_scan_last_is_valid(self):
        """Section 8: verdict_scan="last" is a valid value — not rejected."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                  verdict_scan: last
        """)
        tpl, err = self._load_and_catch(yaml_str)
        assert err is None, f"verdict_scan='last' should be valid but got error: {err}"
        assert tpl is not None

    # Contract: "When adversary_config.valid_categories contains duplicate entries,
    # the system deduplicates silently preserving order (first occurrence kept)"
    def test_duplicate_valid_categories_deduplicated_preserving_order(self):
        """Section 8: Duplicate valid_categories entries → deduplicated, order preserved."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                    - trivial
                    - coverage
                    - leakage
                    - trivial
                  verdict_scan: last
        """)
        tpl = _load_template_from_yaml(yaml_str)
        phase = tpl.phases[0]
        assert phase.adversary_config is not None
        cats = phase.adversary_config.valid_categories
        # Deduplication: each category appears exactly once
        assert len(cats) == len(set(cats))
        # Order preserved (first occurrence): coverage, trivial, leakage
        assert cats.index("coverage") < cats.index("trivial") < cats.index("leakage")
        assert "coverage" in cats
        assert "trivial" in cats
        assert "leakage" in cats

    # Contract: "When adversary_config contains an unknown field (e.g. unknown_key: true),
    # the system logs a warning (consistent with existing unknown-field handling in templates.py)"
    def test_unknown_field_in_adversary_config_logs_warning(self):
        """Section 8: Unknown field in adversary_config → warning logged, not exception."""
        yaml_str = textwrap.dedent("""\
            id: test-pipeline
            name: Test Pipeline
            version: "1.0.0"
            phases:
              - id: phase_one
                name: Phase One
                adversary_config:
                  valid_categories:
                    - coverage
                  verdict_scan: last
                  unknown_key: true
        """)
        # Capture log output to verify warning is emitted
        with self._capture_warnings() as captured_warnings:
            tpl = _load_template_from_yaml(yaml_str)

        # Must not raise: template loads successfully
        assert tpl is not None
        # At least one warning must have been logged
        assert len(captured_warnings) >= 1
        # Warning should reference the unknown field name
        assert any("unknown_key" in record.getMessage() for record in captured_warnings)
        # We check that at least the template loaded (unknown field doesn't block it)
        phase = tpl.phases[0]
        assert phase.adversary_config is not None
        # The unknown_key should NOT appear on the config object
        assert not hasattr(phase.adversary_config, "unknown_key")

    @staticmethod
    def _capture_warnings():
        """Context manager to capture log warnings."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            records = []
            handler = logging.handlers_ListHandler(records)
            root = logging.getLogger("orchestration_engine.templates")
            root.addHandler(handler)
            try:
                yield records
            finally:
                root.removeHandler(handler)

        # Use a simple approach: just a list collector via caplog-like approach
        @contextlib.contextmanager
        def _simple_ctx():
            records = []

            class CollectHandler(logging.Handler):
                def emit(self, record):
                    records.append(record)

            handler = CollectHandler()
            logger = logging.getLogger("orchestration_engine.templates")
            logger.addHandler(handler)
            try:
                yield records
            finally:
                logger.removeHandler(handler)

        return _simple_ctx()


# ===========================================================================
# Additional edge cases and boundary conditions
# ===========================================================================

class TestEdgeCasesAndBoundaries:
    """Additional edge cases derived from the behavioral contracts."""

    # Edge case: single valid_category config
    def test_single_valid_category_config(self):
        """Edge: single valid_categories entry works correctly."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Only this category\n"
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].category == "coverage"

    # Edge case: finding lines but only invalid categories → no findings returned
    def test_all_finding_categories_invalid_returns_empty_findings(self):
        """Edge: all findings in invalid categories → empty findings list."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[leakage] Not valid\n[trivial] Also not valid\n"
        result = parse_fn(text, config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 0

    # Edge case: approve with no findings, raw text preserved
    def test_approve_no_findings_raw_text_preserved(self):
        """Edge: APPROVE verdict, no findings → raw_text is exact input."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "Everything checks out.\nVERDICT: APPROVE\n"
        result = parse_fn(text, config)
        assert result.verdict == "APPROVE"
        assert result.raw_text == text

    # Boundary: finding description after [category] prefix is everything after the space
    def test_finding_description_strips_only_category_prefix(self):
        """Boundary: description is everything after '[category] ' — category stripped."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] This is the description part\n"
        result = parse_fn(text, config)
        assert len(result.findings) == 1
        assert result.findings[0].description == "This is the description part"
        assert "coverage" not in result.findings[0].description or \
               result.findings[0].description == "This is the description part"

    # Edge case: verdict_scan config field defaults to "last" when not specified
    def test_adversary_config_default_verdict_scan(self):
        """Edge: AdversaryConfig defaults verdict_scan to "last" when not specified."""
        _, config_cls = _get_parser_api()
        config = config_cls(
            valid_categories=["coverage"],
        )
        assert config.verdict_scan == "last"

    # Edge case: fallback_category defaults to None
    def test_adversary_config_default_fallback_category(self):
        """Edge: AdversaryConfig defaults fallback_category to None when not specified."""
        _, config_cls = _get_parser_api()
        config = config_cls(valid_categories=["coverage"])
        assert config.fallback_category is None

    # Edge case: reward_enabled defaults to False
    def test_adversary_config_default_reward_enabled(self):
        """Edge: AdversaryConfig defaults reward_enabled to False when not specified."""
        _, config_cls = _get_parser_api()
        config = config_cls(valid_categories=["coverage"])
        assert config.reward_enabled == False

    # Boundary: whitespace-only string
    def test_whitespace_only_string(self):
        """Boundary: Whitespace-only string → REQUEST_CHANGES with explanatory finding."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        result = parse_fn("   \n\t  ", config)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) >= 1

    # Boundary: verdict_scan="first" with single verdict is consistent
    def test_scan_first_single_verdict(self):
        """Boundary: verdict_scan="first" with single verdict returns that verdict."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(verdict_scan="first")
        text = "VERDICT: APPROVE\nAll looks good."
        result = parse_fn(text, config)
        assert result.verdict == "APPROVE"

    # Edge: category in finding line must be exactly as in valid_categories (lowercase)
    def test_category_matching_is_case_sensitive_or_normalized(self):
        """Edge: Category extracted from text is lowercase (per contract: always lowercase)."""
        parse_fn, _ = _get_parser_api()
        config = _make_config(valid_categories=["coverage"])
        text = "VERDICT: REQUEST_CHANGES\n[coverage] Something wrong\n"
        result = parse_fn(text, config)
        assert len(result.findings) == 1
        # Category must be lowercase (contract: "always lowercase")
        assert result.findings[0].category == "coverage"
        assert result.findings[0].category == result.findings[0].category.lower()
