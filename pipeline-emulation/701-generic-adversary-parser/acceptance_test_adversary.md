# Acceptance Test Adversary Review — Issue #701

**Round:** 1  
**Reviewer:** Adversary (automated)  
**Date:** 2026-03-29  

---

## Dimension 1: Coverage

All 30 behavioral contracts across 8 sections have at least one corresponding test.

| Section | Contracts | Tests | Status |
|---------|-----------|-------|--------|
| 1. Verdict Extraction | 5 | 5 direct tests | ✅ Full |
| 2. Category Filtering | 4 | 5 tests (one contract has 2) | ✅ Full |
| 3. Input Coercion | 3 | 8 tests (types broken out) | ✅ Full |
| 4. Data Structures | 3 | 10 tests (thorough) | ✅ Full |
| 5. Config-Driven Behavior | 3 | 4 tests | ✅ Full |
| 6. verdict_parser scan_order | 4 | 10 tests | ✅ Full |
| 7. PhaseDefinition Parsing | 3 | 4 tests | ✅ Full |
| 8. Validation | 5 | 7 tests | ✅ Full |

**No coverage gaps found.**

---

## Dimension 2: Trivial Satisfaction

### `[trivial_satisfaction]` FAIL — `test_unknown_field_in_adversary_config_logs_warning`

**Contract (Section 8):** "When adversary_config contains an unknown field (e.g. unknown_key: true), the system logs a warning"

The test captures warnings into `captured_warnings` via a custom log handler, but **never asserts anything about `captured_warnings`**. The only assertions are:

```python
assert tpl is not None
assert phase.adversary_config is not None
assert not hasattr(phase.adversary_config, "unknown_key")
```

An implementation that silently ignores unknown fields **without logging any warning** would pass this test. The core contract obligation — "logs a warning" — is completely unverified.

**Fix required:** Add `assert len(captured_warnings) >= 1` and optionally assert that at least one record mentions the unknown field name.

### `[trivial_satisfaction]` FAIL — `test_raw_text_coerced_none_input`

**Contract (Section 4):** "raw_text (str, original input preserved verbatim — byte-identical to what was passed in, or `str(input)` if coerced)"

For `None` input, `str(None)` = `"None"`. The test only asserts:

```python
assert isinstance(result.raw_text, str)
```

Any string value passes — `""`, `"hello"`, `"garbage"`. A hardcoded `raw_text = ""` stub satisfies this test despite violating the contract.

**Fix required:** Assert `result.raw_text == "None"` (the result of `str(None)`), matching the contract's `str(input)` requirement.

### `[trivial_satisfaction]` MARGINAL — `test_all_non_string_types_do_not_raise`

Asserts `result.verdict in ("APPROVE", "REQUEST_CHANGES")` when the contract mandates `REQUEST_CHANGES`. Mitigated by individual type tests (int, dict, list, bool, float) that each assert `== "REQUEST_CHANGES"`. However, the loop includes types NOT covered by individual tests (`object()`, `0`, `-1`, `{}`, `[]`, `0.0`, `False`) where only the weak assertion applies. A stub returning `APPROVE` for `object()` would pass.

**Fix required:** Change to `assert result.verdict == "REQUEST_CHANGES"` in the loop, or add individual tests for the additional types.

---

## Dimension 3: Leakage

**No leakage found.**

All referenced APIs and names appear in behavioral.md:
- `parse_adversary_output`, `AdversaryConfig` — behavioral.md Sections 1-5
- `extract_verdict`, `scan_order` — behavioral.md Section 6
- `phase.adversary_config` — behavioral.md Section 7
- `TemplateEngine` — existing public API for template loading (behavioral.md references "YAML phase" loading)
- `reward_enabled` — behavioral.md Section 7 contract 3 explicitly mentions this default

The `logging.getLogger("orchestration_engine.templates")` logger name in the warning test could be considered leakage (references internal module path), but behavioral.md Section 8 contract itself says "consistent with existing unknown-field handling in templates.py", directly referencing the module. Acceptable.

---

## Dimension 4: Specificity

### `[specificity]` FAIL — `test_unknown_field_in_adversary_config_logs_warning`

(Same test as trivial_satisfaction above.) No assertion on the warning content. Even if we add `assert len(captured_warnings) >= 1`, we should also verify the warning message references the unknown field name ("unknown_key") to discriminate between an implementation that warns about the right thing vs. one that logs unrelated warnings during loading.

### `[specificity]` FAIL — `test_pass2_used_when_pass1_finds_nothing_scan_order_last`

**Contract (Section 6):** "Pass 2 behavior is unchanged by scan_order"

The test uses `text = "The code APPROVE the review."` and only asserts:

```python
assert result_last == result_first
```

A stub returning `None` (or any constant) for both scan_order values satisfies this. The test should also assert the **expected return value** — since the text contains the word "APPROVE", Pass 2 should match it:

```python
assert result_last == "approve"
assert result_first == "approve"
```

This both verifies Pass 2 is working AND that scan_order doesn't change it.

### `[specificity]` MINOR — `test_invalid_category_no_exception`

Asserts `result.verdict in ("APPROVE", "REQUEST_CHANGES")` for input `"VERDICT: APPROVE\n[badcat] Unexpected category\n"`. The verdict should be specifically `"APPROVE"` since the text contains `VERDICT: APPROVE`. The broad assertion allows REQUEST_CHANGES to pass, which would be incorrect. This is a supplementary test (the primary contract test is `test_invalid_category_silently_skipped`) so impact is low, but it should be tightened.

---

## Summary of Required Changes

| # | Test | Dimension | Severity | Fix |
|---|------|-----------|----------|-----|
| 1 | `test_unknown_field_in_adversary_config_logs_warning` | trivial_satisfaction + specificity | **HIGH** | Assert `len(captured_warnings) >= 1` and that warning references "unknown_key" |
| 2 | `test_raw_text_coerced_none_input` | trivial_satisfaction | **HIGH** | Assert `result.raw_text == "None"` instead of just `isinstance(..., str)` |
| 3 | `test_all_non_string_types_do_not_raise` | trivial_satisfaction | **MEDIUM** | Change loop assertion to `result.verdict == "REQUEST_CHANGES"` |
| 4 | `test_pass2_used_when_pass1_finds_nothing_scan_order_last` | specificity | **MEDIUM** | Also assert the expected return value (e.g., `== "approve"`) |
| 5 | `test_invalid_category_no_exception` | specificity | **LOW** | Change to `assert result.verdict == "APPROVE"` |

Items 1 and 2 are **blocking** — they represent contracts that are entirely unverified by their tests. A no-op implementation could pass them. Items 3-4 are important but partially mitigated by companion tests. Item 5 is a minor tightening.

### Note: Dead Code in `_capture_warnings`

The `_ctx()` inner function references `logging.handlers_ListHandler(records)` which is an invalid attribute (should be `logging.handlers.ListHandler` which also doesn't exist in stdlib). This code is never called (the method returns `_simple_ctx()` instead), so it's not a runtime error, but it's dead code that should be removed to avoid confusion.

---

VERDICT: REQUEST_CHANGES
