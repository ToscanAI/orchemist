# Acceptance Test Adversary Review — Round 2

**Issue:** #701 — Generic Adversary Parser
**Reviewer:** Adversary (Round 2)
**Focus:** Verify 5 fixes from Round 1 findings; check for regressions.

---

## Fix Verification

### Fix 1: `test_raw_text_coerced_none_input` — `result.raw_text == "None"`
**Round 1 finding:** Weak assertion (`isinstance` check instead of value check).
**Fix:** Now asserts `result.raw_text == "None"`.
**Contract (Section 4):** "raw_text (str, original input preserved verbatim — byte-identical to what was passed in, or `str(input)` if coerced)"
**Verdict:** ✅ **RESOLVED.** `str(None)` is `"None"` — the assertion is exact and matches the contract. No new issues.

### Fix 2: `test_invalid_category_no_exception` — asserts specific verdict value
**Round 1 finding:** Only asserted `result is not None` — tautological for a function that returns a dataclass.
**Fix:** Now asserts `result.verdict == "APPROVE"`.
**Contract (Section 2):** Invalid categories are silently skipped; verdict extraction is independent.
**Verdict:** ✅ **RESOLVED.** The input text contains `VERDICT: APPROVE`, so `verdict == "APPROVE"` is the correct expected value. Meaningful assertion that confirms the parser processed the input rather than crashing.

### Fix 3: `test_all_non_string_types_do_not_raise` — asserts `== "REQUEST_CHANGES"`
**Round 1 finding:** Only asserted `result is not None` — no behavioral verification.
**Fix:** Now asserts `result.verdict == "REQUEST_CHANGES"` for each non-string type.
**Contract (Section 3):** "the system coerces via `str()` and returns `verdict="REQUEST_CHANGES"` — never raises regardless of input type"
**Verification of correctness:** The test inputs `[0, -1, {}, [], False, 0.0, object()]` produce `str()` representations that contain no `VERDICT:` line and no `APPROVE`/`REQUEST_CHANGES` token in the text body, so the no-verdict fallback path fires → `REQUEST_CHANGES`. This matches the contract.
**Verdict:** ✅ **RESOLVED.** Direct contract-to-assertion traceability. No new issues.

### Fix 4: `test_pass2_used_when_pass1_finds_nothing_scan_order_last` — asserts expected verdict values
**Round 1 finding:** Asserted `result_last == result_first` (relative equality) without asserting the actual expected value.
**Fix:** Now asserts `result_last == "approve"` and `result_first == "approve"`.
**Contract (Section 6):** "When Pass 1 finds no match regardless of scan_order, Pass 2 (fallback regex with priority ordering) is used — Pass 2 behavior is unchanged by scan_order"
**Verification:** Input `"The code APPROVE the review."` has no structured `VERDICT:` line → Pass 1 finds nothing → Pass 2 regex matches `APPROVE` → returns `"approve"`. Both scan orders produce the same result because Pass 2 is scan_order-independent.
**Verdict:** ✅ **RESOLVED.** Both absolute value and scan_order-independence are now verified. No new issues.

### Fix 5: `test_unknown_field_in_adversary_config_logs_warning` — asserts warning count and content
**Round 1 finding:** Only checked that template loaded; no assertion on warning emission or content.
**Fix:** Now asserts:
- `len(captured_warnings) >= 1` — at least one warning emitted
- `any("unknown_key" in record.getMessage() for record in captured_warnings)` — warning references the field
- `not hasattr(phase.adversary_config, "unknown_key")` — unknown field doesn't leak onto config

**Contract (Section 8):** "the system logs a warning (consistent with existing unknown-field handling in templates.py)"
**Verdict:** ✅ **RESOLVED.** The assertions now verify all three aspects of the contract: warning is emitted, warning identifies the offending field, and the field doesn't corrupt the config object.

---

## Regression Check

Reviewed all 5 fixes for unintended side effects:

- **No new imports or dependencies introduced.**
- **No test logic changes outside the 5 targeted tests.**
- **No assertion weakening** — all changes strictly strengthened assertions.
- **No test isolation issues** — each fix is self-contained within its test method.

**Minor observation (non-blocking):** The `_capture_warnings` helper contains dead code (`_ctx` function with reference to nonexistent `logging.handlers_ListHandler`). Since only `_simple_ctx` is returned and invoked, this is cosmetic — it doesn't affect correctness. This existed before Round 1 and is not within scope.

---

## Summary

| Fix | Status | New Issues |
|-----|--------|------------|
| 1. `test_raw_text_coerced_none_input` | ✅ Resolved | None |
| 2. `test_invalid_category_no_exception` | ✅ Resolved | None |
| 3. `test_all_non_string_types_do_not_raise` | ✅ Resolved | None |
| 4. `test_pass2_used_when_pass1_finds_nothing_scan_order_last` | ✅ Resolved | None |
| 5. `test_unknown_field_in_adversary_config_logs_warning` | ✅ Resolved | None |

All 5 Round 1 findings are resolved. No regressions or new issues introduced by the fixes.

**VERDICT: APPROVE**
