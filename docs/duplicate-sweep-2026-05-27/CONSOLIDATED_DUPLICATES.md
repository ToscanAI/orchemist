# Consolidated Duplication Sweep — 2026-05-27

Four parallel Explore-agent sweeps (Python src/, frontend, templates, tests) producing **38 net-new findings** against the 2026-05-25 `DUPLICATES_REFRESHED.md` baseline. Two days of rapid shipping (#835 → #858, v0.11.0 release, +10 PRs, +100 tests) introduced significant new drift.

## Headline

The **highest-severity finding** is structural, not code-level: `templates/coding-pipeline-skip-spec.yaml` is silently a **v1.6-era pipeline**. The 7d/7e enforcement we shipped in #835 (Phase 0 port) and #857 (producer-side 7d port) only landed in `coding-pipeline-standard.yaml`. Any user running skip-spec gets the OLD quality bar — no Phase 0 inventory, no §3a dual-path detection, no IMPLEMENT 7e self-check, no §7.2 diff-lint pre-push gate, no REVIEW 7d-producer arm comparison. **This drift originates from the engine's lack of a YAML `extends:` / `include:` mechanism** — every prompt edit must be hand-applied to every variant.

This is the same shape as the engine ↔ skills drift we closed yesterday with #857: one canonical authoring source, multiple consuming files, no automated sync. **Engine needs the template composition feature long-tracked as #704**.

## Cross-check against open issues

| Existing issue | Covers (some of) our findings | Action |
|---|---|---|
| **#704** Template composition (extends/include) | Sweep C Findings 1, 2, 4, 5, 6, 8 (all template duplication) | DON'T refile — link our findings to #704; bump priority |
| **#774** Code quality / duplicated patterns | Sweep B Finding 5 (error handling), Finding 11 (zinc tokens) | Reference; possibly close-as-superset after our PRs ship |
| **#775** Error handling inconsistent | Sweep B Finding 5 (error message unwrap), Finding 9 (spinner) | Reference |
| **#773** SSE event parsing unsafe casts | Sweep B Finding 1 (dead streamRun) | Reference — streamRun deletion satisfies part of #773 |
| **#676** Config schema defaults not merged | RESOLVED in #835 (apply_config_schema_defaults) | Close — superseded |
| **#535** Sequencer reject `<MISSING:>` placeholders | RESOLVED in #835 (apply_config_schema_defaults) | Close — superseded |

---

## Findings by tier

### Tier 1 — CRITICAL drift sentinels (5 findings, file now)

#### A1: skip-spec pipeline drifted to v1.6-era (Sweep C-1)
- `templates/coding-pipeline-skip-spec.yaml:252-313` IMPLEMENT lacks all 7d/7e HARD RULES; standard has them at `templates/coding-pipeline-standard.yaml:670-783`.
- skip-spec REVIEW (362-444) missing 7d-producer.
- skip-spec FIX (458-510) doesn't read `existing_symbols.md`.
- **Issue title:** "Sync skip-spec pipeline to standard — port 7d/7e enforcement + add drift lint"

#### A2: `_extract_output_text` + `_safe_write_phase_output` duplicated daemon ↔ cli (Sweep A-1, A-2)
- `daemon.py:2059-2089` + `cli.py:94-159` = ~85 lines of identical logic with cli.py's `_builtins.list` defensive hardening missing in daemon.
- **Issue title:** "Consolidate output-text helpers — `_extract_output_text` + `_safe_write_phase_output` duplicated between daemon.py and cli.py"

#### A3: `streamRun` dead-code SSE client in frontend/lib/api.ts (Sweep B-1)
- 60 lines duplicating `useRunEvents` hook in `lib/sse.ts`; zero callsites confirmed.
- Plus 4 dead type aliases re-declared from `lib/types.ts`.
- **Issue title:** "Delete dead `streamRun()` and duplicated SSE parser in frontend/lib/api.ts"

#### A4: `_insert_run` / `_insert_pipeline_run` helper drift across 11 test files (Sweep D-1)
- 11 hand-rolled variants with subtly different signatures.
- `test_harness_aggregate_endpoints.py:45` uses raw `INSERT INTO pipeline_runs(...)` SQL — silently breaks on schema migration.
- **Issue title:** "Consolidate `_insert_run`/`_insert_pipeline_run` test helpers into one conftest fixture"

#### A5: Database fixture sprawl across 25+ test files (Sweep D-2)
- Four naming conventions (`db`, `fresh_db`, `_make_db()`, `in_memory_db`) with overlapping but non-identical semantics.
- **Issue title:** "Unify Database fixtures — 25+ ad-hoc copies into 2 canonical conftest fixtures"

### Tier 2 — MAJOR consolidation opportunities (12 findings)

#### B1: `_get_persistent_db_path` duplicated 5 ways (Sweep A-3)
- `cli.py:1770`, `web/api.py:31`, `mcp/tools.py:29` (only one with `parents=True`!), `daemon.py:1889` inline, `db.py:55` default.
- **Issue title:** "Promote `default_db_path()` to db.py — currently duplicated in cli.py, web/api.py, mcp/tools.py (with subtle mkdir drift)"

#### B2: Env-var-with-fallback pattern (#839, #841, notifications.py drift) (Sweep A-4)
- `web/api.py:309-316, 925-928` invent the pattern; `notifications.py:704-705` already has a `_int(value, default)` helper.
- **Issue title:** "Lift `_int(env_var, default)` helper from notifications.py to shared utility — 3 sites in web/api.py since #839/#841"

#### B3: `_parse_json_list` mcp ↔ api drift (Sweep A-5)
- Same `completed_phases` parser at `mcp/tools.py:36-45` and inline at `web/api.py:687-697`.
- **Issue title:** "Promote `_parse_json_list` to db.py — duplicated in mcp/tools.py and web/api.py"

#### B4: postmortem_review byte-identical across both coding pipelines (Sweep C-2)
- 45/46 lines byte-identical; only YAML indentation differs.
- **Issue title:** "postmortem_review prompt duplicated byte-for-byte across coding-pipeline-{standard,skip-spec}.yaml — add drift lint"

#### B5: Group 7 worsened — verdict prose triplicated in same file (Sweep C-3)
- `existing_symbols_inventory §5` + SPEC + SPEC_ADVERSARY all restate CONSUME/EXTEND/DIVERGENT/NEW-OK.
- Subtle drift: SPEC_ADVERSARY says NEW-OK requires "(none found) with rationale"; SPEC says "no overlap with sections 1-4"; existing_symbols_inventory says "grep returned zero plausibly-related symbols".
- **Issue title:** "Triplicate CONSUME/EXTEND/DIVERGENT/NEW-OK verdict prose in coding-pipeline-standard.yaml — replace restatements with §5 indirection"

#### B6: acceptance_test 90% identical across the two coding pipelines (Sweep C-4)
- ~58 byte-identical lines; deltas are "Adversary Feedback Integration" (skip-spec only) and Phase 0 inventory hook (standard only).
- **Issue title:** "acceptance_test prompts in coding-pipeline-{standard,skip-spec}.yaml are ~90% identical — add CI drift lint"

#### B7: 11 hand-rolled `let cancelled = false` + Promise.then closures (Sweep B-8)
- Each page reimplements its own cancellation + setState pattern.
- **Issue title:** "Introduce `useApi`/`useFetch` hook to replace 11 hand-rolled cancellation closures"

#### B8: Pagination shape `{items, total, limit, offset}` declared 5x (Sweep B-4)
- Each list endpoint has its own `XListResponse` interface.
- **Issue title:** "Introduce `Paged<T>` generic for paginated list responses (5 interfaces today)"

#### B9: Error-detail unwrap repeated 3x in templates pages (Sweep B-5)
- Same 17-line nested detail/detail.errors/detail.message branching at templates/new, templates/[id]/edit, templates/[id].
- **Issue title:** "Extract `extractApiErrorMessage()` helper for backend 422 detail handling (3 templates pages)"

#### B10: 3 independent time-ago formatters (Sweep B-2)
- `formatElapsed` (runs/page.tsx), `elapsedHoursMin` (gates/page.tsx), anonymous IIFE (gates/page.tsx) with inconsistent output formats.
- **Issue title:** "Consolidate relative-time formatters into `lib/timeFmt.ts`"

#### B11: TestClient + ORCH_DB_PATH pattern in 7+ recent files (Sweep D-3)
- Each new test file invents `_make_client` / `isolated_launcher` / `client_and_db`.
- **Issue title:** "Add shared `api_client` fixture to tests/conftest.py — replaces per-file TestClient + env setup"

#### B12: `pipeline_run` dict literal hardcoded 45+ times in tests (Sweep D-6)
- ~400-500 lines of hand-rolled `{template_path:.., template_id:.., input_json:..., mode:..., output_dir:..., status:...}` dicts.
- **Issue title:** "Add `pipeline_run_dict(run_id, **overrides)` factory to tests/_helpers.py — 45 hand-rolled call sites"

### Tier 3 — MINOR cleanup (12 findings, batchable)

| # | Sweep | Description | Suggested action |
|---|---|---|---|
| C1 | A-6 | `created.isoformat() if hasattr...` pattern (db.py:2767 vs api.py:3150) with response-shape drift | Lift `_normalize_ts` to module level |
| C2 | A-7 | Tempfile-write-then-engine.load pattern 3x in api.py | Extract `_load_yaml_via_tempfile` helper |
| C3 | A-8 | Lazy `from .daemon import apply_config_schema_defaults` x3 in cli.py | Hoist to module top (verify no cycle) |
| C4 | B-3 | `CreateTemplateRequest`/`UpdateTemplateRequest` dead types | Delete; use `TemplateWriteRequest` everywhere |
| C5 | B-6 | FALLBACK_PHASES + FALLBACK_CARDS + STANDARD_PIPELINE_OVERRIDES | Drop fallbacks; render skeleton until /api/v1/phases resolves |
| C6 | B-7 | Phase subtitles encoded frontend-side as shadow state | Move `ui_subtitle` field into YAML; backend owns presentation |
| C7 | B-9 | Inline SVG spinner re-implemented despite `<Spinner>` | Replace 2 inline blocks |
| C8 | B-10 | Dead `TopNav.tsx` (97 lines) | git rm |
| C9 | B-11 | Legacy zinc-themed inputs (14 sites) | Migrate to harness `h-input` token |
| C10 | B-12 | `<Spinner>` defined twice (Spinner.tsx + Button.tsx inline) | Reuse |
| C11 | C-5, C-6 | `[PIPELINE CONTEXT]` framing (27x) + `## GROUND TRUTH` block (12x) | Idiomatic — accept OR engine template-injection feature |
| C12 | D-4 | Source-grep wiring-guard scaffold repeated 14x | Promote `REPO_ROOT` + add `read_src(rel)` helper |

### Tier 4 — NIT / informational (9 findings, may defer)

- C-7: Review verdict block byte-identical x2 (LOW)
- C-8: config_schema duplicated 100% between coding pipelines (LOW)
- D-7: `importorskip("fastapi")` vs direct import convention split (MINOR)
- D-8: `_dead_pid()`/`_live_pid()` duplication in #754/#839 tests (MINOR)
- D-9: Wiring-guard error phrasing inconsistent across new tests (MINOR)
- D-10: Async TestClient pattern (one-off, defer until 2nd use)
- A-False-positives: 3 trails investigated, not actual duplicates
- B-False-positives: 4 trails investigated, not actual duplicates

---

## Recommended sequencing

1. **CRITICAL tier first** — file all 5 issues now; cluster A2 + A3 + A4 + A5 into focused PRs each (engine, frontend, tests respectively).
2. **MAJOR tier batched** — file each as its own issue but tag with `cleanup-2026-05-27` label for grouped processing.
3. **MINOR tier as a cleanup PR** — most are quick wins; package as one "tier-3 cleanup" PR.
4. **NIT tier** — leave un-filed; check back in 2 weeks.
5. **#704 (template composition)** — bump priority to **P1** since 5 of our findings (Sweep C-1, 2, 4, 5, 6, 8) hinge on it.

---

## Files

- `docs/strategic-audit-2026-05-25/DUPLICATES_REFRESHED.md` — 2-day-old baseline
- `docs/duplicate-sweep-2026-05-27/CONSOLIDATED_DUPLICATES.md` — this file
- Per-sweep raw transcripts under `/tmp/claude-1000/-home-toscan-ToscanWorkspace/.../tasks/` (ephemeral)

*Sweep completed 2026-05-27.*
