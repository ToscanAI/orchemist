"""Unit tests for parse_adversary_output from orchestration_engine.adversary_parser.

Covers all 9 behavioral contracts defined in the spec for Issue #720.
"""

import pytest

from orchestration_engine.adversary_parser import (
    AdversaryConfig,
    AdversaryFinding,
    AdversaryVerdict,
    parse_adversary_output,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_config() -> AdversaryConfig:
    """A minimal valid AdversaryConfig with two categories."""
    return AdversaryConfig(
        valid_categories=["coverage", "security"],
        fallback_category="coverage",
        verdict_scan="first",
    )


@pytest.fixture
def last_scan_config() -> AdversaryConfig:
    """Config with verdict_scan='last'."""
    return AdversaryConfig(
        valid_categories=["coverage", "security"],
        fallback_category="coverage",
        verdict_scan="last",
    )


# ---------------------------------------------------------------------------
# Contract 1: APPROVE verdict detected
# ---------------------------------------------------------------------------


def test_approve_verdict_detected(basic_config: AdversaryConfig) -> None:
    """Contract 1: Given text containing 'VERDICT: APPROVE',
    parse_adversary_output returns AdversaryVerdict with verdict == 'APPROVE'.
    """
    text = "Some preamble.\nVERDICT: APPROVE\nSome trailing text."
    result = parse_adversary_output(text, basic_config)
    assert isinstance(result, AdversaryVerdict)
    assert result.verdict == "APPROVE"


# ---------------------------------------------------------------------------
# Contract 2: REQUEST_CHANGES verdict detected
# ---------------------------------------------------------------------------


def test_request_changes_verdict_detected(basic_config: AdversaryConfig) -> None:
    """Contract 2: Given text containing 'VERDICT: REQUEST_CHANGES',
    parse_adversary_output returns AdversaryVerdict with verdict == 'REQUEST_CHANGES'.
    """
    text = "Analysis complete.\nVERDICT: REQUEST_CHANGES\n[coverage] Missing tests."
    result = parse_adversary_output(text, basic_config)
    assert isinstance(result, AdversaryVerdict)
    assert result.verdict == "REQUEST_CHANGES"


# ---------------------------------------------------------------------------
# Contract 3: verdict_scan='first' picks first verdict
# ---------------------------------------------------------------------------


def test_verdict_scan_first_picks_first_verdict(basic_config: AdversaryConfig) -> None:
    """Contract 3: Given config.verdict_scan='first' and multiple verdict lines,
    parse_adversary_output returns the verdict from the FIRST matching line.
    """
    assert basic_config.verdict_scan == "first"
    text = (
        "Line one.\n"
        "VERDICT: APPROVE\n"
        "Middle text.\n"
        "VERDICT: REQUEST_CHANGES\n"
        "End."
    )
    result = parse_adversary_output(text, basic_config)
    assert result.verdict == "APPROVE"


# ---------------------------------------------------------------------------
# Contract 4: verdict_scan='last' picks last verdict
# ---------------------------------------------------------------------------


def test_verdict_scan_last_picks_last_verdict(last_scan_config: AdversaryConfig) -> None:
    """Contract 4: Given config.verdict_scan='last' and multiple verdict lines,
    parse_adversary_output returns the verdict from the LAST matching line.
    """
    assert last_scan_config.verdict_scan == "last"
    text = (
        "Line one.\n"
        "VERDICT: APPROVE\n"
        "Middle text.\n"
        "VERDICT: REQUEST_CHANGES\n"
        "End."
    )
    result = parse_adversary_output(text, last_scan_config)
    assert result.verdict == "REQUEST_CHANGES"


# ---------------------------------------------------------------------------
# Contract 5: No verdict line → REQUEST_CHANGES with at least one finding
# ---------------------------------------------------------------------------


def test_no_verdict_line_defaults_to_request_changes_with_finding(
    basic_config: AdversaryConfig,
) -> None:
    """Contract 5: Given text with NO verdict line, parse_adversary_output returns
    AdversaryVerdict with verdict == 'REQUEST_CHANGES' and at least one finding
    whose description is a non-empty string. Function must NOT raise.
    """
    text = "There is no verdict marker in this text at all."
    result = parse_adversary_output(text, basic_config)
    assert result.verdict == "REQUEST_CHANGES"
    assert len(result.findings) >= 1
    assert isinstance(result.findings[0].description, str)
    assert len(result.findings[0].description) > 0


# ---------------------------------------------------------------------------
# Contract 6: Invalid category silently excluded; valid category retained (APPROVE)
# ---------------------------------------------------------------------------


def test_category_filtering_valid_included_invalid_excluded_on_approve(
    basic_config: AdversaryConfig,
) -> None:
    """Contract 6: Given APPROVE with valid [coverage] finding AND invalid [typo] finding,
    the result includes the coverage finding AND excludes the typo finding.
    """
    text = (
        "VERDICT: APPROVE\n"
        "[coverage] Test coverage is sufficient.\n"
        "[typo] Spelling mistake found.\n"
    )
    result = parse_adversary_output(text, basic_config)
    categories = [f.category for f in result.findings]
    assert "coverage" in categories, f"Expected 'coverage' in findings but got: {categories}"
    assert "typo" not in categories, f"'typo' should be excluded but appeared in: {categories}"
    descriptions = [f.description for f in result.findings]
    assert not any("Spelling mistake found" in d for d in descriptions), (
        "Typo finding text appeared in findings — it was not silently skipped"
    )


# ---------------------------------------------------------------------------
# Contract 7: Valid category finding description matches input text
# ---------------------------------------------------------------------------


def test_valid_category_finding_description_matches_input(
    basic_config: AdversaryConfig,
) -> None:
    """Contract 7: Given a finding line '[coverage] some text' where 'coverage' IS
    in config.valid_categories, the finding appears in result.findings with
    category == 'coverage' AND its description reflects the actual input text.
    """
    text = "VERDICT: REQUEST_CHANGES\n[coverage] Missing unit tests for parser."
    result = parse_adversary_output(text, basic_config)
    coverage_findings = [f for f in result.findings if f.category == "coverage"]
    assert len(coverage_findings) >= 1, "Expected at least one coverage finding"
    assert any("Missing unit tests" in f.description for f in coverage_findings), (
        f"No coverage finding contained 'Missing unit tests'. "
        f"Descriptions: {[f.description for f in coverage_findings]}"
    )


# ---------------------------------------------------------------------------
# Contract 8: Mixed findings — valid included, invalid excluded, description matches
# ---------------------------------------------------------------------------


def test_mixed_findings_valid_included_invalid_excluded_description_check(
    basic_config: AdversaryConfig,
) -> None:
    """Contract 8: Given text with both a valid '[coverage]' finding AND an invalid
    '[typo]' finding, the result includes the coverage finding (with correct
    description) and excludes the typo finding.
    """
    text = (
        "VERDICT: REQUEST_CHANGES\n"
        "[coverage] Not enough tests.\n"
        "[typo] Spelling mistake found.\n"
    )
    result = parse_adversary_output(text, basic_config)
    categories = [f.category for f in result.findings]
    assert "coverage" in categories, f"Expected 'coverage' in findings but got: {categories}"
    assert "typo" not in categories, f"'typo' should be excluded but appeared in: {categories}"
    coverage_findings = [f for f in result.findings if f.category == "coverage"]
    assert any("Not enough tests" in f.description for f in coverage_findings), (
        f"Coverage finding description did not reflect input text. "
        f"Descriptions: {[f.description for f in coverage_findings]}"
    )


# ---------------------------------------------------------------------------
# Contract 9: Empty string input → REQUEST_CHANGES with at least one finding
# ---------------------------------------------------------------------------


def test_empty_string_input_returns_request_changes_with_finding(
    basic_config: AdversaryConfig,
) -> None:
    """Contract 9: Given empty string input, parse_adversary_output returns
    AdversaryVerdict with verdict == 'REQUEST_CHANGES' and at least one finding.
    Function must NOT raise.
    """
    result = parse_adversary_output("", basic_config)
    assert isinstance(result, AdversaryVerdict)
    assert result.verdict == "REQUEST_CHANGES", (
        f"Expected 'REQUEST_CHANGES' for empty input, got: {result.verdict!r}"
    )
    assert len(result.findings) >= 1, (
        "Expected at least one finding for empty input (no-verdict fallback)"
    )


# ---------------------------------------------------------------------------
# Contract (bonus): None input → REQUEST_CHANGES with at least one finding
# ---------------------------------------------------------------------------


def test_none_input_returns_request_changes_with_finding(
    basic_config: AdversaryConfig,
) -> None:
    """Bonus contract: Given None input, parse_adversary_output returns
    AdversaryVerdict with verdict == 'REQUEST_CHANGES' and at least one finding.
    Function must NOT raise.
    """
    result = parse_adversary_output(None, basic_config)
    assert isinstance(result, AdversaryVerdict)
    assert result.verdict == "REQUEST_CHANGES", (
        f"Expected 'REQUEST_CHANGES' for None input, got: {result.verdict!r}"
    )
    assert len(result.findings) >= 1, (
        "Expected at least one finding for None input (no-verdict fallback)"
    )
