# Forensic Finding 03 — Roadmap vs Reality

> **Severity:** 🟠 Medium  
> **Impact:** The roadmap claims items are "future work" that have already been implemented, creating confusion about actual project status  
> **Generated:** 2026-03-13

---

## Summary

`ROADMAP.md` organizes work into Phases 1–4 progressing from Level 4 to Level 5 ("Dark Factory"). Many Phase 2, Phase 3, and even Phase 4 items described as future milestones are **already implemented** in `src/orchestration_engine/`. The `level5-requirements.md` document similarly describes detailed functional requirements and acceptance criteria for features that already exist and have tests.

This creates a paradox: the detailed requirements doc is excellent engineering work, but reading it suggests these features are yet to be built when in fact most are production code.

---

## Phase 2: "Autonomous Triggers" — Claimed as Months 1–2 Future Work

| Roadmap Item | Status | Implementation |
|---|---|---|
| **2.1 Webhook-Driven Triggers** | ✅ IMPLEMENTED | `webhooks.py` (TriggerConfig, TriggerMatcher, InputMapper), `web/api.py` (CRUD + webhook endpoint), `db.py` (triggers + webhook_invocations tables) |
| **2.2 Pipeline Composition** | ✅ IMPLEMENTED | `chains.py` (interpolation, on_complete evaluation, depth enforcement), `templates.py` (OnCompleteConfig on PipelineTemplate) |
| **2.3 Confidence-Based Routing** | ✅ IMPLEMENTED | `routing.py` (RoutingEngine, 4-tier default config), `db.py` (routing_decisions table) |
| **2.4 Cost Tracking & Budgets** | ✅ IMPLEMENTED | `cost_tracker.py` (PricingTable, CostTracker, BudgetExceededError), `pricing.yaml`, `db.py` (cost_tracking table), `web/api.py` (cost API endpoints) |
| **2.5 GitHub App Integration** | ✅ IMPLEMENTED | `github_app.py` (JWT auth, installation tokens, webhook signature verification) |

**Verdict:** All 5 Phase 2 items are implemented. The roadmap should reflect this.

---

## Phase 3: "Self-Healing" — Claimed as Months 2–4 Future Work

| Roadmap Item | Status | Implementation |
|---|---|---|
| **3.1 Failed Pipeline Diagnosis** | ✅ IMPLEMENTED | `diagnosis.py` (DiagnosisEngine, 8 failure classes, LLM-powered classification), `db.py` (diagnosis_results table) |
| **3.2 Adaptive Retry Strategies** | ✅ IMPLEMENTED | `adaptive_retry.py` (AdaptiveRetryEngine, 6 strategies, model escalation ladder) |
| **3.3 Regression Detection** | ✅ IMPLEMENTED | `regression.py` (RegressionDetector, git-based commit correlation), `db.py` (regressions + ci_green_shas tables) |
| **3.4 Fleet Monitoring Dashboard** | ⚠️ PARTIAL | REST API has cost/run/trust endpoints; no dedicated fleet dashboard UI exists yet |
| **3.5 Stale Detection & Proactive Maintenance** | ❌ NOT IMPLEMENTED | No implementation found |

**Verdict:** 3 of 5 Phase 3 items are fully implemented. 3.4 is partially implemented (backend only). 3.5 is genuinely future work.

---

## Sprint 7: "Behavioral Trust Gate" — Claimed as "In Progress (March 2026)"

| Roadmap Item | Status | Implementation |
|---|---|---|
| **7.1 Spec-Driven Acceptance Tests** | ✅ MARKED SHIPPED | `acceptance_test` phase in `coding-pipeline-v1.yaml` v1.6, with adversarial spec review |
| **7.2 Hash-Sealed Protected Files** | ✅ MARKED SHIPPED | `file_guard.py` (compute_hash, verify_hash), `protected_outputs` config in templates |
| **7.3 Engine-Executed Test Runner** | ✅ MARKED SHIPPED | `test_runner.py` (run_pytest, parse_pytest_output), `acceptance_run` phase type |
| **7.4 Composite Scorer Reweighting** | 🔄 IN PROGRESS | `confidence.py` has DEFAULT_WEIGHTS_V2 but the roadmap marks this as still running |
| **7.5 Behavioral Validation E2E** | 🔵 NOT STARTED per roadmap | Test file `test_behavioral_validation_e2e.py` exists in tests/ suggesting work is underway |
| **7.6 Sprint Board Automation** | 🔵 NOT STARTED per roadmap | No implementation found |

**Verdict:** The roadmap accurately reflects Sprint 7 status — items marked ✅ are shipped, items marked 🔄/🔵 are in progress or not started.

---

## Sprint 8: "External Validator" — Described as Month 2 Future Work

| Roadmap Item | Status | Implementation |
|---|---|---|
| **8.1 External Validator Subprocess** | ❌ NOT IMPLEMENTED | No `validator_runner.py` found |
| **8.2 IPC Protocol** | ❌ NOT IMPLEMENTED | No JSON-RPC IPC implementation found |
| **8.3 Immutable Test Store** | ❌ NOT IMPLEMENTED | `file_guard.py` provides hash verification but no dedicated sealed test store |
| **8.4 Validator-Driven Retry** | ❌ NOT IMPLEMENTED | Retry is currently orchestrator-driven |
| **8.5 Pipeline Template Integration** | ❌ NOT IMPLEMENTED | No `validation_mode: external` option |

**Verdict:** Sprint 8 is accurately described as future work. None of it is implemented.

---

## Phase 4: "Dark Factory" — Claimed as Months 4–8 Future Work

| Roadmap Item | Status | Implementation |
|---|---|---|
| **4.1 Issue → Pipeline Automation** | ✅ IMPLEMENTED | `issue_automation.py` (full end-to-end: classify → select template → extract inputs → launch), `web/api.py` (GitHub issue endpoints) |
| **4.2 Meta-Orchestration** | ⚠️ PARTIAL | `chains.py` enables pipeline-spawning-pipeline, but no dedicated `meta` phase type. The composition is declarative via `on_complete`, not a meta-pipeline orchestrator |
| **4.3 Deployment Integration** | ❌ NOT IMPLEMENTED | No deploy hooks, staging/production promotion, or rollback pipelines |
| **4.4 Trust Calibration Engine** | ✅ IMPLEMENTED | `trust.py` (TrustCalibrator, EMA scoring, per-repo profiles), `web/api.py` (trust API endpoints), `db.py` (trust_profiles + trust_adjustments tables) |
| **4.5 Audit Trail & Compliance** | ⚠️ PARTIAL | `audit.py` provides adversarial audit; DB stores run records and phase outputs; but no immutable append-only audit log, no PDF export, no signed entries |
| **4.6 Multi-Repo Orchestration** | ❌ NOT IMPLEMENTED | No cross-repo coordination, coordinated PRs, or polyrepo support |

**Verdict:** 2 of 6 Phase 4 items are fully implemented (4.1, 4.4). 2 are partially implemented (4.2, 4.5). 2 are genuinely future work (4.3, 4.6).

---

## Sprint 9–10: Level 4.5 → Level 5

All Sprint 9 and Sprint 10 items (pipeline v2 without review, re-implementation loops, trust auto-adjustment, issue-to-pipeline full automation loop, self-healing regression loop, factory dashboard, kill switch) are genuinely future work with no implementation found.

---

## `level5-requirements.md` — 1,098-Line Requirements Doc for Already-Built Features

This document contains detailed functional requirements, technical requirements, acceptance criteria, and risk assessments for Phases 2–4. The quality is excellent — but it describes features that are already implemented:

| Section | Sub-issues Described | Implemented? |
|---------|---------------------|:---:|
| 2.1 Webhook Triggers | 4 sub-issues | ✅ |
| 2.2 Pipeline Chaining | 3 sub-issues | ✅ |
| 2.3 Confidence Routing | 3 sub-issues | ✅ |
| 2.4 Cost Tracking | 3 sub-issues | ✅ |
| 2.5 GitHub App | 3 sub-issues | ✅ |
| 3.1 Failure Diagnosis | 4 sub-issues | ✅ |
| 3.2 Adaptive Retry | 3 sub-issues | ✅ |
| 3.3 Regression Detection | 4 sub-issues | ✅ |
| 4.1 Issue Automation | 4 sub-issues | ✅ |
| 4.4 Trust Calibration | 3 sub-issues | ✅ |

**Recommendation:** This document should be reclassified from "requirements" to "design record" or "implementation specification." Add a header note: "These requirements have been implemented — see the corresponding modules in `src/orchestration_engine/`."

---

## Overall Roadmap Accuracy Matrix

| Phase | Items | Fully Implemented | Partial | Not Started |
|-------|:-----:|:-----------------:|:-------:|:-----------:|
| Phase 1 (UI Polish) | 5 | Unknown (frontend not fully audited) | - | - |
| Phase 2 (Autonomous Triggers) | 5 | **5** | 0 | 0 |
| Sprint 7 (Behavioral Trust) | 6 | **3** | 1 | 2 |
| Sprint 8 (External Validator) | 5 | 0 | 0 | **5** |
| Phase 3 (Self-Healing) | 5 | **3** | 1 | 1 |
| Sprint 9 (Opaque Weights) | 5 | 0 | 0 | **5** |
| Sprint 10 (Dark Factory) | 5 | 0 | 0 | **5** |
| Phase 4 (Dark Factory) | 6 | **2** | 2 | 2 |

**Bottom line:** ~13 roadmap items described as future work are already implemented. The roadmap's "Where We Are Today" section lists Level 4 capabilities but the engine has quietly progressed to approximately **Level 4.5–4.8** based on implemented features.

---

## Recommendation

1. **Update the roadmap "What's Built" table** to reflect Phase 2, Phase 3, and partial Phase 4 completion
2. **Add a "Sprint 7 Status" section** with per-item checkmarks
3. **Reclassify `level5-requirements.md`** from requirements → design record for implemented items
4. **Mark genuinely future items clearly** — Sprint 8, Sprint 9, Sprint 10, Phase 4.3, Phase 4.6
