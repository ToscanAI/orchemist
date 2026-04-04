# Orchemist Web UI — Frontend Review

**Date:** 2026-04-04
**Reviewer:** Toscan (automated sub-agent review)
**Scope:** All pages, components, lib modules, and design tokens in `frontend/`

---

## 1. Executive Summary

The Orchemist Web UI is a well-structured Next.js 14 static export app with a clean dark-theme design system, good TypeScript typing, and solid SSE integration for live run tracking. The codebase is readable, well-documented (JSDoc on every module), and follows consistent patterns.

**Key strengths:**
- Clean design token system (CSS vars + Tailwind extension) in `globals.css` and `tailwind.config.ts`
- Well-typed API client with `ApiError` class and proper error narrowing
- SSE hook (`useRunEvents`) is cleanly implemented with proper cleanup
- Consistent loading/error/empty state handling across pages
- Good accessibility basics (aria-labels, role attributes, aria-live regions)

**Key weaknesses:**
- **Gate management is completely missing from the UI** despite full API support
- **Template CRUD (create/update/delete/validate)** — API exists, UI is read-only
- **Mixed design token usage** — some components use Tailwind zinc-* classes directly, others use the design token system (surface-*, content-*), creating visual inconsistency risk
- **Navigation doesn't indicate active page**
- **No auto-refresh on run detail page** — SSE handles live events but initial state (existing completed phases) is never fetched via `getRun()`
- **`scoring_failed` status** missing from runs page filter options

**Overall assessment:** Solid MVP foundation. The main gap is that ~40% of backend API capability is unexposed. The design system has a split-personality issue between raw Tailwind colors and semantic tokens that will cause drift over time.

---

## 2. Per-Page Findings

### 2.1 Dashboard (`app/page.tsx`)

**Data Flow:**
- Fetches `listTemplates()` and `listRuns({ limit: 10 })` in parallel — good.
- `listRuns` failure is silently caught (line 30: `.catch(() => [] as RunRecord[])`) — the user sees "0" runs with no indication that runs failed to load while templates succeeded.

**Issues:**

1. **"Total Runs" stat is misleading** (lines 71-79): Shows `runs.length` (capped at 10 by the `limit: 10` param) with a `+` suffix. The `RunsListResponse` returns `total` but that field is never used. The stat card should show `total` from the API response, not `items.length`.

2. **No auto-refresh**: Dashboard shows stale data after initial load. Active runs count won't update without manual refresh. The runs page has a 10s polling interval — dashboard should too, or at least have a refresh button.

3. **Empty state for Quick Launch**: If there are 0 templates but runs exist, the Quick Launch section silently renders nothing (no empty state message).

4. **`ApiError` import is unused** (line 5): Imported but never referenced — the error handling uses `instanceof Error` instead.

5. **Back link from template detail goes to dashboard, not templates list** — discussed under navigation.

**Suggestions:**
- Store `RunsListResponse.total` and display it in the "Total Runs" card.
- Add a "Last updated X seconds ago" indicator + refresh button.
- Add a health check indicator (the API has `getHealth()` — unused anywhere in the UI).

### 2.2 Templates List (`app/templates/page.tsx`)

**Data Flow:** Clean — single `listTemplates()` fetch with proper cancel flag.

**Issues:**

1. **No category filter**: Search covers name, category, and description as a single text search. Category could be a dedicated dropdown filter since it's a discrete field on every template.

2. **No sort options**: Templates render in API-returned order. Users can't sort by name, phase count, or category.

3. **Search input has no `aria-label`** (line 32): The `placeholder` serves as a visual label but screen readers need an explicit label.

4. **Error state says "Is orch serve running?"** (line 54) — good contextual hint, but could also offer a retry button like `LogViewer` does.

5. **No indication of template count**: Unlike the runs page which shows "{total} total" in the header, the templates page shows no count.

**Suggestions:**
- Add `aria-label="Search templates"` to the search input.
- Add template count badge next to heading: "Templates (12)".
- Add category filter dropdown.
- Add a "Create Template" button (API supports `createTemplate()`).

### 2.3 Template Detail (`app/templates/[id]/TemplateDetailClient.tsx`)

**Data Flow:**
- Uses `useParams` with `window.location.pathname` fallback for static export — correct pattern.
- Fetches `getTemplate(id)` on mount.
- Launch form calls `startRun()` and navigates to run detail on success.

**Issues:**

1. **`apiKey` and `baseUrl` state are collected but never sent to the API** (lines 65-66, 107-110): `ProviderSelector` captures these values via `onApiKeyChange` and `onBaseUrlChange`, but `handleSubmit` only conditionally includes `api_key` (line 108: `...(apiKey ? { api_key: apiKey } : {})`). The `baseUrl` is never used at all. Furthermore, `api_key` is not in the `StartRunRequest` type — this is cast via `as any` (line 109), which hides the type mismatch.

2. **`_model_map` is stuffed into `input`** (line 105): The model map is added to the `input` object as `input._model_map`. This is a convention leak — the backend should have a proper field for this in `StartRunRequest`, or this should be documented. As-is, the model map gets mixed with user input data.

3. **No form validation before submit**: Required fields from the schema are marked with `*` but no client-side validation prevents submission with empty required fields. The `noValidate` attribute on the form (line 140) explicitly disables browser validation.

4. **Back link goes to dashboard (`/`), not templates list (`/templates`)** (lines 131, 160): After browsing templates, the "← Back to dashboard" link breaks the expected navigation flow. Should be "← Back to templates" linking to `/templates`.

5. **PhaseModelMap only shown for standalone/openrouter** (line 177): This is intentional (openclaw/dry-run don't need model overrides), but there's no explanation to the user about why model assignment isn't available in other modes.

6. **No template delete/edit UI**: The API supports `updateTemplate()` and `deleteTemplate()` but the detail page is read-only.

7. **`config_schema` could be empty object `{}`**: The `SchemaForm` handles this correctly (falls back to JSON textarea), but the label "Input (JSON)" is generic — could say "Pipeline Input" or use the template name.

**Suggestions:**
- Fix back link to go to `/templates`.
- Add client-side required field validation.
- Either properly type `api_key` and `base_url` in `StartRunRequest` or remove the dead state.
- Add edit/delete actions for user-owned templates (with confirmation dialog for delete).

### 2.4 Runs List (`app/runs/page.tsx`)

**Data Flow:**
- `fetchRuns` callback with `useEffect` dependency on `offset`, `statusFilter`, `templateFilter`.
- 10s auto-refresh interval — good for live monitoring.
- Offset resets to 0 when filters change — correct.

**Issues:**

1. **`scoring_failed` missing from filter options** (line 21): `STATUS_OPTIONS` lists `crashed` but omits `scoring_failed`, which is a valid `RunStatus` in the type system (line 67 of types.ts).

2. **Table row `<tr>` has `cursor-pointer` but is not clickable** (line 103): The `<tr>` has `cursor-pointer` and `hover:bg-zinc-900/50` styling, but only the Run ID `<td>` contains a `<Link>`. Clicking on other cells does nothing — misleading affordance. Either make the entire row clickable or remove the pointer cursor.

3. **No loading indicator during refresh**: `loading` is only shown when `runs.length === 0` (line 86). Subsequent refreshes (including the 10s auto-refresh) show no visual feedback. A subtle spinner or "Refreshing..." indicator would help.

4. **`templateFilter` is free-text**: Users must know exact template IDs. A dropdown populated from `listTemplates()` would be more user-friendly.

5. **`params` cast as `any`** (line 73): `listRuns(params as any)` bypasses type checking. The `params` object should be properly typed as `ListRunsParams`.

6. **No "scoring_failed" in status filter but it exists as a RunStatus** — duplicate of point 1, confirming it's a real gap.

7. **Date formatting uses `undefined` locale** (line 45): `d.toLocaleString(undefined, ...)` uses the browser's default locale, which is fine but could be inconsistent across users. Minor.

**Suggestions:**
- Add `scoring_failed` to `STATUS_OPTIONS`.
- Make entire table row clickable (wrap row in Link or use `onClick` with `router.push`).
- Add a subtle loading indicator for background refreshes.
- Replace free-text template filter with a dropdown.

### 2.5 Run Detail (`app/runs/[id]/RunDetailClient.tsx`)

**Data Flow:**
- Subscribes to SSE stream via `useRunEvents(runId)`.
- Derives all display data from accumulated SSE events in a `useMemo`.
- Resume/cancel actions call API and update local state.

**Issues:**

1. **No initial state fetch — SSE-only data** (critical): The page relies entirely on SSE events for its data. If the user navigates to a completed run, they get "Waiting for phase events…" forever because the SSE stream for a completed run likely sends a terminal `status_changed` event but no historical `phase_started`/`phase_completed` events. The page should call `getRun(runId)` on mount to populate initial state (completed phases, status, timestamps).

2. **Back link goes to dashboard (`/`), not runs list (`/runs`)** (line 172): Same issue as template detail. Should be "← Back to runs".

3. **`border-surface-3` and `bg-surface-2` classes in phase spinner** (line 236): These use the design token system, but the rest of the page uses raw Tailwind (`border-zinc-800`, `bg-zinc-900`). Inconsistent — see Cross-Cutting Issues.

4. **`text-content-primary` in phase spinner** (line 248): Same design token inconsistency.

5. **LogViewer is a one-shot fetch with manual refresh**: For running pipelines, logs should auto-refresh (perhaps on a 5s interval while the run is active). Currently the user must click "Refresh" repeatedly.

6. **ObserverPanel position is unusual**: It's placed immediately after the heading, before the tab bar. This means it's always visible regardless of which tab is active, which is intentional but may confuse users about what context it relates to.

7. **Tab state not in URL**: Switching between "Timeline" and "Logs" tabs doesn't update the URL hash/query. Sharing a run URL always lands on "Timeline". Minor but would improve shareability.

8. **`PhaseEventRow` uses `tokens_consumed` as `tokensIn`, `tokensOut` is always null** (line 168): The SSE `phase_completed` event has `tokens_in` and `tokens_out` fields (defined in types.ts lines 71-72), but `RunDetailClient` passes `tokens_consumed` as `tokensIn` and hardcodes `tokensOut: null`. The enriched fields are available but unused.

9. **No "Re-run" or "Clone" action**: After a completed/failed run, there's no way to re-launch with the same parameters. Users must navigate back to the template and fill in the form again.

**Suggestions:**
- **Critical:** Add `getRun(runId)` fetch on mount to populate initial state for non-live runs.
- Fix back link to `/runs`.
- Use `tokens_in` and `tokens_out` from SSE events instead of `tokens_consumed`.
- Add auto-refresh to LogViewer while run is active.
- Add "Re-run" button that navigates to template detail with pre-filled input.

---

## 3. Cross-Cutting Issues

### 3.1 Design Token Split Personality

The codebase has two styling vocabularies used interchangeably:

**Semantic tokens** (defined in `tailwind.config.ts` + `globals.css`):
- `bg-surface-1`, `bg-surface-2`, `text-content-primary`, `border-surface-3`
- Used in: `Button.tsx`, `PhaseEventRow.tsx`, `globals.css` focus ring

**Raw Tailwind zinc-*** classes:
- `bg-zinc-900`, `text-zinc-100`, `border-zinc-800`, `text-zinc-400`
- Used in: `layout.tsx`, `page.tsx` (dashboard), `templates/page.tsx`, `runs/page.tsx`, `RunDetailClient.tsx`, `ProviderSelector.tsx`, `SchemaForm.tsx`, `PhaseModelMap.tsx`, `LogViewer.tsx`, `ObserverPanel.tsx`

**Impact:** Right now the values map to the same colors (`surface-1` = `zinc-900`, etc.), but if you ever want to theme, change the palette, or add a light mode, every raw `zinc-*` usage must be found and updated manually. The design token system was set up correctly — it's just not used consistently.

**Recommendation:** Migrate all color references to semantic tokens. This is the highest-leverage refactor for maintainability.

### 3.2 Navigation Doesn't Show Active Page

`TopNav` in `layout.tsx` (lines 25-50) renders three links (Dashboard, Runs, Templates) with the `nav-item` class. There's a `nav-item-active` class defined in `globals.css` but it's never applied. The user has no visual indication of which page they're on.

**Fix:** Use `usePathname()` from `next/navigation` to conditionally apply `nav-item-active`. Note: this requires making `TopNav` a client component or extracting it.

### 3.3 Back Links Are Inconsistent

| Page | Back link text | Links to | Should link to |
|---|---|---|---|
| Template Detail | "← Back to dashboard" | `/` | `/templates` |
| Run Detail | "← Back to dashboard" | `/` | `/runs` |

Both should link to their parent list page, not the dashboard.

### 3.4 Error Handling Patterns Are Inconsistent

Three different error display patterns exist:

1. **Dashboard/Templates list:** `<div className="card border-red-500/50 bg-red-900/10" role="status">` — uses `role="status"` (wrong — should be `role="alert"` for errors)
2. **Template detail:** `role="alert"` — correct
3. **Runs list:** `<div className="mb-4 rounded-lg border border-red-500/20 bg-red-500/10 p-4">` — no role attribute at all

**Recommendation:** Extract a shared `ErrorBanner` component with consistent styling and `role="alert"`.

### 3.5 Loading Spinner Duplication

The same SVG spinner markup is copy-pasted across 4 files:
- `app/page.tsx` (dashboard)
- `app/templates/page.tsx`
- `app/templates/[id]/TemplateDetailClient.tsx`
- `app/runs/[id]/RunDetailClient.tsx`

**Recommendation:** Extract a `<Spinner />` component (like the one inside `Button.tsx` but standalone).

### 3.6 No Global Error Boundary

There's no React error boundary. If a component throws during render (e.g., unexpected API response shape), the entire app crashes with a white screen. A root-level error boundary in `layout.tsx` or a `error.tsx` file would catch this.

### 3.7 No Keyboard Shortcut Support

No keyboard shortcuts for common actions (e.g., `/` to focus search, `Esc` to close panels, `R` to refresh). Low priority but notable for a power-user tool.

### 3.8 `<Link>` vs `<a>` Inconsistency

- `layout.tsx` TopNav uses raw `<a href="...">` tags (lines 37-47) — these cause full page reloads.
- All other navigation uses `next/link` `<Link>` — SPA navigation.

**Fix:** Replace `<a>` tags in TopNav with `<Link>` from `next/link`.

### 3.9 Unused SSE Module

`lib/sse.ts` is used only by `RunDetailClient.tsx`. Meanwhile, `lib/api.ts` also exports a `streamRun()` function (lines 227-278) that provides an imperative SSE API. These are two different SSE interfaces for the same endpoint — one hook-based, one callback-based. The `api.ts` `streamRun()` appears unused in any component.

**Recommendation:** Remove `streamRun()` from `api.ts` if `useRunEvents()` in `sse.ts` is the canonical approach. Or keep both but document the intended use case for each.

---

## 4. Backend Features Not Exposed

| API Capability | Endpoint | UI Status |
|---|---|---|
| **Gate management** — list, get, approve, reject | `GET/POST /api/v1/gates/*` | ❌ **Completely missing** — no page, no component, no mention |
| **Template creation** | `POST /api/v1/templates` | ❌ No create UI |
| **Template editing** | `PUT /api/v1/templates/{name}` | ❌ No edit UI |
| **Template deletion** | `DELETE /api/v1/templates/{name}` | ❌ No delete UI |
| **Template validation** | `POST /api/v1/templates/validate` | ❌ No validation UI (could be live validation in create/edit form) |
| **Health check** | `GET /api/v1/health` | ❌ No health indicator anywhere |
| **Run resume** | `POST /api/v1/runs/{id}/resume` | ✅ Exposed in RunDetailClient (paused banner) |
| **Run cancel** | `DELETE /api/v1/runs/{id}` | ✅ Exposed in RunDetailClient |
| **Run logs** | `GET /api/v1/runs/{id}/logs` | ✅ Exposed via LogViewer |
| **SSE streaming** | `GET /api/v1/runs/{id}/stream` | ✅ Exposed via useRunEvents |
| **Run initial state** | `GET /api/v1/runs/{id}` | ⚠️ **Defined in api.ts but never called** — RunDetailClient relies solely on SSE |
| **`skip_scoring` option** | `StartRunRequest.skip_scoring` | ❌ No checkbox in launch form |
| **`output_dir` option** | `StartRunRequest.output_dir` | ❌ No field in launch form |
| **`gateway_url` option** | `StartRunRequest.gateway_url` | ❌ No field in launch form |
| **Scoring status/score** | `RunRecord.scoring_status/scoring_score` | ❌ Not displayed on run detail or runs list |

**Biggest gap:** Gate management is a complete feature with list/detail/approve/reject endpoints and it has zero UI surface. This likely represents merge gates for pipeline approval workflows — a critical operational feature.

---

## 5. Priority Recommendations

Ranked by impact (user-facing value × effort ratio):

### P0 — Critical

1. **Fetch initial run state on mount** (`RunDetailClient.tsx`)
   - Call `getRun(runId)` on mount to populate completed phases, status, and metadata for historical runs.
   - Without this, navigating to any completed run shows an empty timeline.
   - **Effort:** Small — add a `useEffect` with `getRun()`, merge result with SSE events.

### P1 — High Impact

2. **Add Gates page and management UI**
   - New `/gates` page with list, status filters, approve/reject actions.
   - Gate detail view showing scoring status, commits, branch info.
   - **Effort:** Medium — new page + components, API client already complete.

3. **Fix navigation: active page indicator + correct back links**
   - Apply `nav-item-active` class based on current path.
   - Fix back links: template detail → `/templates`, run detail → `/runs`.
   - Replace `<a>` tags with `<Link>` in TopNav.
   - **Effort:** Small.

4. **Unify design token usage**
   - Replace all raw `zinc-*`, `sky-*`, `red-*`, `amber-*` color classes with semantic tokens.
   - Establishes consistency and enables future theming.
   - **Effort:** Medium — mechanical but touches every file.

5. **Add health indicator**
   - Call `getHealth()` on app mount, show a status dot in the nav bar.
   - Shows version number and whether the backend is reachable.
   - If backend is down, show a persistent banner instead of per-page errors.
   - **Effort:** Small.

### P2 — Medium Impact

6. **Add template CRUD UI**
   - "Create Template" button on templates list → YAML editor with live validation.
   - Edit/Delete actions on template detail page (for user-owned templates).
   - **Effort:** Medium-large.

7. **Make runs table rows fully clickable**
   - Wrap entire `<tr>` in navigation logic instead of just the Run ID cell.
   - **Effort:** Small.

8. **Add `scoring_failed` to runs filter + display scoring data**
   - Add missing status to filter dropdown.
   - Show `scoring_status` and `scoring_score` on run detail summary.
   - **Effort:** Small.

9. **Extract shared components (ErrorBanner, Spinner, EmptyState)**
   - Reduce code duplication across pages.
   - Ensure consistent error/loading/empty treatment.
   - **Effort:** Small.

10. **Auto-refresh LogViewer while run is active**
    - Poll logs every 5s when run status is `running` or `pending`.
    - Stop polling when terminal status is reached.
    - **Effort:** Small.

### P3 — Nice to Have

- Add `skip_scoring` checkbox to launch form
- Add `output_dir` field to launch form (advanced section)
- Use enriched SSE fields (`tokens_in`, `tokens_out`, `model_used`, `word_count`) in PhaseEventRow
- Add keyboard shortcuts (search focus, refresh)
- Add error boundary (`error.tsx`)
- Remove or document dual SSE interfaces (`api.ts` `streamRun()` vs `sse.ts` `useRunEvents()`)
- Add URL hash for run detail tab state (`#timeline`, `#logs`)
- Add "Re-run" button on completed/failed runs

---

## Appendix: File-Level Issue Index

| File | Line(s) | Issue |
|---|---|---|
| `app/page.tsx` | 5 | Unused `ApiError` import |
| `app/page.tsx` | 30 | Silent failure for runs fetch — user sees "0" with no error |
| `app/page.tsx` | 73 | "Total Runs" shows `items.length` (max 10) not `total` |
| `app/templates/page.tsx` | 32 | Search input missing `aria-label` |
| `app/templates/[id]/TemplateDetailClient.tsx` | 65-66 | `apiKey`/`baseUrl` state collected but `baseUrl` never sent |
| `app/templates/[id]/TemplateDetailClient.tsx` | 105 | `_model_map` stuffed into `input` — convention leak |
| `app/templates/[id]/TemplateDetailClient.tsx` | 109 | `as any` cast hides type mismatch |
| `app/templates/[id]/TemplateDetailClient.tsx` | 131, 160 | Back link goes to `/` instead of `/templates` |
| `app/templates/[id]/TemplateDetailClient.tsx` | 140 | `noValidate` disables browser validation with no client-side replacement |
| `app/runs/page.tsx` | 21 | `scoring_failed` missing from `STATUS_OPTIONS` |
| `app/runs/page.tsx` | 73 | `params as any` bypasses type safety |
| `app/runs/page.tsx` | 103 | `cursor-pointer` on `<tr>` but row isn't clickable |
| `app/runs/[id]/RunDetailClient.tsx` | — | No `getRun()` call on mount — empty state for historical runs |
| `app/runs/[id]/RunDetailClient.tsx` | 168 | Uses `tokens_consumed` instead of `tokens_in`/`tokens_out` |
| `app/runs/[id]/RunDetailClient.tsx` | 172 | Back link goes to `/` instead of `/runs` |
| `app/runs/[id]/RunDetailClient.tsx` | 236 | Mixed design tokens (`border-surface-3`) with raw Tailwind |
| `app/layout.tsx` | 37-47 | `<a>` tags instead of `<Link>` — causes full page reloads |
| `app/layout.tsx` | — | `nav-item-active` class exists but never applied |
| `components/pipeline/SchemaForm.tsx` | 52 | `useEffect` deps list missing `onChange` and `properties` |
| `components/pipeline/ProviderSelector.tsx` | — | `baseUrl` value captured but never reaches the API |
| `components/pipeline/LogViewer.tsx` | — | No auto-refresh for active runs |
| `lib/api.ts` | 227-278 | `streamRun()` appears unused — duplicate of `sse.ts` approach |
| `globals.css` | — | `role="status"` used for error displays (should be `role="alert"`) |
