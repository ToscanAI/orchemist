"""Unit tests for parse_adversary_output from orchestration_engine.adversary_parser.

Covers all 9 behavioral contracts defined in the spec for Issue #720, plus the
generic reward functions (compute_reward / persist_reward) added by Issue #702.
"""

import json

import pytest

from orchestration_engine.adversary_parser import (
    AdversaryConfig,
    AdversaryFinding,
    AdversaryVerdict,
    compute_reward,
    parse_adversary_output,
    persist_reward,
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
    text = "Line one.\n" "VERDICT: APPROVE\n" "Middle text.\n" "VERDICT: REQUEST_CHANGES\n" "End."
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
    text = "Line one.\n" "VERDICT: APPROVE\n" "Middle text.\n" "VERDICT: REQUEST_CHANGES\n" "End."
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
    assert not any(
        "Spelling mistake found" in d for d in descriptions
    ), "Typo finding text appeared in findings — it was not silently skipped"


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
    assert (
        result.verdict == "REQUEST_CHANGES"
    ), f"Expected 'REQUEST_CHANGES' for empty input, got: {result.verdict!r}"
    assert (
        len(result.findings) >= 1
    ), "Expected at least one finding for empty input (no-verdict fallback)"


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
    assert (
        result.verdict == "REQUEST_CHANGES"
    ), f"Expected 'REQUEST_CHANGES' for None input, got: {result.verdict!r}"
    assert (
        len(result.findings) >= 1
    ), "Expected at least one finding for None input (no-verdict fallback)"


# ---------------------------------------------------------------------------
# Issue #702 — generic compute_reward
# ---------------------------------------------------------------------------


def test_compute_reward_approve_returns_zero_float(basic_config: AdversaryConfig) -> None:
    """APPROVE → 0.0 as a float."""
    verdict = AdversaryVerdict(verdict="APPROVE", findings=[])
    result = compute_reward(verdict, basic_config)
    assert result == 0.0
    assert isinstance(result, float)


def test_compute_reward_request_changes_returns_findings_count_float(
    basic_config: AdversaryConfig,
) -> None:
    """REQUEST_CHANGES with N findings → float(N)."""
    verdict = AdversaryVerdict(
        verdict="REQUEST_CHANGES",
        findings=[
            AdversaryFinding("coverage", "a"),
            AdversaryFinding("coverage", "b"),
            AdversaryFinding("security", "c"),
        ],
    )
    result = compute_reward(verdict, basic_config)
    assert result == 3.0
    assert isinstance(result, float)


def test_compute_reward_pinned_values(basic_config: AdversaryConfig) -> None:
    """Generic compute_reward is pinned to literal expected floats (#703).

    Once the legacy spec_adversary reference implementation is deleted, the
    surviving contract is "generic compute_reward = float(len(findings)) on
    REQUEST_CHANGES, 0.0 on APPROVE". Pin it to constants (the previous parity
    test compared against the now-deleted legacy module).
    """
    cases = [
        ("APPROVE", [], 0.0),
        ("REQUEST_CHANGES", [], 0.0),
        ("REQUEST_CHANGES", ["a"], 1.0),
        ("REQUEST_CHANGES", ["a", "b", "c"], 3.0),
    ]
    for verdict_str, descs, expected in cases:
        verdict = AdversaryVerdict(
            verdict=verdict_str,
            findings=[AdversaryFinding("coverage", d) for d in descs],
        )
        result = compute_reward(verdict, basic_config)
        assert result == expected
        assert isinstance(result, float)


def test_compute_reward_request_changes_zero_findings_is_zero_float(
    basic_config: AdversaryConfig,
) -> None:
    """Ported from legacy: REQUEST_CHANGES with zero findings → 0.0 (a float)."""
    verdict = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=[])
    result = compute_reward(verdict, basic_config)
    assert result == 0.0
    assert isinstance(result, float)


def test_compute_reward_approve_with_findings_is_zero_float(
    basic_config: AdversaryConfig,
) -> None:
    """Ported from legacy: APPROVE with a finding still scores 0.0 (verdict-gated)."""
    verdict = AdversaryVerdict(verdict="APPROVE", findings=[AdversaryFinding("coverage", "x")])
    result = compute_reward(verdict, basic_config)
    assert result == 0.0
    assert isinstance(result, float)


def test_compute_reward_equals_findings_count_float(basic_config: AdversaryConfig) -> None:
    """Ported from legacy (loop n=0..4): reward == float(n) for n REQUEST_CHANGES findings."""
    for n in range(5):
        findings = [AdversaryFinding("coverage", f"t{i}") for i in range(n)]
        verdict = AdversaryVerdict(verdict="REQUEST_CHANGES", findings=findings)
        result = compute_reward(verdict, basic_config)
        assert result == float(n)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Issue #702 — generic persist_reward
# ---------------------------------------------------------------------------


def test_persist_reward_uses_config_filename(tmp_path) -> None:
    """persist_reward writes to config.reward_filename, not the default name."""
    config = AdversaryConfig(valid_categories=["x"], reward_filename="custom_reward.json")
    verdict = AdversaryVerdict(verdict="APPROVE", findings=[])

    persist_reward(str(tmp_path), verdict, 0.0, config)

    assert (tmp_path / "custom_reward.json").exists()
    assert not (tmp_path / "adversary_reward.json").exists()


def test_persist_reward_payload_shape(tmp_path) -> None:
    """Payload has the documented keys; reward_score for a 3-finding REQUEST_CHANGES is 3.0."""
    config = AdversaryConfig(valid_categories=["x"], reward_filename="custom_reward.json")
    verdict = AdversaryVerdict(
        verdict="REQUEST_CHANGES",
        findings=[
            AdversaryFinding("x", "a"),
            AdversaryFinding("x", "b"),
            AdversaryFinding("x", "c"),
        ],
    )

    persist_reward(str(tmp_path), verdict, 3.0, config)

    payload = json.loads((tmp_path / "custom_reward.json").read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "verdict",
        "reward_score",
        "findings_count",
        "findings",
        "persisted_at",
    }
    assert payload["verdict"] == "REQUEST_CHANGES"
    assert payload["findings_count"] == 3
    assert payload["reward_score"] == 3.0
    assert isinstance(payload["findings"], list)
    assert len(payload["findings"]) == 3
    for f in payload["findings"]:
        assert set(f.keys()) == {"category", "description"}


def test_persist_reward_default_filename(tmp_path) -> None:
    """Ported from legacy (test_writes_correct_file): with a default-filename config,
    persist_reward writes 'adversary_reward.json' with reward_score 1.0 for a
    1-finding REQUEST_CHANGES verdict."""
    config = AdversaryConfig(valid_categories=["vague"])  # default reward_filename
    verdict = AdversaryVerdict(
        verdict="REQUEST_CHANGES",
        findings=[AdversaryFinding("vague", "too vague")],
    )

    persist_reward(str(tmp_path), verdict, 1.0, config)

    artifact = tmp_path / "adversary_reward.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["verdict"] == "REQUEST_CHANGES"
    assert payload["reward_score"] == 1.0
    assert payload["findings_count"] == 1
    assert len(payload["findings"]) == 1
    assert "persisted_at" in payload


def test_persist_reward_findings_serialization_order(tmp_path) -> None:
    """Ported from legacy (test_findings_serialized_correctly): findings serialize in
    order with category + description preserved per-element."""
    config = AdversaryConfig(valid_categories=["vague", "leakage"])
    verdict = AdversaryVerdict(
        verdict="REQUEST_CHANGES",
        findings=[
            AdversaryFinding("vague", "Contract A"),
            AdversaryFinding("leakage", "Contract B"),
        ],
    )

    persist_reward(str(tmp_path), verdict, 2.0, config)

    payload = json.loads((tmp_path / "adversary_reward.json").read_text(encoding="utf-8"))
    assert payload["findings"][0]["category"] == "vague"
    assert payload["findings"][0]["description"] == "Contract A"
    assert payload["findings"][1]["category"] == "leakage"
    assert payload["findings"][1]["description"] == "Contract B"


def test_persist_reward_none_output_dir_no_raise(tmp_path) -> None:
    """output_dir=None returns without raising and writes nothing."""
    config = AdversaryConfig(valid_categories=["x"], reward_filename="custom_reward.json")
    verdict = AdversaryVerdict(verdict="APPROVE", findings=[])

    ret = persist_reward(None, verdict, 0.0, config)

    assert ret is None
    assert not (tmp_path / "custom_reward.json").exists()


def test_persist_reward_nonexistent_dir_no_raise(tmp_path) -> None:
    """A nonexistent output_dir returns without raising and creates no file/dir."""
    config = AdversaryConfig(valid_categories=["x"], reward_filename="custom_reward.json")
    verdict = AdversaryVerdict(verdict="APPROVE", findings=[])
    missing = tmp_path / "missing"

    ret = persist_reward(str(missing), verdict, 0.0, config)

    assert ret is None
    assert not missing.exists()


def test_module_exports_compute_and_persist_reward() -> None:
    """compute_reward and persist_reward are exported via __all__ and importable."""
    import orchestration_engine.adversary_parser as ap

    assert "compute_reward" in ap.__all__
    assert "persist_reward" in ap.__all__
    assert callable(ap.compute_reward)
    assert callable(ap.persist_reward)
