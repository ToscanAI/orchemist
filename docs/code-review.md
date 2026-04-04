# Code Review: Orchemist Web UI Frontend

**Branch:** `feature/744-Self-Sufficient-Web-UI-with-Multi-Model-Routing`
**Reviewer:** Toscan (AI)
**Date:** 2026-04-04
**Overall Score: 7.5 / 10**

---

## 1. Summary

This is a well-structured, production-grade Next.js static export frontend for the Orchestration Engine. The codebase demonstrates strong fundamentals: proper TypeScript typing, comprehensive JSDoc documentation, good accessibility practices, and a clean component architecture. The SSE streaming implementation is thoughtfully designed with proper cleanup. However, there are several issues that should be addressed before merge — two security concerns, a few correctness bugs, missing type safety in key areas, and significant test coverage gaps.

---

## 2. Critical Issues — Must Fix Before Merge

### C1. SPA Fallback: Missing Path Traversal Check on `.html` Extension Route

**File:** `src/orchestration_engine/cli.py`, lines 4770–4772
**Severity:** 🔴 Security — Path Traversal

The `.html` extension fallback (step 2) does NOT include the `is_relative_to()` safety check that steps 1 and 3 correctly include:

```python
# Step 1 ✅ — has traversal guard
static_file = frontend_out / full_path
if static_file.is_file() and static_file.resolve().is_relative_to(frontend_out.resolve()):

# Step 2 ❌ — NO traversal guard
html_file = frontend_out / f"{full_path}.html"
if html_file.is_file():
    return FileResponse(str(html_file))  # Path traversal possible!

# Step 3 ✅ — has traversal guard
candidate = frontend_out / '/'.join(parts[:i]) / '_.html'
if candidate.is_file() and candidate.resolve().is_relative_to(frontend_out.resolve()):
```

A crafted path like `../../etc/passwd` would fail (no `.html` extension on that file), but a path like `../../some-dir/some-file` where `some-file.html` exists could be served. While the blast radius is limited (only serves `.html` files), this is an inconsistency that should be fixed.

**Fix:**
```python
html_file = frontend_out / f"{full_path}.html"
if html_file.is_file() and html_file.resolve().is_relative_to(frontend_out.resolve()):
    return FileResponse(str(html_file))
```

### C2. `StartRunRequest` Type Missing `api_key` and `model_map` Fields

**File:** `frontend/lib/types.ts`, lines 57–63
**File:** `frontend/app/templates/[id]/TemplateDetailClient.tsx`, line 164
**Severity:** 🔴 Type Safety / Correctness — `as any` Cast Hides Bug

The `StartRunRequest` interface is missing `api_key` and `model_map` fields, forcing an `as any` cast when launching runs:

```typescript
// types.ts — missing fields
export interface StartRunRequest {
  readonly template: string;
  readonly mode: RunMode;
  readonly input: Record<string, unknown>;
  readonly output_dir?: string;
  readonly gateway_url?: string;
  readonly skip_scoring?: boolean;
  // ❌ Missing: api_key, model_map, executor, issue_number, repo
}

// TemplateDetailClient.tsx:164 — forced to use `as any`
const run = await startRun({
  template: template.id,
  mode: selectedMode,
  input,
  ...(apiKey ? { api_key: apiKey } : {}),
} as any);
```

This also means the `model_map` is being smuggled through `input._model_map` instead of the proper top-level `model_map` field that the backend expects.

**Fix:** Update `StartRunRequest` to include:
```typescript
export interface StartRunRequest {
  readonly template: string;
  readonly mode: RunMode;
  readonly input: Record<string, unknown>;
  readonly output_dir?: string;
  readonly gateway_url?: string;
  readonly skip_scoring?: boolean;
  readonly api_key?: string;
  readonly model_map?: Record<string, string>;
  readonly executor?: string;
}
```

Then update TemplateDetailClient to use `model_map` at the top level instead of `input._model_map`.

### C3. API Key Sent as Plain JSON in Request Body

**File:** `frontend/app/templates/[id]/TemplateDetailClient.tsx`, line 164
**File:** `frontend/lib/api.ts` (startRun function)
**Severity:** 🟡 Security — Credential Handling

The `api_key` (Anthropic or OpenRouter) is sent in the JSON body of the `POST /api/v1/runs` request. While the backend correctly avoids persisting it (per the docstring), it will appear in:
- Browser DevTools Network tab (request body)
- Any HTTP proxy/middleware logs
- Server access logs if request body logging is enabled

The backend handles this reasonably (passes to subprocess env var, never persists), but the frontend should at minimum:
1. Clear the key from state after successful submission
2. Consider noting in the UI that the key is transmitted to the server but not stored

Not a blocker, but worth documenting the trust model.

---

## 3. Important Issues — Should Fix

### I1. `listRuns` Called with `as any` Cast

**File:** `frontend/app/runs/page.tsx`, line 85
**Severity:** 🟡 Type Safety

```typescript
const params: Record<string, unknown> = { limit: PAGE_SIZE, offset };
// ...
const data: RunsListResponse = await listRuns(params as any);
```

The `params` object is typed as `Record<string, unknown>` instead of using the existing `ListRunsParams` interface. This bypasses TypeScript's type checking.

**Fix:** Use `ListRunsParams` directly:
```typescript
const params: ListRunsParams = { limit: PAGE_SIZE, offset };
if (statusFilter !== 'all') params.status = statusFilter as RunStatus;
if (templateFilter.trim()) params.template_id = templateFilter.trim();
const data = await listRuns(params);
```

### I2. `SchemaForm` useEffect Missing `onChange` Dependency + Risk of Stale Closure

**File:** `frontend/components/pipeline/SchemaForm.tsx`, line ~55
**Severity:** 🟡 Correctness — Potential Stale Closure

```typescript
useEffect(() => {
  if (!hasSchema || !properties) return;
  const defaults: Record<string, unknown> = {};
  for (const [key, prop] of Object.entries(properties)) {
    if (prop.default !== undefined) {
      defaults[key] = prop.default;
    }
  }
  setValues(defaults);
  onChange(defaults);  // ← stale if parent re-renders with new onChange
}, [hasSchema]); // eslint-disable-line react-hooks/exhaustive-deps
```

The `onChange` callback is called inside the effect but excluded from the dependency array. If the parent component recreates the `onChange` callback (which `TemplateDetailClient` does — it's an inline arrow), the effect could call a stale reference. In practice this works because the effect only runs once (when `hasSchema` becomes true), but it's fragile.

**Fix:** Either:
- Wrap `onChange` in a ref (`useRef`) inside SchemaForm
- Or memoize `onChange` in the parent with `useCallback`

### I3. Dashboard "Total Runs" Count Is Misleading

**File:** `frontend/app/page.tsx`, lines ~121–125
**Severity:** 🟡 UX / Correctness

```typescript
const [runs, setRuns] = useState<RunRecord[]>([]);
// fetches with limit: 10
listRuns({ limit: 10 }).then((r) => r.items)
// later:
<span>{runs.length > 0 ? `${runs.length}+` : '0'}</span>
```

The dashboard fetches at most 10 runs but displays `{runs.length}+` as "Total Runs". This shows "10+" even when there are exactly 10 runs (or 10,000). The `RunsListResponse` includes a `total` field — use it.

**Fix:**
```typescript
const [totalRuns, setTotalRuns] = useState(0);
// in fetch:
listRuns({ limit: 5 }).then((r) => { setTotalRuns(r.total); return r.items; })
```

### I4. `RunDetailClient` Opens SSE Connection Even for Completed Runs

**File:** `frontend/app/runs/[id]/RunDetailClient.tsx`, `frontend/lib/sse.ts`
**Severity:** 🟡 Performance — Unnecessary Connections

When viewing a historical/completed run, the `useRunEvents` hook unconditionally opens an SSE EventSource connection. The backend will send the terminal `status_changed` event and close, but this is still a wasted HTTP connection and brief resource allocation.

**Fix:** Accept an optional `enabled` parameter in `useRunEvents`:
```typescript
export function useRunEvents(runId: string, enabled = true): UseRunEventsResult {
  // ...
  useEffect(() => {
    if (!enabled) return;
    // ...
  }, [runId, enabled]);
}
```

Then in `RunDetailClient`, disable SSE once `initialRun` shows a terminal status.

### I5. `baseUrl` State Variable Set but Never Used

**File:** `frontend/app/templates/[id]/TemplateDetailClient.tsx`, line 98
**Severity:** 🟡 Dead Code

```typescript
const [baseUrl, setBaseUrl] = useState<string>('');
```

`baseUrl` is set via `ProviderSelector.onBaseUrlChange` but never read or sent to the API. The backend `LaunchRequest` doesn't have a `base_url` field either, so this is dead state.

**Fix:** Remove `baseUrl` state and `onBaseUrlChange` from `ProviderSelector`, or wire it through if the backend will support custom base URLs.

### I6. `PhaseModelMap` Overrides Apply Per-Tier, Not Per-Phase

**File:** `frontend/components/pipeline/PhaseModelMap.tsx`
**Severity:** 🟡 UX / Correctness

The `handleOverride` function keys on `tier` (e.g., "sonnet"), not on `phase.id`. This means overriding the model for one sonnet-tier phase changes ALL sonnet-tier phases to the same model. The UI renders per-phase rows, creating the impression that each phase can have a different override — but they're all linked by tier.

This matches the backend's `model_map` structure (which is tier→model), so it's technically correct, but the UI should make this coupling explicit — e.g., by grouping phases by tier or showing a single dropdown per tier.

### I7. No Error Boundary for Client Components

**File:** Frontend-wide
**Severity:** 🟡 Error Handling

There are no React Error Boundaries in the component tree. If any client component throws during rendering (e.g., unexpected data shape from the API), the entire app crashes with a white screen.

**Fix:** Add an error boundary at the layout level:
```tsx
// app/error.tsx
'use client';
export default function ErrorBoundary({ error, reset }) { ... }
```

---

## 4. Minor Issues — Nice to Fix

### M1. SSE `parseEvent` / `parseRawEvent` Use `as SseEvent` Without Validation

**File:** `frontend/lib/api.ts`, lines ~340–355; `frontend/lib/sse.ts`, lines ~110–125
**Severity:** 🟢 Type Safety

Both `parseEvent` (in api.ts) and `parseRawEvent` (in sse.ts) spread the raw parsed JSON and cast with `as SseEvent`. If the backend sends unexpected fields or missing required fields, the cast silently succeeds and downstream code may crash accessing `null` properties.

**Fix:** Add minimal runtime validation (e.g., check `typeof parsed.run_id === 'string'`) or use a validation library like `zod`.

### M2. Duplicated `BASE_URL` Constant

**File:** `frontend/lib/api.ts`, line 38; `frontend/lib/sse.ts`, line 67
**Severity:** 🟢 Code Quality — DRY Violation

The `BASE_URL` constant (reading `NEXT_PUBLIC_API_BASE_URL`) is defined identically in both files.

**Fix:** Export `BASE_URL` from `api.ts` (or a shared `config.ts`) and import in `sse.ts`.

### M3. Duplicated SSE Parsing Logic

**File:** `frontend/lib/api.ts` (streamRun) and `frontend/lib/sse.ts` (useRunEvents)
**Severity:** 🟢 Code Quality — DRY Violation

Both files contain nearly identical SSE event parsing logic (`parseEvent` / `parseRawEvent`) and EventSource setup code. The `streamRun` function in `api.ts` appears to be unused by the app (the hook in `sse.ts` is used instead).

**Fix:** Remove `streamRun` from `api.ts` if unused, or extract shared parsing into a utility.

### M4. `RunDetailClient` Dynamic `window.location` Fallback for Static Export

**File:** `frontend/app/runs/[id]/RunDetailClient.tsx`, lines 72–76; `frontend/app/templates/[id]/TemplateDetailClient.tsx`, lines 82–86
**Severity:** 🟢 Code Quality

The `rawId` fallback (checking for `_` placeholder and falling back to `window.location`) is duplicated across both dynamic route pages. Consider extracting to a shared hook:

```typescript
function useStaticExportParam(paramName: string): string {
  const params = useParams<Record<string, string>>();
  const rawId = params[paramName] && params[paramName] !== '_' ? params[paramName] : (() => {
    if (typeof window === 'undefined') return '_';
    const segments = window.location.pathname.split('/').filter(Boolean);
    return segments[segments.length - 1] ?? '_';
  })();
  return decodeURIComponent(rawId);
}
```

### M5. `ObserverPanel` Uses String Keys for Observation IDs

**File:** `frontend/components/pipeline/ObserverPanel.tsx`
**Severity:** 🟢 Minor — Nitpick

Using `String(++obsId)` with a local counter is fine for keys but could use `crypto.randomUUID()` for more robust uniqueness in edge cases (not really needed here since the memo is deterministic).

### M6. Magic Number for Health Check Interval

**File:** `frontend/components/TopNav.tsx`, line ~35
**Severity:** 🟢 Style

```typescript
const interval = setInterval(check, 30_000);
```

The 30-second health check interval and the 10-second runs auto-refresh (runs/page.tsx) are inline magic numbers. Consider named constants for discoverability.

### M7. `ProviderSelector` Key Input Not Controlled

**File:** `frontend/components/pipeline/ProviderSelector.tsx`
**Severity:** 🟢 Minor

The API key `<input>` uses `onChange` but no `value` prop — it's an uncontrolled input. This means the parent can't reset/clear the field programmatically.

### M8. `LogViewer` Uses `eslint-disable` Comment Unnecessarily

**File:** `frontend/components/pipeline/LogViewer.tsx`, line ~31
**Severity:** 🟢 Style

```typescript
// eslint-disable-next-line react-hooks/exhaustive-deps
const fetchLogs = useCallback(async () => { ... }, [runId]);
```

The `useCallback` already has `[runId]` as a dependency. The ESLint rule shouldn't fire here unless there's an additional dependency being suppressed. Verify and remove if not needed.

---

## 5. Positive Observations

### ✅ Excellent TypeScript Type Definitions
The `types.ts` file is thorough, well-documented, and mirrors the backend Pydantic models accurately. All fields are `readonly` — a mature pattern that prevents accidental mutation. The discriminated union for SSE events is clean.

### ✅ Strong Documentation (JSDoc)
Every module, function, component, and interface has JSDoc comments explaining purpose, parameters, and usage examples. The quality is unusually high for a frontend codebase.

### ✅ Proper SSE Lifecycle Management
The `useRunEvents` hook correctly:
- Resets state on `runId` change
- Uses a ref for the EventSource
- Handles strict mode double-invocation
- Auto-closes on terminal status
- Cleans up on unmount

### ✅ Security-Conscious API Client
- `encodeURIComponent()` on all path parameters (prevents injection)
- Path traversal checks in SPA fallback (steps 1 and 3)
- API key field uses `type="password"` and `autoComplete="off"`
- No `dangerouslySetInnerHTML` or `innerHTML` anywhere

### ✅ Good Accessibility
- `aria-label`, `aria-live`, `aria-current`, `role` attributes used throughout
- Loading states have proper `role="status"` and screen-reader text
- Form inputs have associated `<label>` elements
- Navigation uses semantic `<nav>` with `aria-label`
- Phase list uses `<ol>` with `aria-label="Phase execution plan"`

### ✅ Clean Error Handling Pattern
Consistent use of `cancelled` flags in effects to prevent state updates after unmount. Error states are surfaced in the UI with proper `role="alert"`.

### ✅ Thoughtful Observer Panel
The `ObserverPanel` is a clever addition — heuristic-based anomaly detection (slow phases, token usage, cost milestones) from SSE events. Read-only, deterministic, and useful.

### ✅ Backend API Design
- Clean REST API with proper HTTP verbs and status codes
- SSE streaming with named event types (not generic `message`)
- Path traversal protection in SPA fallback
- API key never persisted to database

---

## 6. Test Coverage Assessment

**Current:** 6 test files, ~156 tests across:
- `api.test.ts` (678 lines) — API client functions ✅
- `sse.test.ts` (464 lines) — SSE hook ✅
- `TemplateDetailPage.test.tsx` (589 lines) — Template detail page ✅
- `Badge.test.tsx` — Badge component ✅
- `Button.test.tsx` — Button component ✅
- `PhaseList.test.tsx` — PhaseList component ✅

### Critical Gaps:
| Missing Test | Risk |
|---|---|
| `RunDetailClient` — no tests at all | SSE rendering, resume/cancel, initial state, paused banner untested |
| `RunsPage` — no tests | Pagination, filtering, auto-refresh untested |
| `DashboardPage` — no tests | API failure handling, stats rendering untested |
| `TopNav` — no tests | Health indicator, active link detection untested |
| `SchemaForm` — no tests | Schema-driven form rendering, JSON fallback, validation untested |
| `ProviderSelector` — no tests | Mode-conditional rendering untested |
| `PhaseModelMap` — no tests | Override behavior untested |
| `ObserverPanel` — no tests | Heuristic rules, severity mapping untested |
| `LogViewer` — no tests | Truncation logic, error states untested |

The tested components are well-tested (Badge, Button, PhaseList have thorough variant/accessibility coverage). But the most complex and bug-prone components — `RunDetailClient`, `SchemaForm`, and the runs list — have zero tests.

**Recommendation:** Prioritize tests for:
1. `RunDetailClient` — highest complexity, SSE + REST + state machine
2. `SchemaForm` — user-facing form with multiple input types
3. `RunsPage` — pagination arithmetic and filter interactions

---

## 7. Recommendations — Architectural Suggestions

### R1. Extract Shared Hooks
`useStaticExportParam`, `useAutoRefresh`, and `useApiCall` (loading/error/data pattern repeated in every page) would reduce boilerplate by ~30%.

### R2. Consider React Query / SWR for Data Fetching
Every page manually implements loading/error/data states with `useState` + `useEffect`. A library like `@tanstack/react-query` would provide:
- Automatic caching and deduplication
- Background refetching
- Error retry with exponential backoff
- Optimistic updates

This would eliminate ~50% of the state management code.

### R3. Add a `<StatusIndicator>` Compound Component
Status badge rendering logic is duplicated across Dashboard (`statusVariant`), `RunStatusBadge`, and various inline conditionals. Consolidate into a single component with consistent mapping.

### R4. Type the `as any` Casts Away
There are exactly 2 `as any` casts in the app code (TemplateDetailClient:164 and runs/page.tsx:85). Both are caused by incomplete `StartRunRequest` / `ListRunsParams` types. Fix the types and both casts disappear.

### R5. Add `output: 'export'` Dynamic Route Testing
The `window.location` fallback for static export dynamic routes is clever but fragile. Add integration tests that verify the fallback works correctly when `useParams` returns `{ id: '_' }`.

### R6. Consider WebSocket Upgrade Path
EventSource (SSE) is unidirectional and doesn't support custom headers (no auth). For future features like real-time collaboration or authenticated streams, consider a WebSocket upgrade path. The current SSE architecture is clean enough that this would be a straightforward migration.

---

## Appendix: Files Reviewed

| File | Lines | Status |
|---|---|---|
| `frontend/lib/types.ts` | 233 | ✅ Reviewed |
| `frontend/lib/api.ts` | 382 | ✅ Reviewed |
| `frontend/lib/sse.ts` | 184 | ✅ Reviewed |
| `frontend/app/page.tsx` | 161 | ✅ Reviewed |
| `frontend/app/layout.tsx` | 41 | ✅ Reviewed |
| `frontend/app/templates/page.tsx` | ~120 | ✅ Reviewed |
| `frontend/app/templates/[id]/TemplateDetailClient.tsx` | ~300 | ✅ Reviewed |
| `frontend/app/runs/page.tsx` | ~200 | ✅ Reviewed |
| `frontend/app/runs/[id]/RunDetailClient.tsx` | ~350 | ✅ Reviewed |
| `frontend/components/TopNav.tsx` | ~80 | ✅ Reviewed |
| `frontend/components/pipeline/SchemaForm.tsx` | ~200 | ✅ Reviewed |
| `frontend/components/pipeline/ProviderSelector.tsx` | ~60 | ✅ Reviewed |
| `frontend/components/pipeline/PhaseModelMap.tsx` | ~100 | ✅ Reviewed |
| `frontend/components/pipeline/ObserverPanel.tsx` | ~175 | ✅ Reviewed |
| `frontend/components/pipeline/LogViewer.tsx` | ~100 | ✅ Reviewed |
| `src/orchestration_engine/cli.py` (SPA fallback) | ~50 lines diff | ✅ Reviewed |
| `src/orchestration_engine/web/api.py` | ~3000 | ✅ Reviewed (launch endpoint focus) |
| `src/orchestration_engine/executors/openrouter_executor.py` | ~100 | ✅ Reviewed |
| `frontend/__tests__/` (6 test files) | ~2400 | ✅ Reviewed |

---

*Review complete. 3 critical, 7 important, 8 minor issues identified. Strong foundation — the critical items are straightforward fixes.*
