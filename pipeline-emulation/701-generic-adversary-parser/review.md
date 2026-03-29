# Code Review — Issue #701: Generic Adversary Parser (Phase 1)

**Reviewer:** Toscan (Opus 4.6, review phase)  
**Date:** 2026-03-29  
**Files reviewed:**
1. `src/orchestration_engine/adversary_parser.py` (NEW)
2. `src/orchestration_engine/verdict_parser.py` (MODIFIED)
3. `src/orchestration_engine/templates.py` (MODIFIED)

**Test results:** 64/64 acceptance tests pass, 6994/6994 full suite (0 regressions)

---

## 1. Correctness

### All behavioral contracts satisfied ✅

Every contract from `behavioral.md` (Sections 1–8) has a corresponding acceptance test that passes. I verified the following key behaviors directly:

- **Verdict extraction:** `APPROVE` / `REQUEST_CHANGES` correctly parsed with both `verdict_scan="first"` and `"last"`.
- **Category filtering:** Invalid categories silently skipped; valid categories included; order preserved.
- **Input coercion:** `None`, empty string, `int`, `dict`, `list`, `bool`, `float`, `object()` — all handled without raising.
- **No-verdict fallback:** Correctly defaults to `REQUEST_CHANGES` with explanatory finding using `fallback_category` or first valid category.
- **Findings independent of verdict:** `APPROVE` with finding lines still populates `findings`.
- **`raw_text` preservation:** Byte-identical to input (or `str(input)` for coerced types).
- **`scan_order` in verdict_parser:** Default `"last"` is backward compatible; `"first"` correctly scans forward.
- **Template parsing:** `AdversaryConfig` populated from YAML, `None` when absent, defaults correct.
- **Validation:** Empty categories, bad fallback, invalid `verdict_scan` all rejected with clear errors; deduplication works; unknown fields logged.

### Edge cases covered ✅

- Empty `valid_categories` at runtime (bypasses template validation): handled gracefully with `"unknown"` fallback category.
- Whitespace-only string: correctly treated as no-verdict → `REQUEST_CHANGES`.
- Single valid category: works correctly.
- All finding categories invalid: verdict still parsed, findings list is empty.

## 2. Code Quality

### Clean, well-documented, consistent with codebase style ✅

- Docstrings are thorough — algorithm steps documented, parameter semantics clear, return types specified.
- `__all__` exports defined (matches existing modules).
- Logging at appropriate levels: `DEBUG` for skipped lines, `WARNING` for missing verdicts.
- Code structure mirrors existing parsers (`spec_adversary.py`, `acceptance_test_adversary.py`) making the codebase internally consistent.
- Step comments (`── 1. Coerce...`, `── 2. Extract...`) aid readability.
- `_parse_adversary_config` follows the exact same pattern as `_parse_git_config`, `_parse_budget_config`, etc.

### No unnecessary complexity ✅

The implementation is minimal and direct. No over-abstraction, no unnecessary indirection.

## 3. Integration Safety

### No side effects on import ✅

Module-level code is limited to: logger setup, regex compilation, dataclass definitions. No I/O, no global state mutation.

### No circular import risks ✅

Verified dependency chain:
```
verdict_parser.py → (no internal imports)
adversary_parser.py → verdict_parser
templates.py → adversary_parser, git_integration, routing
```
Clean DAG. No cycles possible.

### Backward compatibility of verdict_parser changes ✅

The `scan_order` parameter:
- Defaults to `"last"` — existing callers get identical behavior with zero code changes.
- Added as keyword-only (positionally safe since it's the 4th parameter after `text`, `file_path`, `allowed_verdicts`).
- Pass 2 (fallback regex) behavior completely unchanged regardless of `scan_order`.

All 6994 existing tests pass — confirmed zero regressions.

## 4. Deviations from Spec

### Finding regex differs from existing parsers (intentional, acceptable)

The generic parser uses `^\s*\[([A-Za-z_]+)\] (.*)$` (requires exactly one literal space after `]`, no stripping of description).

Compared to:
- **spec_adversary:** `^\s*\[([A-Za-z_]+)\]\s+(.+)$` — requires 1+ whitespace, requires non-empty description, strips description via `.strip()`.
- **acceptance_test_adversary:** `^\s*\[([A-Za-z_]+)\]\s*(.*)$` — allows 0+ whitespace, allows empty description, strips description.

Practical differences:
| Input | Generic | spec_adversary | acceptance_test_adversary |
|---|---|---|---|
| `[cat]\tdesc` | No match | Match | Match |
| `[cat]desc` | No match | No match | Match |
| `[cat]  two spaces` | Match (leading space in desc) | Match (stripped) | Match (stripped) |

**Assessment:** This is a deliberate standardization. LLMs consistently produce `[category] description` with a single space. The `(.*)` without stripping aligns with the behavioral contract ("preserved verbatim"). This is a clean design choice that simplifies the regex and makes behavior more predictable. When Phases 2-3 migrate existing phases to the generic parser, the prompts already produce single-space format, so no behavioral change expected.

### `valid_categories` not auto-lowercased in template parsing

`_parse_adversary_config` uses `str(cat)` without `.lower()`. Since the parser lowercases text categories before comparison, YAML categories must be lowercase to match. The spec documents this: *"case-sensitive — should match what the LLM emits, typically lowercase"*.

**Assessment:** Acceptable — consistent with the spec and with how existing parsers use lowercase hardcoded sets. Could add `.lower()` normalization in `_parse_adversary_config` for user-friendliness, but this is a Phase 2/3 concern when actual YAML templates are written.

### No other deviations detected.

## 5. Security / Robustness

### No input can cause an exception ✅

- Non-string input: `try/except` on `str()` with fallback to `""`.
- `extract_verdict` returns `None` safely on any input.
- `_FINDING_RE.match()` never raises.
- Category lookup uses set membership — no KeyError possible.
- Template parsing validates all fields before constructing `AdversaryConfig`, with clear `ValueError` messages.

### Logging appropriate ✅

- `DEBUG`: skipped categories (operational noise).
- `WARNING`: no verdict found, unknown config fields.
- No sensitive data logged (only first 120 chars of output, category tokens).

## 6. Architecture

### Foundation for Phases 2-3 ✅

- `AdversaryConfig` on `PhaseDefinition` gives the sequencer a clean typed interface to detect adversary phases and drive parsing.
- `reward_enabled` and `reward_filename` are parsed and stored but not acted on — clean placeholder for Phase 2 (#702).
- The sequencer can do `if phase.adversary_config: result = parse_adversary_output(output, phase.adversary_config)` — no special casing needed.
- `AdversaryVerdict` is a standalone dataclass — easy to serialize for reward persistence.

### Generic parser can replace both existing parsers ✅

Both `spec_adversary.py` and `acceptance_test_adversary.py` can be replaced by configuring `AdversaryConfig` with their respective category sets:

```python
# spec_adversary replacement
AdversaryConfig(
    valid_categories=["vague", "trivial", "missing_edge_case", "leakage", "divergence"],
    fallback_category="vague",
    verdict_scan="last",
)

# acceptance_test_adversary replacement  
AdversaryConfig(
    valid_categories=["coverage", "trivial_satisfaction", "leakage", "specificity"],
    fallback_category="coverage",
    verdict_scan="first",
)
```

The only functional difference is the regex (discussed in Section 4 above), which is immaterial for real LLM output.

---

## Summary

| Criterion | Rating |
|---|---|
| Correctness | ✅ All contracts satisfied |
| Code Quality | ✅ Clean, well-documented, idiomatic |
| Integration Safety | ✅ No side effects, no circular imports, backward compatible |
| Spec Adherence | ✅ Minor intentional deviations, all justified |
| Security / Robustness | ✅ Never raises, logging appropriate |
| Architecture | ✅ Clean foundation for Phases 2-3 |

No blocking issues. No requested changes. Implementation is clean, correct, and well-integrated.

**VERDICT: APPROVE**
