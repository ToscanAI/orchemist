# Forensic Finding 02 — Undocumented Features

> **Severity:** 🟡 High  
> **Impact:** Contributors and users have no discoverability for significant production features  
> **Generated:** 2026-03-13

---

## Summary

The implementation has outpaced documentation significantly. The following features are fully implemented in `src/orchestration_engine/` with production-quality code, tests, and DB persistence — but have **no dedicated documentation** or are mentioned only in code comments/docstrings.

---

## Category A: Entire Modules With No Documentation

### A.1 Confidence Calculator (`confidence.py` — 858 lines)

**What it does:** Composite confidence scoring from 9 signals: `acceptance_pass_rate`, `test_pass_rate`, `code_quality`, `review_quality`, `change_complexity`, `llm_judge`, `review_catch_value`, `adversarial_audit`, `historical_calibration`.

**Impact:** This is the central scoring mechanism that drives auto-merge decisions, human review routing, and retry strategies. Contributors cannot understand the trust model without reading source code.

**Key classes:** `ConfidenceLevel`, `ConfidenceSignal`, `ConfidenceResult`, `ConfidenceCalculator`  
**Weight tables:** `DEFAULT_WEIGHTS` (v1), `DEFAULT_WEIGHTS_V2`  
**Thresholds:** `AUTO_MERGE_THRESHOLD = 0.90`, `HUMAN_REVIEW_THRESHOLD = 0.70`

---

### A.2 Routing Engine (`routing.py` — 592 lines)

**What it does:** Evaluates `ConfidenceResult` against configurable score tiers to decide: auto-merge, queue for human review, retry with different strategy, or reject.

**Key classes:** `RoutingTier`, `RoutingConfig`, `RoutingDecision`, `RoutingEngine`  
**Default tiers:** auto_merge (≥0.90), queue_review (≥0.70), retry (≥0.50), reject (<0.50)

---

### A.3 Trust Calibrator (`trust.py` — 657 lines)

**What it does:** Per-(repo, template_id, task_type) trust profiles with EMA-based scoring. Trust decays after regressions, increases after successful merges. Auto-adjusts thresholds based on track record.

**Key classes:** `TrustProfile`, `TrustConfig`, `TrustCalibrator`  
**DB tables:** `trust_profiles`, `trust_adjustments`

---

### A.4 Diagnosis Engine (`diagnosis.py` — 552 lines)

**What it does:** LLM-powered failure classification into 8 categories (BAD_PROMPT, INSUFFICIENT_CONTEXT, WRONG_MODEL, FLAKY_TEST, INFRA_ISSUE, QUALITY_GAP, TIMEOUT, BUDGET_EXCEEDED). Collects phase context, calls Haiku for classification, persists to DB.

**Key classes:** `FailureClass`, `Remediation`, `DiagnosisResult`, `DiagnosisEngine`  
**DB table:** `diagnosis_results`

---

### A.5 Adaptive Retry Engine (`adaptive_retry.py` — 704 lines)

**What it does:** Maps `DiagnosisResult` to `RetryPlan` with 6 strategies: ESCALATE_MODEL, ADD_CONTEXT, SPLIT_TASK, REPHRASE_PROMPT, RETRY_UNCHANGED, INCREASE_TIMEOUT. Includes model escalation ladder and budget-aware execution.

**Key classes:** `RetryStrategy`, `RetryPlan`, `AdaptiveRetryEngine`

---

### A.6 Regression Detector (`regression.py` — 1,367 lines)

**What it does:** Git-based breaking commit identification by correlating CI error logs with commit file-path overlap. Persists regression records with lifecycle states (DETECTED → DIAGNOSING → FIXING → FIXED/ESCALATED/NEEDS_REVIEW).

**Key classes:** `RegressionStatus`, `Regression`, `RegressionDetector`  
**DB tables:** `regressions`, `ci_green_shas`

---

### A.7 Issue Automation (`issue_automation.py` — 1,643 lines)

**What it does:** End-to-end GitHub issue → pipeline automation: classify issue (bug/feature/docs/refactor/research/content), select template, extract inputs, post GitHub comment, label, launch pipeline.

**Key classes:** `IssueClassification`, `IssueClassifier`, `TemplateSelector`, `InputExtractor`, `IssueAutomation`  
**DB table:** `issue_pipeline_map`

---

### A.8 Sprint Chain Manager (`sprint_chain.py` — 502 lines)

**What it does:** Post-merge sprint chain automation: loads sprint queue YAML, gets next issue, score guard → budget guard → human-pause check → label → comment → launch pipeline for next issue.

**Key classes:** `SprintQueueConfig`, `TriggerResult`, `SprintChainManager`  
**DB table:** `sprint_chain_state`

---

### A.9 Pipeline Chaining (`chains.py` — 422 lines)

**What it does:** Pipeline composition via `on_complete` blocks in templates. Interpolates `{{placeholder}}` values, enforces chain depth limits (max: 20), spawns child daemon processes.

**Key functions:** `interpolate_input_map()`, `evaluate_on_complete()`, `spawn_chain_runs()`

---

### A.10 Chain Monitor (`chain_monitor.py`)

**What it does:** Monitors active chain runs, tracks parent-child relationships, enables `orch chain` CLI command.

---

### A.11 Cost Tracker (`cost_tracker.py` — 448 lines)

**What it does:** DB-backed per-phase cost tracking with budget enforcement. Loads pricing from `pricing.yaml`, records per-phase costs, computes run/daily aggregates, raises `BudgetExceededError`.

**Key classes:** `PricingTable`, `CostTracker`, `BudgetExceededError`  
**DB table:** `cost_tracking`

---

### A.12 Reviewer Calibration (`reviewer_calibration.py`)

**What it does:** Tracks reviewer accuracy over time. Records calibration data to DB for confidence signal weighting.

**DB table:** `reviewer_calibration`

---

### A.13 Spec Adversary (`spec_adversary.py`)

**What it does:** Adversarial review of spec behavioral contracts before acceptance tests are generated. Checks for vague contracts, trivial satisfaction, missing edge cases, implementation leakage.

---

### A.14 Adversarial Audit (`audit.py` — 500 lines)

**What it does:** Post-scoring adversarial re-review. Security-focused audit that cross-references findings against original reviewer's approvals. Computes accuracy scores and detects false approvals.

**Key classes:** `AuditIssue`, `AuditResult`, `AuditPhase`

---

### A.15 Preflight Checks (`preflight.py`)

**What it does:** Definition-of-ready validation before pipeline launch. Validates inputs, environment, dependencies.

---

### A.16 Postflight Checks (`postflight.py`)

**What it does:** Post-run validation including acceptance test hash verification.

---

### A.17 Notifications (`notifications.py`)

**What it does:** Notification dispatch system. Integrated with issue automation and pipeline events.

---

### A.18 Concurrency Module (`concurrency.py`)

**What it does:** Thread-safe utilities for parallel wave execution.

---

### A.19 Review Catch Value (`review_catch_value.py`)

**What it does:** Measures the incremental value a review phase adds by comparing pre-review and post-review code quality. Feeds into confidence scoring as the `review_catch_value` signal.

---

### A.20 Rubric Generator (`rubric_generator.py`)

**What it does:** Converts skill definitions into evaluation rubrics for LLM judge grading. Powers `orch rubric generate`.

---

## Category B: Undocumented Template/Phase Fields

### B.1 PhaseDefinition Fields Not in `template-authoring.md`

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `write_files` | bool | `False` | Parse `FILE:` blocks from output and write to disk |
| `working_dir` | str | `"."` | Directory for extracted files |
| `base_dir` | str | `""` | Safety root for file writes |
| `command` | str | None | Shell command for `task_type: command` phases |
| `allowed_commands` | list[str] | `[]` | Command prefix allowlist for security |
| `supervisor` | bool | `False` | Enable supervisor evaluation after phase |
| `supervisor_prompt` | str | None | Custom evaluation prompt for supervisor |
| `supervisor_model` | str | None | Model tier override for supervisor |
| `supervisor_rubric` | str | None | Quality rubric for supervisor scoring |
| `supervisor_max_retries` | int | 2 | Max REVISE cycles for supervisor loop |
| `model_chain` | list[str] | `[]` | Fallback model chain on retry |
| `min_output_length` | int | 0 | Minimum character count for phase output |
| `protected_outputs` | list[str] | `[]` | SHA256-sealed files between phases |
| `context_files` | list[str] | `[]` | Local files to inline into prompts |
| `max_iterations` | int | 1 | Max loop iterations for transition-driven phases |

### B.2 PipelineTemplate Fields Not in Any Doc

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `parallel` | bool | `True` | Enable parallel wave execution |
| `max_parallel` | int | - | Max concurrent phases within a wave |
| `fail_fast` | bool | - | Abort all parallel phases on first failure |
| `on_complete` | dict | None | Pipeline chaining configuration |
| `budget` | BudgetConfig | None | Per-run cost limits |
| `auto_merge` | AutoMergeConfig | None | Auto-merge PR configuration |
| `scenario` | str | None | Associated scenario file for validation |
| `category` | str | None | Template category for search/filtering |
| `tags` | list[str] | `[]` | Template tags for discovery |
| `use_cases` | list[str] | `[]` | Template use case descriptions |
| `author` | str | None | Template author |

---

## Category C: Undocumented CLI Commands

See `docs/forensics/01-documentation-drift.md`, Finding 1.7 for the complete list of 15 undocumented CLI commands.

---

## Category D: Undocumented REST API Endpoints

See `docs/forensics/07-api-endpoint-audit.md` for the complete list of 33 `/api/v1/` endpoints, of which zero are formally documented outside of code.

---

## Recommendation

Create the following new documentation files:

1. **`docs/confidence-scoring.md`** — Cover the composite scoring system, weight tables, and auto-merge/review thresholds
2. **`docs/trust-calibration.md`** — Cover trust profiles, EMA scoring, and decay mechanics  
3. **`docs/rest-api-v1.md`** — Complete REST API reference for all `/api/v1/` endpoints
4. **`docs/pipeline-chaining.md`** — Cover `on_complete`, depth limits, and input interpolation
5. **`docs/diagnosis-recovery.md`** — Cover the Level 4 recovery system (DiagnosisEngine → AdaptiveRetryEngine)
6. **`docs/issue-automation.md`** — Cover the GitHub issue → pipeline automation flow
7. **`docs/sprint-chain.md`** — Cover multi-issue sprint execution
8. Update **`docs/template-authoring.md`** — Add all undocumented phase and pipeline fields
9. Update **`docs/api-reference.md`** — Add all undocumented CLI commands
