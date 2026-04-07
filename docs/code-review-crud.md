# Code Review: Template CRUD (Commit `2cc87ed`)

**Issue:** #770 — Template CRUD (create, edit, delete, duplicate) from Web UI  
**Branch:** `feature/744-Self-Sufficient-Web-UI-with-Multi-Model-Routing`  
**Reviewer:** Opus 4.6 (automated review)  
**Date:** 2026-04-04  
**Files:** 12 changed, +1226 −19  

---

## Overall Score: 7/10

Solid implementation with good test coverage and sensible architecture choices. The code follows existing patterns, has proper validation, and handles the bundled-vs-user distinction well. However, there are two security issues (one critical), a consistency bug, and several quality improvements needed.

---

## Critical Issues (Must Fix Before Merge)

### C1. `_resolve_template` Path Traversal — Arbitrary File Read via `yaml_content`

**File:** `src/orchestration_engine/web/api.py`, line ~595  
**Severity:** CRITICAL  
**Pre-existing?** The `_resolve_template` function is pre-existing, but this commit **expands its attack surface** by adding `yaml_content` (raw file read) and the duplicate endpoint (raw file read + YAML manipulation) on top of it.

The `_resolve_template` function accepts bare file paths when the input looks path-like (contains `/`, `.yaml`, `.yml`):

```python
if looks_like_path:
    p = Path(name_or_path)
    if not p.exists():
        raise HTTPException(status_code=404, ...)
    return p  # ← No sandboxing check!
```

While Starlette's `{name}` path parameter normally rejects slashes, a name like `..%2F..%2Fetc%2Fpasswd.yaml` or a template ID containing path separators could potentially bypass this. More importantly, this function is used by:

- **GET `/api/v1/templates/{name}`** — now returns `yaml_content` (raw file contents)
- **POST `/api/v1/templates/{name}/duplicate`** — reads and parses the file
- **PUT `/api/v1/templates/{name}`** — overwrites the resolved file
- **DELETE `/api/v1/templates/{name}`** — deletes the resolved file

**Before this commit**, the function only returned parsed template data (structured output). **After this commit**, it returns raw file contents (`yaml_content`), making it a file-read oracle.

**Fix:** Add a sandboxing check in `_resolve_template` for the `looks_like_path` branch — verify the resolved path is inside one of the engine's search paths:

```python
if looks_like_path:
    p = Path(name_or_path)
    if not p.exists():
        raise HTTPException(status_code=404, ...)
    # Sandbox check
    engine = _make_engine()
    resolved = p.resolve()
    allowed = any(
        resolved.is_relative_to(d.resolve()) 
        for d, _ in engine.get_search_paths()
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Path outside template directories")
    return p
```

### C2. `get_template_api` Uses `TemplateEngine()` Instead of `_make_engine()`

**File:** `src/orchestration_engine/web/api.py`, line ~877  
**Severity:** HIGH (causes incorrect behavior in test and custom deployments)

```python
engine = TemplateEngine()  # ← Should be _make_engine()
```

This is **pre-existing** but now impacts the new `source` and `yaml_content` fields — when `_user_templates_dir` is configured (e.g., in tests), the `source` detection will use wrong paths, potentially misclassifying user templates as "unknown" and showing wrong `yaml_content`.

The test fixture patches `TemplateEngine.__init__` to work around this, which masks the bug.

**Fix:** Replace `TemplateEngine()` with `_make_engine()` in `get_template_api`.

---

## Important Issues (Should Fix)

### I1. Duplicate-of-Duplicate Accumulates "(Copy)" in Name

**File:** `src/orchestration_engine/web/api.py`, line ~1316  

```python
raw["name"] = f"{raw.get('name', template.name)} (Copy)"
```

When duplicating a copy, the name becomes `"My Template (Copy) (Copy)"`, then `"My Template (Copy) (Copy) (Copy)"`, etc.

**Fix:** Strip existing `(Copy)` suffix before appending:

```python
base_name = raw.get('name', template.name)
base_name = re.sub(r'\s*\(Copy\)$', '', base_name)
raw["name"] = f"{base_name} (Copy)"
```

### I2. Duplicate Race Condition — TOCTOU on `existing_ids`

**File:** `src/orchestration_engine/web/api.py`, lines ~1307–1312

The duplicate ID generation reads `existing_ids` from `engine.list_templates()`, then writes the file. Two concurrent duplicate requests for the same template could both see the same `existing_ids`, generate the same candidate ID, and one would overwrite the other's file.

**Mitigation:** Low risk (local single-user tool), but for correctness, use `O_EXCL` / `open(..., 'x')` mode for the write, and retry with an incremented counter on `FileExistsError`.

### I3. `source` Field Missing from `TemplateSummary` TypeScript Interface

**File:** `frontend/lib/types.ts`

The backend's list endpoint returns `source` in each template summary, but `TemplateSummary` doesn't declare it. `TemplateCard.tsx` works around this with an unsafe type assertion:

```typescript
const isBundled = (template as TemplateSummary & { source?: string }).source === 'bundled';
```

**Fix:** Add `readonly source?: string;` to `TemplateSummary`.

### I4. `deleteTemplate` Imported but Unused in `templates/page.tsx`

**File:** `frontend/app/templates/page.tsx`, line 11

```typescript
import { listTemplates, deleteTemplate, ApiError } from '@/lib/api';
```

`deleteTemplate` is imported but never used in this file. This will trigger lint warnings.

**Fix:** Remove the unused import.

### I5. Update Endpoint Doesn't Verify ID Consistency

**File:** `src/orchestration_engine/web/api.py`, PUT endpoint (~line 1215)

The update endpoint writes `req.content` to `existing_path`, but if the user changes the `id` field inside the YAML, the file stem no longer matches the template ID. This creates a ghost template — the file says it's `new-id` but lives at `old-id.yaml`.

**Fix:** After parsing, verify `template.id` matches the URL `name` parameter, or rename the file accordingly.

---

## Minor Issues (Nice to Have)

### M1. Duplicate Response Duplicates Phase Serialization Logic

**File:** `src/orchestration_engine/web/api.py`, lines ~1337–1356

The phase serialization dict comprehension is copy-pasted between `get_template_api`, `duplicate_template_api`, and potentially other places. Should be extracted into a helper function.

### M2. `EditTemplateClient` Error Parsing is Overly Complex

**File:** `frontend/app/templates/[id]/edit/EditTemplateClient.tsx`, lines ~85–105

The error extraction logic has 5 levels of nested type checks (`detail.detail.errors`, `detail.detail.message`, etc.). This same pattern is duplicated in `new/page.tsx`. Should be extracted into a shared `extractApiError(err: unknown): string` utility.

### M3. No Client-Side YAML Validation Before Submit

Both the create and edit pages submit raw YAML without any client-side validation. A simple `js-yaml` parse check before submission would give instant feedback instead of a server round-trip.

### M4. Default Template YAML Has Hardcoded Values

**File:** `frontend/app/templates/new/page.tsx`, lines ~18–31

The default template YAML starts with `id: my-new-template`. If a user creates two templates without changing the ID, the second will fail with a 409 conflict. Consider generating a unique default ID (e.g., with a timestamp).

### M5. Delete Modal Shares `apiError` State with Other Actions

**File:** `frontend/app/templates/[id]/TemplateDetailClient.tsx`

The delete modal and the duplicate button share the same `apiError` state. If duplication fails, opening the delete modal still shows the duplication error. Each action should have its own error state, or errors should be cleared more carefully.

### M6. `get_template_api` Missing `_make_engine` Has Inconsistent Behavior

Already covered in C2, but worth noting that this inconsistency means the `source` field could return `"unknown"` for user templates when `_user_templates_dir` is set, even though the template was correctly loaded.

---

## Verified: Known Bug Fix

✅ **Line 4779 in `cli.py`:** `trial = [*parts]` — confirmed fixed. The original `trial = list(parts)` would have called Click's `list` command (defined at line 487) instead of Python's builtin `list()`.

### Other Shadowed Builtins Check

Scanned the SPA fallback code (lines 4750–4800) for uses of Python builtins that are shadowed by Click commands:

- `list` — **was** shadowed (line 487), **fixed** to `[*parts]`
- `format` — shadowed by `format_datetime` and `format_duration` at module level (not builtins, no conflict)
- `type` — used as Click option name `'task_type'` (aliased, not shadowing the builtin)
- `id`, `dict`, `input`, `hash`, `set`, `open` — **not used** in the SPA fallback block

**No other shadowed builtin issues found in the SPA fallback code.**

---

## Test Coverage Assessment

**Strengths:**
- 17 tests covering the duplicate endpoint, detail fields, and full CRUD lifecycle
- Good isolation via `tmp_path` and monkeypatching
- Tests for 404 on nonexistent template duplication
- Tests for bundled template duplication
- Round-trip test (create → read → update → duplicate → delete)

**Gaps:**
- No test for duplicating-a-duplicate (accumulating "(Copy)")
- No test for concurrent duplicate requests (race condition)
- No test for update with changed ID in YAML
- No test for path traversal attempts via template name
- No test for the SPA fallback routing logic in `cli.py`
- Frontend: no unit tests for the new pages (EditTemplateClient, CreateTemplatePage)

---

## Architecture Notes

- Good separation: backend CRUD → frontend consumption via typed API client
- `generateStaticParams` with `{ id: '_' }` is the correct pattern for Next.js static export with dynamic routes
- SPA fallback in cli.py correctly handles the new `/templates/{id}/edit` route via segment substitution
- Path traversal protection in `_writable_template_path` is solid (regex + resolve check)
- Delete/update correctly restricted to `user` source only

---

## Verdict: **FIX**

Fix C1 (path traversal sandbox) and C2 (`_make_engine` consistency) before merge. I1–I5 should be addressed in this PR or a fast-follow. The implementation is architecturally sound and the test suite is good — these fixes are straightforward.
