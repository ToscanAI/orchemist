# Forensic Finding 08 — Remediation Plan

> **Severity:** Actionable  
> **Purpose:** Break down all forensic findings into workable execution batches  
> **Generated:** 2026-03-13

---

## Execution Strategy

The remediation is organized into **4 tiers** based on dependency ordering and effort level. Tiers must be executed in order — Tier 1 decisions unblock everything downstream.

---

## Tier 1 — Decisions Required (Blockers)

These require a human decision before any remediation can proceed. Each answer affects multiple files.

| # | Decision | Options | Files Affected |
|---|----------|---------|----------------|
| 1a | **License** | MIT or Apache 2.0? | `LICENSE`, `pyproject.toml`, `README.md`, `ROADMAP.md` |
| 1b | **Canonical repo URL** | `github.com/ToscanAI/orchestration-engine` or `github.com/connylazo/orchestration-engine`? | `pyproject.toml`, `README.md`, `GETTING_STARTED.md`, template YAML files |
| 1c | **Product name** | "Orchestration Engine" or "Orchemist"? | `README.md`, `ROADMAP.md`, `pyproject.toml`, CLI help text |
| 1d | **README YAML example** | Fix to real template syntax, or add a "simplified for illustration" disclaimer? | `README.md` |

**Estimated effort:** 5 minutes of decision-making, then ~15 minutes to apply across all files.

**Relates to:** [05-license-metadata-inconsistencies.md](05-license-metadata-inconsistencies.md)


---

## Tier 2 — Factual Corrections to Existing Docs (Quick, Independent)

Each is a self-contained edit to one file. No dependencies between items — all can be executed in a single batch.

| # | File | Fix | Forensic Reference |
|---|------|-----|--------------------|
| 2a | `docs/ARCHITECTURE.md` | Fix serial→parallel claim; add `OpenAICompatibleExecutor` to executor table; update architecture diagram | [01 §1.2, §1.3](01-documentation-drift.md) |
| 2b | `docs/GETTING_STARTED.md` | Remove "serially for simplicity" qualifier; describe parallel wave execution as the default behavior | [01 §1.4](01-documentation-drift.md) |
| 2c | `docs/tech-stack.md` | Rewrite "No Framework Dependencies" section → "Optional Framework Layer"; acknowledge FastAPI as opt-in via `[web]` extra | [01 §1.1](01-documentation-drift.md) |
| 2d | `docs/structured-schemas.md` | Add 9 missing `TaskType` values: `TRIAGE`, `ANALYSIS`, `COMPLIANCE`, `FINANCIAL`, `SALES`, `SUPPORT`, `COMMAND`, `ACCEPTANCE_RUN` | [01 §1.5](01-documentation-drift.md) |
| 2e | `docs/error-recovery.md` | Add "Level 4 Recovery" section covering `DiagnosisEngine` (8 failure classes) and `AdaptiveRetryEngine` (6 strategies) | [01 §1.10](01-documentation-drift.md) |
| 2f | `docs/web-ui.md` | Add note about the separate `/api/v1/` REST API surface; link to new `rest-api-v1.md` (created in Tier 4) | [01 §1.6](01-documentation-drift.md) |
| 2g | `docs/task-queue.md` | Add forward reference to the full database schema doc (created in Tier 4); note that the DB now has 22+ tables | [01 §1.8](01-documentation-drift.md) |

**Estimated effort:** ~30 minutes total for all 7 edits.

**Relates to:** [01-documentation-drift.md](01-documentation-drift.md)

---

## Tier 3 — Stale Doc Headers + Roadmap Update (Quick, Independent)

Add status banners to historical docs so readers know the current state. Update the roadmap's "What's Built" table.

| # | File | Fix | Forensic Reference |
|---|------|-----|--------------------|
| 3a | `docs/orchestration-engine-audit-v2.md` | Add prominent "⚠️ HISTORICAL — RESOLVED" header; note all findings have been addressed | [04 §4.1](04-stale-artifacts.md) |
| 3b | `docs/openclaw-output-extraction-architecture.md` | Add "DECISION MADE — IMPLEMENTED" header; note which approach was chosen | [04 §4.2](04-stale-artifacts.md) |
| 3c | `docs/opus-review-output-capture.md` | Add "⚠️ HISTORICAL — RESOLVED" header; note bugs have been fixed | [04 §4.3](04-stale-artifacts.md) |
| 3d | `docs/output-extraction-architecture-review.md` | Add "⚠️ HISTORICAL — RESOLVED" header; note recommendations were followed | [04 §4.4](04-stale-artifacts.md) |
| 3e | `docs/orchestration-engine-scenario-strategy.md` | Add "STATUS: IMPLEMENTED" header; link to `scenario_runner/` and `quality-gates.md` | [04 §4.5](04-stale-artifacts.md) |
| 3f | `docs/design/phase-transitions-191.md` | Add "STATUS: IMPLEMENTED" header; link to `transitions.py` and `StateMachineSequencer` | [04 §4.6](04-stale-artifacts.md) |
| 3g | `docs/architecture/267-async-run.md` | Add "STATUS: IMPLEMENTED" header; link to `daemon.py` and `orch launch` | [04 §4.7](04-stale-artifacts.md) |
| 3h | `docs/future/metrics.md` | Add section noting which metrics already exist (cost, confidence, trust) vs what remains planned | [04 §4.8](04-stale-artifacts.md) |
| 3i | `ROADMAP.md` | Update "What's Built" table: add Phase 2 items (webhooks, chaining, routing, cost, GitHub App), Phase 3 items (diagnosis, adaptive retry, regression), Phase 4 items (issue automation, trust calibration) | [03](03-roadmap-vs-reality.md) |

**Estimated effort:** ~30 minutes total for all 9 edits.

**Relates to:** [03-roadmap-vs-reality.md](03-roadmap-vs-reality.md), [04-stale-artifacts.md](04-stale-artifacts.md)

---

## Tier 4 — New Documentation Files (Heavy, Sequential)

Each item requires reading source code and writing a new or substantially expanded doc. These should be done one at a time to ensure quality.

| # | File | Scope | Size Estimate | Forensic Reference |
|---|------|-------|:---:|---|
| 4a | `docs/api-reference.md` (update) | Add 15 missing CLI commands: `orch launch`, `wait`, `serve`, `ui`, `api-server`, `logs`, `gate`, `new`, `import`, `chain`, `children`, `rubric`, `reviews`, `resume`, `scenario` | M | [01 §1.7](01-documentation-drift.md), [02 §C](02-undocumented-features.md) |
| 4b | `docs/template-authoring.md` (update) | Add ~15 undocumented phase fields (`write_files`, `supervisor*`, `model_chain`, `protected_outputs`, `context_files`, `command`, etc.) and ~12 pipeline-level fields (`parallel`, `on_complete`, `budget`, `auto_merge`, `tags`, etc.) | L | [02 §B](02-undocumented-features.md) |
| 4c | `docs/rest-api-v1.md` (new) | Document all 33 `/api/v1/` endpoints with request/response schemas, status codes, and examples | XL | [07](07-api-endpoint-audit.md) |
| 4d | `docs/database-schema.md` (new) | Document all 22+ tables with columns, types, relationships, and which module owns each table | L | [06](06-database-schema-drift.md) |
| 4e | `docs/confidence-scoring.md` (new) | `ConfidenceCalculator`, 9 signals, weight tables (v1/v2), `AUTO_MERGE_THRESHOLD`, `HUMAN_REVIEW_THRESHOLD`, calibration rationale | M | [02 §A.1](02-undocumented-features.md) |
| 4f | `docs/trust-calibration.md` (new) | `TrustProfile`, `TrustCalibrator`, EMA algorithm, decay mechanics, per-repo profiles, DB tables | M | [02 §A.3](02-undocumented-features.md) |
| 4g | `docs/pipeline-chaining.md` (new) | `on_complete` syntax, `{{placeholder}}` interpolation, depth limits (max 20), child daemon spawning, `orch chain` monitoring | S | [02 §A.9](02-undocumented-features.md) |
| 4h | `docs/diagnosis-recovery.md` (new) | `DiagnosisEngine` (8 failure classes, LLM classification), `AdaptiveRetryEngine` (6 strategies, model escalation ladder), interaction with base `RecoveryManager` | M | [02 §A.4–A.5](02-undocumented-features.md) |
| 4i | `docs/issue-automation.md` (new) | `IssueClassifier` (6 categories), `TemplateSelector`, `InputExtractor`, `IssueAutomation` end-to-end flow, GitHub API integration, sprint chain integration | M | [02 §A.7](02-undocumented-features.md) |

**Size key:** S = ~100 lines, M = ~200 lines, L = ~400 lines, XL = ~600+ lines

**Estimated effort:** ~2–4 hours total across all 9 items.

**Relates to:** [02-undocumented-features.md](02-undocumented-features.md), [06-database-schema-drift.md](06-database-schema-drift.md), [07-api-endpoint-audit.md](07-api-endpoint-audit.md)

---

## Suggested Execution Order

```
Tier 1 (decisions)  ──►  Tier 2 + Tier 3 (parallel)  ──►  Tier 4 (sequential)
     5 min                       ~1 hour                        ~2-4 hours
```

1. **Batch 1:** Human answers Tier 1 questions → apply metadata fixes (1a–1d)
2. **Batch 2:** Execute all Tier 2 (2a–2g) in one pass — independent file edits
3. **Batch 3:** Execute all Tier 3 (3a–3i) in one pass — header additions
4. **Batch 4:** Execute Tier 4 items one by one (4a → 4i), each requiring code analysis

Batches 2 and 3 can be done in parallel (no dependencies between them).

---

## Tracking

Use this checklist to track remediation progress:

### Tier 1 — Decisions
- [x] 1a: License decided — **MIT** (ROADMAP.md fixed from Apache 2.0 → MIT)
- [x] 1b: Repo URL decided — **Dev: ToscanAI/orchestration-engine, Stable: connylazo/orchestration-engine** (dual-repo noted in README, pyproject, GETTING_STARTED)
- [x] 1c: Product name decided — **Orchemist** (README, pyproject, comparison table, footer updated)
- [x] 1d: README example approach decided — **"simplified for illustration" disclaimer added** with note that format is evolving for diverse workloads

### Tier 2 — Factual Corrections
- [x] 2a: `ARCHITECTURE.md` — parallel execution + executor table + DB section expanded
- [x] 2b: `GETTING_STARTED.md` — parallel execution description updated
- [x] 2c: `tech-stack.md` — FastAPI acknowledged as opt-in `[web]` extra
- [x] 2d: `structured-schemas.md` — 9 missing TaskType values added (14 total)
- [x] 2e: `error-recovery.md` — Level 4 recovery section added (DiagnosisEngine + AdaptiveRetryEngine)
- [x] 2f: `web-ui.md` — `/api/v1/` REST API surface documented (33 endpoints), dual API distinction clarified
- [x] 2g: `task-queue.md` — schema drift note added (22+ tables)

### Tier 3 — Stale Headers + Roadmap
- [x] 3a: `orchestration-engine-audit-v2.md` — HISTORICAL header (already present)
- [x] 3b: `openclaw-output-extraction-architecture.md` — HISTORICAL header (already present)
- [x] 3c: `opus-review-output-capture.md` — HISTORICAL header (already present)
- [x] 3d: `output-extraction-architecture-review.md` — HISTORICAL header (already present)
- [x] 3e: `orchestration-engine-scenario-strategy.md` — Status updated: ✅ IMPLEMENTED (scoring.py, ScenarioRunner, graders)
- [x] 3f: `design/phase-transitions-191.md` — Status updated: ✅ IMPLEMENTED (transitions.py, StateMachineSequencer)
- [x] 3g: `architecture/267-async-run.md` — Status updated: ✅ IMPLEMENTED (daemon.py, orch launch/status/wait)
- [x] 3h: `future/metrics.md` — Updated to PARTIALLY IMPLEMENTED (cost_tracker, confidence, trust live; dashboard deferred)
- [x] 3i: `ROADMAP.md` — "What's Built" expanded: Phase 2 (5 items), Phase 3 (3 items), Phase 4 (2 items), Sprint 7 (4 items), DB row added; "What's Missing" rewritten; Phase 2/3/4 headers annotated with implementation status

### Tier 4 — New/Expanded Docs
- [x] 4a: `api-reference.md` — 15 missing CLI commands documented (launch, wait, logs, children, chain, resume, gate, new, import, serve, ui, api-server, rubric, scenario, reviews)
- [x] 4b: `template-authoring.md` — 11 template-level fields + 15 phase-level fields + 2 aliases documented; 6 new cookbook patterns added; checklist updated
- [x] 4c: `rest-api-v1.md` — 33 endpoints (new file)
- [x] 4d: `database-schema.md` — 21 tables, 20 migrations, 22+ indexes (new file)
- [x] 4e: `confidence-scoring.md` — 9 signals, 2 weight tables, 4 routing tiers, 6 subsystems (new file)
- [x] 4f: `trust-calibration.md` — EMA update rule, threshold derivation, bootstrap guard, idle decay, 3 worked examples (new file)
- [x] 4g: `pipeline-chaining.md` — on_complete config, placeholder interpolation, depth limiting, child spawning, daemon integration, DB schema, CLI/REST, chain monitor, sprint-runner example (new file)
- [x] 4h: `diagnosis-recovery.md` — 8 failure classes, 6 remediations, LLM diagnosis engine, pattern tracking, 6 retry strategies, adaptive retry engine, model escalation ladder, cost estimation, daemon integration (new file)
- [x] 4i: `issue-automation.md` — 6 classification types, LLM classifier, template selector, input extractor, IssueAutomation orchestrator, 2 webhook endpoints, GitHub fetcher, pipeline-ready label trigger, sprint chain with 3 guard rails, generic webhook triggers, result delivery (new file)
