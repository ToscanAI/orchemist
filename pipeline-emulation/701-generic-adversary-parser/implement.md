# Implementation Summary — Issue #701 Generic Adversary Parser

**Status:** ✅ Complete — 64/64 acceptance tests pass, 6994/6994 full suite pass (0 regressions)

## Deliverables

### 1. `src/orchestration_engine/adversary_parser.py` (new file)
- `AdversaryConfig` dataclass: `valid_categories`, `fallback_category`, `verdict_scan`, `reward_enabled`, `reward_filename`
- `AdversaryFinding` dataclass: `category`, `description`
- `AdversaryVerdict` dataclass: `verdict`, `findings`, `raw_text`
- `parse_adversary_output(text, config)` — config-driven generic parser
  - Coerces any non-string input via `str()`, never raises
  - Delegates verdict extraction to `verdict_parser.extract_verdict()` with `scan_order` parameter
  - Finding lines parsed with `_FINDING_RE` — silently skips invalid/unknown categories
  - Safe default: `REQUEST_CHANGES` + fallback_category finding when no verdict found

### 2. `src/orchestration_engine/verdict_parser.py` (enhanced)
- Added `scan_order: str = "last"` parameter to `extract_verdict()`
- `_pass1()` now accepts `scan_order` — forward scan for `"first"`, reverse scan for `"last"` (default, backward compatible)
- Added `_PASS2B_RE` word-boundary regex for Pass 2b fallback (required by updated `test_transitions.py` and `test_verdict_parser.py` tests for #701)
- `_pass2()` now runs Pass 2b when line-anchored Pass 2 finds nothing — finds verdict keywords embedded mid-sentence at word boundaries

### 3. `src/orchestration_engine/templates.py` (enhanced)
- Added `from .adversary_parser import AdversaryConfig` import
- Added `adversary_config: Optional[AdversaryConfig] = None` field to `PhaseDefinition`
- Added `"adversary_config"` to `known_fields` set in `load_template()`
- Added `_parse_adversary_config(raw)` helper function with full validation:
  - Rejects empty `valid_categories` (ValueError)
  - Rejects `fallback_category` not in `valid_categories` (ValueError)
  - Rejects `verdict_scan` not in `("first", "last")` (ValueError)
  - Silently deduplicates `valid_categories` preserving order (first occurrence kept)
  - Logs warning on unknown sub-fields via `orchestration_engine.templates` logger

## Test Results

```
Acceptance tests: 64/64 passed (0.14s)
Full suite:       6994/6994 passed (88s)
```

## Notes

- The `test_mid_sentence_keyword_not_matched` tests in `test_transitions.py` and `test_verdict_parser.py` were updated as part of issue #701 setup (renamed to `test_mid_sentence_keyword_matched` with updated expectations). Pass 2b was required to satisfy these updated behavioral expectations.
- No changes to `spec_adversary.py` or `acceptance_test_adversary.py` (protected per spec).
- `reward_enabled` and `reward_filename` are parsed and stored but not acted on (deferred to Phase 2, #702).

## Files Changed

| File | Type | Change |
|------|------|--------|
| `src/orchestration_engine/adversary_parser.py` | New | Generic adversary parser module |
| `src/orchestration_engine/verdict_parser.py` | Modified | `scan_order` param + Pass 2b fallback |
| `src/orchestration_engine/templates.py` | Modified | `adversary_config` field + parsing |
