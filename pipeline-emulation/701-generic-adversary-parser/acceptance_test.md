# Acceptance Test Report — Issue #701: Generic Adversary Parser

**Phase:** ACCEPTANCE_TEST  
**Round:** 2 (post-adversary revision)  
**Date:** 2026-03-29  
**Test file:** `acceptance_tests.py`  
**Run command:** `python3 -m pytest pipeline-emulation/701-generic-adversary-parser/acceptance_tests.py -v`

---

## Result Summary

| Status | Count |
|--------|-------|
| Total tests | 64 |
| Passing (expected) | 4 |
| Failing (expected — not implemented yet) | 60 |

**Overall verdict: ✅ ACCEPTANCE TEST PHASE COMPLETE (Round 2)**

All failures are _expected_: they reflect missing implementation, not test defects. The 4 passing tests confirm existing behaviors that must remain intact after implementation.

---

## Round 2 Adversary Fixes Applied

5 tests were flagged by the adversary (Round 1) and surgically fixed:

| # | Test | Dimension | Fix Applied |
|---|------|-----------|-------------|
| 1 | `test_unknown_field_in_adversary_config_logs_warning` | trivial_satisfaction + specificity | Added `assert len(captured_warnings) >= 1` and assertion that warning references `"unknown_key"` |
| 2 | `test_raw_text_coerced_none_input` | trivial_satisfaction | Changed `isinstance(..., str)` to `result.raw_text == "None"` |
| 3 | `test_all_non_string_types_do_not_raise` | trivial_satisfaction | Changed loop assertion from `in ("APPROVE", "REQUEST_CHANGES")` to `== "REQUEST_CHANGES"` |
| 4 | `test_pass2_used_when_pass1_finds_nothing_scan_order_last` | specificity | Replaced equality-between-results with explicit `== "approve"` assertions on both |
| 5 | `test_invalid_category_no_exception` | specificity | Changed broad `in (...)` assertion to `== "APPROVE"` (text contains `VERDICT: APPROVE`) |

All other 59 tests are byte-identical to Round 1.

---

## Coverage by Behavioral Contract Section

| Section | Contracts | Tests Written |
|---------|-----------|---------------|
| 1. Generic Parser — Verdict Extraction | 5 | 5 |
| 2. Generic Parser — Category Filtering | 4 | 5 |
| 3. Generic Parser — Input Coercion | 3 | 9 (all input types) |
| 4. Generic Parser — Data Structures | 3 | 10 |
| 5. Generic Parser — Config-Driven Behavior | 3 | 4 |
| 6. verdict_parser Enhancement — scan_order | 4 | 10 |
| 7. PhaseDefinition Parsing | 3 | 4 |
| 8. Validation | 5 | 7 |
| Edge cases & boundaries | — | 10 |
| **Total** | **30** | **64** |

---

## Failure Categories (by root cause)

### Sections 1–5 and edge cases: `adversary_parser` module missing
```
ModuleNotFoundError: No module named 'orchestration_engine.adversary_parser'
```
**Fix required:** Create `src/orchestration_engine/adversary_parser.py` with:
- `AdversaryConfig` dataclass (`valid_categories`, `fallback_category`, `verdict_scan`, `reward_enabled`, `reward_filename`)
- `AdversaryFinding` dataclass (`category`, `description`)
- `AdversaryVerdict` dataclass (`verdict`, `findings`, `raw_text`)
- `parse_adversary_output(text: Any, config: AdversaryConfig) -> AdversaryVerdict`

### Section 6: `scan_order` parameter missing from `extract_verdict`
```
TypeError: extract_verdict() got an unexpected keyword argument 'scan_order'
```
**Fix required:** Add `scan_order: str = "last"` to `extract_verdict()` signature in `verdict_parser.py`. Pass 1 must scan in reverse when `scan_order="last"` (current) and forward when `scan_order="first"`.

### Sections 7–8: `adversary_config` attribute missing from `PhaseDefinition`
```
AttributeError: 'PhaseDefinition' object has no attribute 'adversary_config'
```
**Fix required:**
- Add `adversary_config: Optional[AdversaryConfig] = None` to `PhaseDefinition`
- Add `_parse_adversary_config()` helper in `templates.py`
- Add `"adversary_config"` to `known_fields` in the phase-parsing loop
- Implement validation: reject empty `valid_categories`, reject `fallback_category` not in `valid_categories`, reject `verdict_scan` not in `{"first", "last"}`
- Deduplicate `valid_categories` silently (preserve order)
- Log warning on unknown sub-fields (referenced by logger name `"orchestration_engine.templates"`)

---

## Tests That Pass Today (and Must Keep Passing)

| Test | Reason |
|------|--------|
| `test_scan_order_param_does_not_break_existing_signature` | Tests existing `extract_verdict()` call without `scan_order` — still works |
| `test_scan_order_defaults_to_last_backward_compat` | Existing last-match-wins behavior confirmed correct |
| `test_verdict_scan_first_is_valid` | Template with `verdict_scan: first` loads (unknown field today, valid after impl) |
| `test_verdict_scan_last_is_valid` | Template with `verdict_scan: last` loads (unknown field today, valid after impl) |

⚠️ Note: `test_verdict_scan_first_is_valid` and `test_verdict_scan_last_is_valid` pass today because the template loader silently drops unknown fields. After implementation, they pass because the field is explicitly valid. Either way they pass — which is the correct behavior.

---

## Test Design Decisions

### Discovery-based import for generic parser (Sections 1–5)
Tests use `importlib.import_module("orchestration_engine.adversary_parser")` and `getattr()` to discover `parse_adversary_output` and `AdversaryConfig`. This avoids hardcoding internal module structure while still testing the public behavioral API.

### Specific assertions throughout
Every test has meaningful assertions beyond existence checks:
- Verdict values are asserted as exact strings (`"APPROVE"` / `"REQUEST_CHANGES"`)
- Finding counts and category values are explicitly checked
- `raw_text` is tested for verbatim preservation and exact coercion value (`"None"` for `None` input)
- Category ordering is checked where contracts specify it
- Warning capture assertions verify the warning references the unknown field name

### Boundary and edge cases covered
- All non-string input types: `None`, `int`, `float`, `bool`, `dict`, `list`, `object()`
- Empty string and whitespace-only string
- Mixed valid/invalid categories in one text
- Multiple verdict lines with both `scan_order` values
- `fallback_category=None` resolves to first valid category
- Duplicate `valid_categories` deduplication preserving order

---

## Implementation Checklist

Once each item is implemented, the corresponding tests should pass:

- [ ] `src/orchestration_engine/adversary_parser.py` created → 33 tests pass (Sections 1–5, edge cases)
- [ ] `verdict_parser.extract_verdict(scan_order=...)` added → 8 more tests pass (Section 6)
- [ ] `PhaseDefinition.adversary_config` field added + parsing → 4 more tests pass (Section 7)
- [ ] Validation logic in `_parse_adversary_config()` → 5 more tests pass (Section 8)
- [ ] All 64 tests pass → implementation complete per behavioral contracts
