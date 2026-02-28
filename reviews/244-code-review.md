# Code Review: #244 — Structured HTTP Error Classification

**Branch:** `fix/244-structured-http-errors`
**Reviewer:** Opus (automated)
**Date:** 2026-02-28

## Verdict: REQUEST_CHANGES

## Issues Found

### 🔴 Critical — Broken `RuntimeError` catch in polling loop

**File:** `src/orchestration_engine/openclaw_executor.py:582`

The polling loop in `_poll_session_result` catches `RuntimeError` from `_invoke_tool`:

```python
except RuntimeError as exc:
    # Session may not be ready yet
    logger.debug(f"Poll error (may be transient): {exc}")
    continue
```

`_invoke_tool` calls `_http_post`, which **previously** raised `RuntimeError` but **now** raises `GatewayHTTPError` (which extends `OrchestratorError(Exception)`, not `RuntimeError`). This means transient HTTP errors (502, 503, 504) during session polling will **no longer be caught**, causing the entire `execute()` call to crash instead of retrying gracefully.

**Fix:** Change line 582 to catch both:
```python
except (RuntimeError, GatewayHTTPError) as exc:
```
Or better, catch `(RuntimeError, OrchestratorError)` to future-proof. Also update the import at the top to include `GatewayHTTPError` (or `OrchestratorError`).

**Note:** `_invoke_tool` itself still raises `RuntimeError` on line 428 for logical failures (`ok: false`), so you must keep catching `RuntimeError` too — don't just replace it.

### 🟡 Medium — Docstring still says "Raises RuntimeError"

**File:** `src/orchestration_engine/openclaw_executor.py:419`

`_invoke_tool` docstring says "Raises RuntimeError on failure" but it now also raises `GatewayHTTPError` subclasses from `_http_post`. Update the docstring.

Similarly, `_spawn_session` docstring at line 464 says "RuntimeError: On HTTP errors" — should mention `GatewayHTTPError`.

### 🟡 Medium — `retry_after` doesn't handle HTTP-date format

**File:** `src/orchestration_engine/errors.py:125-128`

Per RFC 9110, `Retry-After` can be either seconds (integer) or an HTTP-date (e.g., `Fri, 31 Dec 1999 23:59:59 GMT`). The current code only handles integer values and silently drops dates. This is acceptable for now (most APIs send integers), but worth a `# TODO` comment.

### 🟢 Low — `_invoke_tool` still raises bare `RuntimeError`

**File:** `src/orchestration_engine/openclaw_executor.py:428`

Consider introducing a `ToolInvocationError(OrchestratorError)` for consistency with the new hierarchy. Not blocking, but would clean up the exception model. Could be a follow-up issue.

## What's Good

- Clean exception hierarchy with `OrchestratorError` as the root
- `classify_http_error()` factory is well-designed — single dispatch point
- `is_retryable` as a property on the base class is elegant
- Constructor guards on `AuthenticationError` and `GatewayUnavailableError` prevent misuse
- Test coverage is thorough (59 tests, all passing)
- `retry_after` safely handles missing/invalid headers

## Required Before Merge

1. **Fix the `except RuntimeError` at line 582** to also catch `GatewayHTTPError` (or `OrchestratorError`)
2. **Update docstrings** in `_invoke_tool` and `_spawn_session`

## Tests

- `tests/test_http_errors.py`: **59/59 passed** ✅
- `tests/` regression: 1 pre-existing failure in `test_cli_batch1.py` (unrelated to this PR)
