# Code Review: #194 — Supervisor Hooks

**Branch:** `feat/194-supervisor-hooks`  
**Reviewer:** Claude (Opus 4.6)  
**Date:** 2026-02-28  
**Verdict:** ✅ **APPROVE**

---

## Summary

Adds a supervisor hook that evaluates phase output after successful execution. Supports three verdicts (APPROVE/REVISE/ABORT) with configurable retry limits. Clean implementation, solid test coverage.

## Files Reviewed

### `src/orchestration_engine/templates.py`
- **5 new fields** on `PhaseDefinition`: `supervisor`, `supervisor_prompt`, `supervisor_model`, `supervisor_rubric`, `supervisor_max_retries` ✅
- `__post_init__` normalisation: handles `None`, negative, and float values for `max_retries` ✅
- Fields added to YAML known-fields set — no spurious warnings ✅
- **Default `supervisor=False`** — zero behavioral change for existing pipelines ✅

### `src/orchestration_engine/sequencer.py`
- `_DEFAULT_SUPERVISOR_PROMPT` — clear, well-structured with `{rubric}` and `{phase_output}` placeholders ✅
- `_run_supervisor_for_phase()` — correct APPROVE/REVISE/ABORT loop with bounded retries ✅
- `_parse_supervisor_response()` — case-insensitive, first-non-blank-line matching, defaults to APPROVE on gibberish (safe fallback) ✅
- Wired into both `_execute_wave_sequential` (line ~290) and `_run_phase` parallel path (line ~430) ✅
- Supervisor only runs on `state == 'success'` — failed phases skip supervisor correctly ✅
- `phase_outputs` updated under lock after revision ✅

### `tests/test_supervisor.py` — 35 tests, all passing
| Category | Tests | Coverage |
|----------|-------|----------|
| `_parse_supervisor_response` | 9 | case sensitivity, multiline, no verdict, empty string |
| `PhaseDefinition` fields | 5 | defaults, all fields, None/negative/float coercion |
| YAML parsing | 2 | with and without supervisor fields |
| Supervisor disabled | 2 | no extra calls, failed phase still aborts normally |
| APPROVE flow | 5 | single/multi-phase, default/custom prompt, result storage |
| REVISE flow | 3 | revise→approve, feedback injection, counter logic |
| ABORT flow | 4 | abort flag, failed_phase, downstream blocked, supervisor_abort |
| Max retries | 5 | 0/1/2 retries, logging, exact call counts |

## Edge Cases Analysis

| Scenario | Handling | Assessment |
|----------|----------|------------|
| Supervisor prompt fails/times out | Treated as task failure; `_execute_and_wait` returns failed result; `_extract_phase_text` returns empty string; parser defaults to APPROVE | ⚠️ Acceptable but worth noting — a supervisor failure silently approves. Consider logging a warning here in a follow-up. |
| Supervisor returns gibberish | `_parse_supervisor_response` defaults to APPROVE with warning log | ✅ Safe default — pipeline continues rather than blocking |
| `supervisor=True` but no executor | Executor selection is upstream; same failure path as any unconfigured phase | ✅ No special case needed |
| Revised phase itself fails | Detected at line ~860, returns abort dict | ✅ |

## Backward Compatibility

**No breaking changes.** `supervisor` defaults to `False`. When disabled:
- No supervisor task is submitted
- No additional executor calls
- Existing pipeline behavior is identical (confirmed by `TestSupervisorDisabled`)

## Minor Observations

1. **Stray #190 commit** (`b645d51`): Adds `COMMAND` task type to `schemas.py`. Unrelated to #194 but harmless — just adds an enum value and escalation path. **Flagged, not blocking.**

2. **Module-level alias** `_parse_supervisor_response = PhaseSequencer._parse_supervisor_response` — convenient for tests but slightly unusual. Fine for internal use.

3. **Supervisor timeout** inherits from `phase.timeout_minutes` — reasonable default but could be a separate config in the future.

## Test Results

```
tests/test_supervisor.py: 35 passed (0.11s)
tests/ (full suite):      109 passed, 1 failed (pre-existing on main), 1 skipped
```

The single failure (`test_cli_batch3.py::TestStartWithPath::test_start_content_pipeline_path` — "database table is locked") reproduces on `main` and is unrelated.

## Verdict

✅ **APPROVE** — Clean implementation, comprehensive tests, no regressions. Ship it.
