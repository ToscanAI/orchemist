# Orchemist Engine Frontend Audit — 2026-05-24

## Summary

- **Pages:** 12 (6 unique routes + 6 nested / param variants).
- **Components:** 15 (8 pipeline domain + 3 UI primitives + 1 nav + 1 error boundary + 2 page-client shells).
- **lib/:** 3 modules (`api.ts` 20 functions, `sse.ts` 1 hook, `types.ts` discriminated unions for SSE events).
- **Tests:** 6 suites (≈ 15.8 % of source files); component + lib only — **no page-level tests** on 5 of 7 pages.
- **Rendering mode:** Static export (`output: 'export'` in `next.config.js`) with SPA-fallback HTML shell for dynamic routes (`72be115`). All data is client-fetched at runtime from a live engine; no mocks.

## Pages

| Route | Purpose | Endpoints | User actions |
|---|---|---|---|
| `/` (`app/page.tsx`) | Dashboard with activity stats | `GET /api/v1/templates`, `GET /api/v1/runs?limit=5` | View 5 most-recent runs, quick-launch top 3 templates, click through to detail |
| `/runs` (`app/runs/page.tsx`) | Paginated run list, 10 s poll | `GET /api/v1/runs` (status / template_id filters, offset-limit pagination) | Filter by status & template, sort by created_at, 20 / page, click row → detail |
| `/runs/[id]` (`app/runs/[id]/page.tsx` + `RunDetailClient.tsx`) | Real-time run monitor with SSE | `GET /api/v1/runs/{id}/stream` (phase_started, phase_completed, status_changed), `GET /api/v1/runs/{id}`, `POST .../resume`, `DELETE .../{id}` | Resume / cancel, collapse logs, expand event timeline, heuristic observer panel |
| `/templates` (`app/templates/page.tsx`) | Template grid with search | `GET /api/v1/templates` | Search by name / category / description, navigate to detail, create new |
| `/templates/[id]` (`app/templates/[id]/page.tsx` + `TemplateDetailClient.tsx`) | Detail + launch form | `GET /api/v1/templates/{id}`, `POST /api/v1/runs`, `DELETE /templates/{id}`, `POST .../duplicate` | Launch in 4 modes (dry-run, standalone, openclaw, openrouter), edit / delete / duplicate, override per-phase models |
| `/templates/[id]/edit` (`EditTemplateClient.tsx`) | YAML editor for user templates | `GET /api/v1/templates/{id}`, `PUT /api/v1/templates/{id}` | Edit raw YAML, validate on blur, persist |
| `/templates/new` (`app/templates/new/page.tsx`) | Create via YAML editor | `POST /api/v1/templates` | Paste YAML, validate, create with `source='user'` |

## Components

**UI primitives (`components/ui/`)**

- `Badge.tsx` — variant pill (success / error / warning / info / neutral)
- `Button.tsx` — primary / secondary / ghost / danger × sm / md / lg + loading spinner
- `Spinner.tsx` — animated SVG spinner

**Pipeline domain (`components/pipeline/`)**

- `PhaseList.tsx` — ordered list of template phases (tier badges, task type, depends_on)
- `PhaseEventRow.tsx` — completed / errored phase row with tokens (in / out), cost_usd, elapsed_seconds, collapsible output preview
- `PhaseModelMap.tsx` — per-phase model-override table (haiku / sonnet / opus default; openai / gemini / deepseek dropdowns)
- `ProviderSelector.tsx` — conditional API key input: standalone (Anthropic) or openrouter (OpenRouter); hidden for dry-run / openclaw
- `RunStatusBadge.tsx` — status → variant mapper; pulsing animation for `running`
- `SchemaForm.tsx` — JSON Schema config form (string / number / integer / bool / enum + textarea fallback)
- `LogViewer.tsx` — daemon log viewer with 5 000-line truncation, refresh, auto-scroll-to-bottom, 404 handling
- `TemplateCard.tsx` — clickable template summary card

**Nav**

- `TopNav.tsx` — sticky nav, 3 routes (`/`, `/runs`, `/templates`), active-link via `usePathname()`, health check every 30 s

## lib/

**`api.ts` (20 functions)**

- Templates: `listTemplates`, `getTemplate`, `validateTemplate`, `createTemplate`, `updateTemplate`, `deleteTemplate`, `duplicateTemplate`
- Runs: `startRun`, `listRuns`, `getRun`, `getRunLogs`, `cancelRun`, `resumeRun`
- Gates (issue #743): `listGates`, `getGate`, `approveGate`, `rejectGate` — **endpoints wired, no UI consumes them**
- SSE: `streamRun(runId, onEvent, onError?)` — opens EventSource, parses discriminated union
- Health: `getHealth()` → `{status, version}`

**`sse.ts`**

- `useRunEvents(runId, enabled?)` — React hook around EventSource; returns `{events[], status, connected}`; auto-closes on terminal status; re-mounts on runId change

**`types.ts`**

- `TemplateSummary`, `TemplateDetail`, `PhaseDetail`, `CreateTemplateRequest`, `UpdateTemplateRequest`
- `RunRecord`, `RunsListResponse`, `ListRunsParams`, `RunMode`, `RunStatus`
- SSE events: `SsePhaseStartedEvent`, `SsePhaseCompletedEvent`, `SseStatusChangedEvent`, `SseStreamErrorEvent` (discriminated union `SseEvent`)
- Validation: `TemplateValidateRequest`, `TemplateValidateResponse`
- Error: `ApiError`, `ApiErrorBody`

> **Drift risk:** Gate types declared in `api.ts` not `types.ts`; `RunStatus` literal-union diverges from backend `TaskState` enum (`'pending'`, `'crashed'`, `'scoring_failed'` exist on the frontend with no Python counterpart). See duplicate audit Group 6 (HIGH severity).

## Tests

| Module | Test file | Status |
|---|---|---|
| `components/ui/Badge.tsx` | `Badge.test.tsx` (rendering, variants, ref forwarding, aria) | ✓ |
| `components/ui/Button.tsx` | `Button.test.tsx` (variants, sizes, loading spinner, disabled) | ✓ |
| `components/pipeline/PhaseList.tsx` | `PhaseList.test.tsx` (empty, populated, aria-labels) | ✓ |
| `lib/api.ts` | `api.test.ts` (`listTemplates`, `createTemplate`, `startRun`, `streamRun`, error handling) | ✓ |
| `lib/sse.ts` | `sse.test.ts` (`useRunEvents`, accumulation, terminal status, unmount cleanup) | ✓ |
| `app/templates/[id]/page.tsx` | `TemplateDetailPage.test.tsx` (basic) | ✓ |
| Dashboard, `/runs`, `/runs/[id]`, `/templates`, edit, new | — | **5 page tests missing** (tracked in #776) |

No dead imports / exports detected; all exported components are consumed by parent pages.

## Recent PR history touching `frontend/`

| Commit | Issues | Change |
|---|---|---|
| `c0640da` | #771 | ProviderSelector: controlled input, clear API key after submit |
| `92d7874` | #779, #780, #781 | Dup naming, TOCTOU race, type safety, ID validation |
| `2cc87ed` | #770 | Template CRUD: create / edit / delete / duplicate from Web UI |
| `e4e051a` | #763-#769 | Code review pass: security, types, performance, error handling |
| `18f6b18` | #757, #758, #760, #762 | UI review pass: initial state, nav, health, quick wins |
| `2bd87d5` | — | Separate Dashboard and Templates pages |
| `6a318b4` | — | Read route ID from `window.location` in static export mode |
| `72be115` | — | SPA fallback serves correct HTML shell for dynamic routes |
| `616ac86` | #748 | Observer agent — read-only heuristic monitor |
| `fcdf0d6` | #747 | Enrich SSE phase events with model / timing / token breakdown |

## Gaps for Level-5 harness UI

| Capability | Why missing | Priority |
|---|---|---|
| **Gate management page** (`/gates` or run-detail sidebar) | Endpoints defined (#743) but zero UI consumes them | **HIGH** — merge gates are core to the harness approval flow |
| **Fleet dashboard** (multi-repo, regression queue, autonomy ramp) | No page exists; only single-run detail | **HIGH** — closes ROADMAP §3.4 |
| **Dialogue-phase visualizer** (drafter ↔ reviewer transcripts) | No component for multi-turn agent dialogue | **HIGH** — Track B PR #808 has no UI hook yet |
| **Trust calibration UI** | No surface for per-(repo, template, task_type) trust scores | **MEDIUM** — `trust.py` API exists |
| **Cost breakdown per phase** (receipt / invoice) | Observer panel shows heuristic warnings only | **LOW** — cost_usd already in SSE events |
| **Run-level scoring dashboard** | `scoring_status` / `scoring_score` on `RunRecord` but no UI | **MEDIUM** |
| **Template versioning / diff** | Edit overwrites; no history, no side-by-side diff | **LOW** |
| **Phase-0 inventory viewer** (existing_symbols.md) | New artifact (v4 / v4.2); no UI | **HIGH** — surface for sub-check 7d verdicts |
| **Branch protection enforcement status** | Today all 4 repos are unprotected; no UI shows this | **HIGH** — admin/activation surface |

**Bottom line:** the existing frontend is a solid template+runs CRUD layer. It is **not** a Level-5 harness. The new screens (fleet dashboard, gates queue, dialogue visualizer, trust calibration, admin/activation, skills-pack mode) are net-new builds; they do not displace existing surfaces — they sit alongside them.
