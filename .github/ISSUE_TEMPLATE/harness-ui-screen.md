---
name: Harness UI Screen
about: A page or panel for the Orchemist Harness (web surface) — scoped to one screen
labels: enhancement, pipeline-ready, frontend
---

<!-- Use this template when filing a sub-issue for the harness epic #810. -->
<!-- One template = one screen (or one tab of a multi-tab screen). Larger scope → split into multiple issues. -->
<!-- The pipeline agent reads ONLY this issue body — keep behavioral context self-contained. -->

## Screen identity

- **Name:** <e.g. Fleet Dashboard, Run Cockpit, Adversary Loop, Trust & Gates, Admin / Activation, Skills Pack Mode>
- **Route:** <`/<path>`>
- **Mockup:** [`docs/harness-redesign-2026-05-24/screens/<N>-<slug>.svg`](https://github.com/ToscanAI/orchemist/tree/main/docs/harness-redesign-2026-05-24/screens)
- **Parent epic:** #810

## Purpose (one paragraph)

<What does this screen exist to do? Who uses it? What is the one job it does best?>

## Pillars this screen surfaces

<!-- Pick from VISION.md §2. Cite at least one. -->
- <e.g. Adversarial quality gates — the dialogue transcript is visible per turn>
- <e.g. One-file-per-phase artifact handoff — every artifact listed with its SHA and size>

## Data this screen consumes

| Source | Path | Live / static | Owner |
|---|---|---|---|
| `GET /api/v1/...` | `frontend/lib/api.ts :: <fn>` | live | engine |
| SSE `...` | `frontend/lib/sse.ts :: <hook>` | live | engine |
| local file | `<path>` | static | skills pack |

## Behavioral Contracts

<!-- Each contract = one testable behavior. Every contract must specify: Given [input/state], the system [observable outcome]. -->

### Happy path

- Given <initial state>, when <user action>, the system <observable outcome>.

### Empty state

- Given <no data yet>, the system <renders explicit empty state, not blank>.

### Live update

- Given <new event over SSE>, the system <updates in place without full re-render>.

### Cross-link affordances

- Given the user clicks <element>, the system <navigates to / opens / focuses>.

### Forbidden affordances

- Given <destructive intent>, the system <does NOT offer this affordance> (per VISION.md §5 "Intentionally NOT in the UI").

## Integration points

- Reads from: `frontend/lib/...`
- Extends: `frontend/components/...`
- New files: `frontend/app/...`
- Reuses primitives: `Button`, `Badge`, `Spinner`, others from `components/ui/`

## Acceptance Criteria

<!-- One checkbox per contract. -->

- [ ] ...
- [ ] No silent fall-through in any status mapping (every status has an explicit variant)
- [ ] Page test covers happy + empty + error states
- [ ] Screen matches mockup at the IA level (cross-links present, hierarchy preserved); pixel-perfect is **not** required
