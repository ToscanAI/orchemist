# Acceptance Test Phase — Issue #701: Generic Adversary Parser

## Overview

Tests derived exclusively from behavioral contracts in `spec.md`.
No implementation internals are tested — all tests are behavioral (stimulus → observable output).

---

## Behavioral Contracts (one per test)

### `parse_adversary_output` — Happy Path

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_approve_verdict_returned_uppercase` | When text contains `VERDICT: APPROVE`, the result verdict is `"APPROVE"` (uppercase string). | Validates verdict normalization to uppercase as specified. |
| `test_request_changes_verdict_returned_uppercase` | When text contains `VERDICT: REQUEST_CHANGES`, the result verdict is `"REQUEST_CHANGES"`. | Validates the REQUEST_CHANGES path and uppercase normalization. |
| `test_finding_category_stored_lowercase` | Finding categories are always stored in lowercase regardless of input casing. | Spec says category is "always lowercase". |
| `test_finding_description_preserved_verbatim` | The description field after `[category]` is preserved verbatim including whitespace and punctuation. | Spec explicitly states "preserved verbatim from input". |
| `test_raw_text_preserved_verbatim` | `AdversaryVerdict.raw_text` is the original unmodified input string. | Spec says raw_text "original input preserved verbatim" for traceability. |
| `test_approve_with_findings_populates_findings` | An APPROVE verdict can still carry findings — findings are parsed independently of verdict. | Spec says "Findings parsed independently of verdict (APPROVE with finding lines still populates findings)". |
| `test_unknown_category_lines_silently_skipped` | Finding lines with categories NOT in `valid_categories` are silently skipped. | Spec says "silently skip others". |

### `parse_adversary_output` — No Verdict Found (Fallback)

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_no_verdict_defaults_to_request_changes` | When no recognizable verdict is found, the result is `REQUEST_CHANGES`. | Spec: "On no verdict found: return REQUEST_CHANGES". Safety-first default. |
| `test_no_verdict_produces_single_explanatory_finding` | When no verdict is found, exactly one explanatory finding is produced. | Spec: single finding using `fallback_category`. |
| `test_no_verdict_uses_explicit_fallback_category` | When `fallback_category` is set and no verdict found, the explanatory finding uses that category. | Spec: "one finding using `config.fallback_category`". |
| `test_no_verdict_uses_first_category_when_no_fallback` | When `fallback_category=None` and no verdict is found, the first `valid_categories` entry is used. | Spec: "or first category if fallback is None". |

### `parse_adversary_output` — Non-String Input Coercion

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_none_input_is_coerced_and_returns_request_changes` | `None` input is coerced via `str()` and results in `REQUEST_CHANGES`. | Spec: "Coerce non-string input via `str()`, fallback to `""`". |
| `test_integer_input_is_coerced` | Integer input is coerced via `str()` and stored in `raw_text`. | Same coercion contract — validates non-None non-string types. |
| `test_dict_input_is_coerced` | Dict input is coerced via `str()` without raising an exception. | Validates `text: Any` signature — function never raises on any input type. |

### `extract_verdict` — scan_order Behavior

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_verdict_scan_last_returns_last_verdict` | `scan_order="last"` returns the LAST `VERDICT:` line. | Spec: Pass 1 scans in reverse, last match wins when `scan_order="last"`. |
| `test_verdict_scan_first_returns_first_verdict` | `scan_order="first"` returns the FIRST `VERDICT:` line. | Spec: "Pass 1 scans forward, first match wins" for `scan_order="first"`. |
| `test_verdict_scan_default_is_last` | Default `scan_order` behavior (no argument) is equivalent to `"last"`. | Spec: "`scan_order="last"` (default): current behavior … Backward compatible." |
| `test_verdict_extract_returns_none_for_empty_text` | Empty or whitespace-only text (and None) returns `None`. | Edge case: spec says text is optional; empty input should not produce a verdict. |
| `test_verdict_allowed_verdicts_filter` | When `allowed_verdicts` excludes a found verdict, `None` is returned. | Spec: `allowed_verdicts` parameter filters which verdicts are valid to return. |
| `test_verdict_allowed_verdicts_pass` | A verdict in `allowed_verdicts` passes through normally. | Validates the positive path of the `allowed_verdicts` filter. |

### `parse_adversary_output` — verdict_scan Config Integration

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_adversary_parser_respects_verdict_scan_first` | When `config.verdict_scan="first"`, the FIRST VERDICT line in the text is used. | Validates config wiring — `parse_adversary_output` must pass `scan_order` to `verdict_parser`. |
| `test_adversary_parser_respects_verdict_scan_last` | When `config.verdict_scan="last"`, the LAST VERDICT line is used. | Same wiring check for the "last" direction. |

### `_parse_adversary_config` — Template Parsing

| Test | Behavioral Contract | Rationale |
|------|---------------------|-----------|
| `test_parse_adversary_config_returns_none_for_non_dict` | Non-dict values (None, string, int, list) return `None`. | Spec: "Returns None if key not present" — non-dict is treated as absent. |
| `test_parse_adversary_config_valid_input` | A valid dict produces an `AdversaryConfig` with correct fields. | Happy path: validates the entire dict → dataclass conversion. |
| `test_parse_adversary_config_raises_on_empty_categories` | Empty `valid_categories` list raises `ValueError`. | Spec/validation: "Empty `valid_categories` → error". |
| `test_parse_adversary_config_raises_when_fallback_not_in_categories` | `fallback_category` not in `valid_categories` raises `ValueError`. | Spec/validation: "`fallback_category` not in `valid_categories` → error". |
| `test_parse_adversary_config_raises_on_invalid_verdict_scan` | `verdict_scan` not `"first"` or `"last"` raises `ValueError`. | Spec/validation: "`verdict_scan` not 'first' or 'last' → error". |
| `test_parse_adversary_config_deduplicates_categories_preserving_order` | Duplicate entries in `valid_categories` are silently removed, preserving first occurrence order. | Spec: "Deduplicates valid_categories silently (preserve order)". |
| `test_parse_adversary_config_defaults_verdict_scan_to_last` | When `verdict_scan` is absent from the dict, it defaults to `"last"`. | Spec: `verdict_scan: str = "last"` default. |
| `test_parse_adversary_config_reward_fields_parsed_but_not_acted_on` | `reward_enabled` and `reward_filename` are stored in the config, not acted upon. | Spec: "parsed but NOT acted on in this phase". |

---

## Ambiguities and Assumptions

1. **behavioral.md absent**: The file `/tmp/702-output/behavioral.md` did not exist. Tests were derived directly from `spec.md` (the implementation spec) which contained explicit behavioral contracts in algorithm steps, data structures, and validation rules. This is consistent with the pipeline intent.

2. **`_parse_adversary_config` is a module-level function**: The spec describes it as a helper in `templates.py`. Since it is an exported module-level function (not a private method that requires introspection), testing it directly is behavioral — it expresses what the template parser does when given specific inputs.

3. **Finding description "verbatim" after separator**: The spec says "preserved verbatim from input" and the regex splits on `[category] ` with a single space. The test asserts the description is everything after `[category] ` with no stripping. If the implementation strips leading whitespace from descriptions, this test would need adjustment.

4. **`reward_enabled`/`reward_filename` "not acted on"**: The spec says these are "parsed but NOT acted on in this phase." The acceptance test validates they are stored, but cannot verify absence of side effects without integration tests (outside scope per spec).

5. **No `PhaseDefinition` template integration test**: The spec describes wiring `adversary_config` into `PhaseDefinition`, but testing full YAML template parsing is integration-level. That is outside the specified test scope ("No integration/pipeline tests in this phase").
