# Forensic Finding 04 — Stale Artifacts

> **Severity:** 🟡 High  
> **Impact:** Historical documents could mislead contributors who don't notice their vintage  
> **Generated:** 2026-03-13

---

## Summary

Several documentation files in the `docs/` folder describe early-stage architecture, known bugs, or recommendations that have since been addressed by implementation work. While some carry "historical" or "partially outdated" warnings, the warnings understate the degree of staleness.

---

## Finding 4.1: `orchestration-engine-audit-v2.md` — Feb 2026 Audit, Now Mostly Resolved

**Document:** `docs/orchestration-engine-audit-v2.md`  
**Date:** February 2026  
**Context:** A second-pass audit that identified critical gaps

**Key claims at time of writing:**
- "OpenClaw integration doesn't exist"
- "No DAG/dependency support"
- "Database API mismatch (claims execute()/fetch_all() but has different methods)"
- "2 of 11 requirements met"

**Current status:** All of these have been addressed:
- OpenClaw executor is fully implemented (`openclaw_executor.py`)
- Template engine has full DAG support with topological sort
- Database has `execute()` and `fetch_all()` implemented
- The content pipeline runs end-to-end

**Risk:** A new contributor reading this file would believe the project is in a broken state and might not trust the codebase.

**Recommendation:** Add a prominent header: "⚠️ HISTORICAL — This audit was conducted in early February 2026. All findings have been addressed. See the current architecture in ARCHITECTURE.md."

---

## Finding 4.2: `openclaw-output-extraction-architecture.md` — Design Options Doc Now Moot

**Document:** `docs/openclaw-output-extraction-architecture.md`  
**Context:** Explores multiple approaches (A–E) for extracting structured output from OpenClaw sub-agent sessions

**Status:** The implementation chose its approach and shipped. The document reads as though a decision hasn't been made yet.

**Recommendation:** Add a header noting which approach was chosen and link to the actual implementation.

---

## Finding 4.3: `opus-review-output-capture.md` — Bug Tracking Doc for Fixed Bugs

**Document:** `docs/opus-review-output-capture.md`  
**Context:** Identifies specific bugs: `_PhaseOutput` parsing issue, polling race condition in OpenClaw executor

**Status:** These bugs were fixed during Sprint 5–6 development. The document remains useful as archaeological context but should not be in the active docs path.

**Recommendation:** Move to `docs/archive/` or add a "RESOLVED" header with links to the fixing commits/PRs.

---

## Finding 4.4: `output-extraction-architecture-review.md` — Architecture Review with Recommendations

**Document:** `docs/output-extraction-architecture-review.md`  
**Context:** Reviews the output extraction approach and recommends "fix 3 bugs first, then try approach A2"

**Status:** The recommendations were followed. The document is now a historical record, not an active guide.

**Recommendation:** Same as 4.3 — archive or mark as resolved.

---

## Finding 4.5: `orchestration-engine-scenario-strategy.md` — Scenario Strategy Now Implemented

**Document:** `docs/orchestration-engine-scenario-strategy.md`  
**Context:** Proposes the scenario runner approach with graders, acceptance criteria, and scoring logic

**Status:** All proposed features are implemented in `scenario_runner/`. The document is accurate as a design record but could be mistaken for a proposal that hasn't been built yet.

**Recommendation:** Add a "STATUS: IMPLEMENTED" header with links to `scenario_runner/` and `quality-gates.md`.

---

## Finding 4.6: `docs/design/phase-transitions-191.md` — Design Doc for Shipped Feature

**Document:** `docs/design/phase-transitions-191.md`  
**Context:** Issue #191 design document for phase transitions and state machine sequencing

**Status:** Fully implemented in `transitions.py` (PhaseOutcome, determine_outcome, extract_verdict) and `StateMachineSequencer` in `sequencer.py`. The coding-pipeline-v1.yaml uses transition-driven phases extensively.

**Recommendation:** Mark as "IMPLEMENTED" with reference to `transitions.py`.

---

## Finding 4.7: `docs/architecture/267-async-run.md` — Async Execution Recommendation, Now Shipped

**Document:** `docs/architecture/267-async-run.md`  
**Context:** Issue #267 architecture recommendation for background/async pipeline execution

**Status:** Fully implemented in `daemon.py` with PID file management, SIGTERM handling, DB-backed progress, and the `orch launch` / `orch wait` / `orch status` CLI commands.

**Recommendation:** Mark as "IMPLEMENTED" with reference to `daemon.py`.

---

## Finding 4.8: `docs/future/` — Future Features Directory, Partially Stale

**Directory:** `docs/future/`  
**Contents:**
- `README.md` — Index of future features
- `mcp-integration.md` — MCP (Model Context Protocol) integration plans
- `memory-system.md` — Cross-run memory/learning system  
- `metrics.md` — Metrics and observability plans

**Status:**
- **MCP integration** — Genuinely future, no implementation found. Accurately labeled.
- **Memory system** — Genuinely future, no implementation found. Accurately labeled.
- **Metrics** — Partially implemented via `cost_tracker.py` (cost metrics), `confidence.py` (scoring metrics), `trust.py` (trust metrics), and the REST API cost/trust endpoints. The doc likely describes a broader metrics vision than what exists.

**Recommendation:** Update `metrics.md` to note which metrics already exist and which are still planned.

---

## Summary of Stale Documents

| Document | Status | Recommended Action |
|----------|--------|--------------------|
| `orchestration-engine-audit-v2.md` | All findings resolved | Add "RESOLVED" header |
| `openclaw-output-extraction-architecture.md` | Decision made, implemented | Note which approach was chosen |
| `opus-review-output-capture.md` | Bugs fixed | Move to archive or mark resolved |
| `output-extraction-architecture-review.md` | Recommendations followed | Mark as resolved |
| `orchestration-engine-scenario-strategy.md` | Fully implemented | Add "IMPLEMENTED" header |
| `design/phase-transitions-191.md` | Fully implemented | Add "IMPLEMENTED" header |
| `architecture/267-async-run.md` | Fully implemented | Add "IMPLEMENTED" header |
| `future/metrics.md` | Partially implemented | Update with current vs planned |
