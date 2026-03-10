# Orchemist Roadmap: Level 4 → Level 5 (Dark Factory)

> **Last updated:** 2026-03-05
> **Status:** Active engineering roadmap
> **Maintainer:** @ToscanAI

> **Open Source Target:** After Sprint 4 (Dark Factory) — ~May 2026
> **License:** Apache 2.0
> **V2 (Go rewrite):** In parallel — `ToscanAI/orchemist-v2`

---

## North Star: A Day in the Life of Level 5

It's Tuesday morning. You open GitHub and see 14 issues closed overnight. Here's what happened while you slept:

1. **09:47 PM** — A user opens issue #892: "CSV export truncates rows > 10,000." Orchemist's issue watcher picks it up, classifies it as a bug (confidence: 0.91), and spawns a coding pipeline.

2. **09:52 PM** — The spec agent reads the issue, identifies `export.py` and `test_export.py` as relevant files, and produces an implementation plan. The plan scores 0.88 on the rubric — above the auto-proceed threshold for this repo.

3. **10:14 PM** — Implementation complete. The review agent flags a missing edge case (empty DataFrame). A fix pipeline runs. Re-review scores APPROVE. Tests pass. The pipeline creates PR #347 with full provenance: issue link, spec, review transcript, test results.

4. **10:16 PM** — Confidence score is 0.93, above this repo's auto-merge threshold of 0.90. CI runs. Green. PR merges. Issue closes with a comment linking the fix.

5. **10:18 PM** — Post-merge CI on `main` passes. The deployment pipeline promotes to staging. Smoke tests pass. The change ships to production at 10:22 PM.

6. **11:30 PM** — Sentry reports a new error in `analytics.py` — unrelated to the fix, but a latent bug exposed by a dependency update merged earlier. Orchemist detects the regression, creates issue #893, and spawns a fix pipeline. By midnight, it's resolved.

7. **Meanwhile** — Two content pipelines ran for the blog. A documentation pipeline updated the API reference after detecting stale docstrings. A research pipeline produced a competitive analysis draft and flagged it for human review (confidence: 0.74, below the auto-publish threshold).

You review 3 items that need human judgment. Everything else handled itself. The factory ran in the dark.

---

## Where We Are Today (Level 4)

### What's Built

| Capability | Status | Key Components |
|---|---|---|
| Multi-agent pipelines | ✅ Shipped | `PhaseSequencer`, `StateMachineSequencer`, parallel wave execution |
| State machine transitions | ✅ Shipped | `transitions.py`, `PhaseOutcome` routing (success/failed/timeout/skipped) |
| Review→fix loops | ✅ Shipped | Supervisor hooks, APPROVE/REVISE/ABORT verdicts, re-review cycles |
| LLM judge scoring | ✅ Shipped | `scoring.py`, `ScenarioRunner`, assertion + LLM + URL graders |
| Hard quality gates | ✅ Shipped | Failed score = no PR, scoring enforcement in daemon |
| CLI | ✅ Shipped | `orch run`, `orch launch`, `orch status`, `orch wait`, `orch serve`, `orch ui` |
| REST API + SSE | ✅ Shipped | FastAPI (`web/api.py`), live progress streaming |
| Web UI | ✅ Shipped | Dashboard, template cards, run detail with live progress |
| Template CRUD | ✅ Shipped | Create/update/delete/validate via API |
| Git lifecycle | ✅ Shipped | `git_integration.py` — branch, commit, push, merge gate (no force-push) |
| Error recovery | ✅ Shipped | `recovery.py` — error classification, backoff, circuit breaker, model escalation |
| Non-blocking daemon | ✅ Shipped | `daemon.py` — PID files, SIGTERM handling, DB-backed progress |
| Coding pipeline v1.1 | ✅ Shipped | spec → implement → review → fix → test (5-phase) |
| Content pipeline v2.7 | ✅ Shipped | 7-phase with fact-checking and red-teaming |
| Testing | ✅ Shipped | 3000+ tests, CI scenario dry-runs, extended QA suite |

### What's Missing for Full Autonomy

The engine can execute pipelines reliably. But a human must:
- **Decide** which pipeline to run and when
- **Trigger** every pipeline manually (`orch launch`)
- **Route** results (merge, retry, or discard)
- **Monitor** for failures and regressions
- **Compose** multi-pipeline workflows by hand

Level 5 eliminates all of these manual steps for routine work.

---

## Phase 1: Complete Level 4 — Polish & Trust (Weeks 1-4)

*Ship the remaining UI and developer experience work that makes Level 4 genuinely usable by teams, not just us.*

### 1.1 Visual Pipeline Builder

- **What:** Drag-and-drop UI for creating pipeline templates. Nodes represent phases, edges represent transitions. Generates valid YAML.
- **Why:** Lowers the barrier to template creation. More templates = more automation coverage. Also makes the transition graph visible and debuggable.
- **Dependencies:** Existing template CRUD API, `config_schema` validation
- **Complexity:** L
- **Existing foundation:** Template card grid UI, `config_schema` auto-generated forms, transition graph validation (`test_transition_graph_validation.py`)

### 1.2 Monaco Editor with Schema Validation

- **What:** In-browser YAML editor (Monaco) with live validation, autocomplete for Orchemist schema fields, and inline error markers.
- **Why:** Power users will always want to edit YAML directly. Monaco with schema awareness catches errors before `orch validate`.
- **Dependencies:** Template CRUD API
- **Complexity:** M
- **Existing foundation:** `orch validate` linting rules, JSON Schema from `config_schema`

### 1.3 Human-in-the-Loop Approvals via UI

- **What:** When a pipeline hits a HITL gate (supervisor verdict = REVISE, or confidence below threshold), the UI shows an approval queue. Reviewer sees the phase output, rubric, and score — clicks Approve, Revise (with feedback), or Reject.
- **Why:** This is the trust-building mechanism. Before we automate approvals, humans need to see and approve enough runs to calibrate confidence thresholds. Also required for regulated environments.
- **Dependencies:** SSE progress streaming, existing HITL `resume_event` mechanism
- **Complexity:** M
- **Existing foundation:** HITL pause/resume in sequencer, `resume_event` with timeout, supervisor APPROVE/REVISE/ABORT verdicts

### 1.4 Template Marketplace v1

- **What:** Browse, search, install, and rate community templates from within the UI. Backed by `community-templates/index.yaml` with GitHub shorthand installs.
- **Why:** Network effects. Every shared template is a reusable automation that others don't have to build. Also establishes Orchemist as a platform, not just a tool.
- **Dependencies:** `orch templates install/uninstall`, template index
- **Complexity:** M
- **Existing foundation:** `template_index.py`, `community-templates/index.yaml`, `orch templates list/info/install`

### 1.5 Run Analytics Dashboard

- **What:** Aggregate metrics across runs: success rate by template, average phase duration, token usage trends, failure hotspots. Stored in SQLite, displayed in UI.
- **Why:** You can't improve what you can't measure. This data feeds Phase 2's confidence-based routing and Phase 3's self-healing.
- **Dependencies:** DB schema for run metrics (already partially there via `db.py`)
- **Complexity:** M
- **Existing foundation:** `db.py` run records, `progress.py` phase timing, token counts from executor results

---

## Phase 2: Level 4.5 — Autonomous Triggers (Months 1-2)

*The engine stops waiting for humans to push buttons. Events trigger pipelines. Pipelines chain together.*

### 2.1 Webhook-Driven Pipeline Triggers

- **What:** HTTP webhook endpoints that map events to pipeline runs. Configuration:
  ```yaml
  triggers:
    - event: github.push
      branch: main
      template: coding-pipeline-v1
      input_map:
        issue_title: "Post-push validation"
        repo_path: "{{payload.repository.full_name}}"
    - event: github.issues.opened
      labels: [bug]
      template: coding-pipeline-v1
      input_map:
        issue_title: "{{payload.issue.title}}"
        issue_body: "{{payload.issue.body}}"
  ```
- **Why:** This is the single most important feature for Level 5. Without event-driven triggers, every pipeline needs a human to start it. With triggers, the factory can respond to the world.
- **Dependencies:** REST API (`web/api.py`), template resolution, daemon launch
- **Complexity:** L
- **Existing foundation:** FastAPI server, `orch launch` non-blocking execution, template resolution by ID/name

### 2.2 Pipeline Composition (Chaining)

- **What:** A pipeline's output can trigger another pipeline. Defined declaratively:
  ```yaml
  on_complete:
    success:
      - template: deploy-staging
        input_map:
          artifact_path: "{{output_dir}}/build"
    failed:
      - template: notify-team
        input_map:
          error_summary: "{{final_output.errors}}"
  ```
- **Why:** Real workflows are multi-pipeline. Code review → deploy → smoke test → promote. Without composition, humans must manually chain these steps.
- **Dependencies:** Webhook triggers (2.1) or internal event bus, template resolution
- **Complexity:** M
- **Existing foundation:** `on_phase_complete` callbacks in sequencer, `_final_output.json` per run, daemon DB records with status

### 2.3 Confidence-Based Routing

- **What:** Scoring results automatically route to different actions based on configurable thresholds:
  ```yaml
  routing:
    auto_merge:
      min_score: 0.95
      requires: [tests_pass, no_security_findings]
    human_review:
      min_score: 0.70
      max_score: 0.95
      notify: [slack, github_pr_comment]
    auto_retry:
      max_score: 0.70
      strategy: escalate_model  # or: split_task, add_context
      max_retries: 2
    reject:
      max_score: 0.40
      action: close_with_comment
  ```
- **Why:** This is what makes the factory run without humans for high-confidence work while still escalating uncertain results. It's the core trust mechanism.
- **Dependencies:** Scoring system (exists), HITL approvals (1.3), run analytics (1.5) for threshold calibration
- **Complexity:** M
- **Existing foundation:** `scoring.py`, LLM judge graders, supervisor APPROVE/REVISE/ABORT, `recovery.py` retry logic with model escalation

### 2.4 Cost Tracking and Budget Limits

- **What:** Track token usage and estimated cost per phase, per run, per template, per org. Enforce budget caps:
  ```yaml
  budget:
    max_cost_per_run: 5.00      # USD
    max_cost_per_day: 50.00
    alert_threshold: 0.80       # alert at 80% of budget
  ```
  Pipeline aborts cleanly when budget is exceeded.
- **Why:** Autonomous pipelines without cost controls will bankrupt you. A runaway retry loop with Opus could burn $100 in minutes. This is a safety mechanism, not a nice-to-have.
- **Dependencies:** Token counts from executors (already captured), pricing table per model
- **Complexity:** M
- **Existing foundation:** Token count extraction in `openclaw_executor.py`, `ExecutorResult` with token metadata, `recovery.py` circuit breaker pattern

### 2.5 GitHub App Integration

- **What:** A GitHub App (or lightweight bot) that receives webhooks, maps them to pipeline triggers, and posts results back as PR comments, issue comments, and status checks.
- **Why:** GitHub is where the work lives. Without native integration, every trigger and result requires manual bridging. The App also provides proper authentication and permission scoping.
- **Dependencies:** Webhook triggers (2.1), pipeline composition (2.2)
- **Complexity:** L
- **Existing foundation:** `git_integration.py` (branch/commit/push), `gh` CLI usage patterns

---

## Phase 3: Level 4.8 — Self-Healing (Months 2-4)

*The factory doesn't just run — it detects problems and fixes them without being told.*

### 3.1 Failed Pipeline Diagnosis

- **What:** When a pipeline fails, a diagnosis agent analyzes the failure:
  - Reads the full run transcript, phase outputs, and error logs
  - Classifies the failure (bad prompt, insufficient context, wrong model, flaky test, infra issue)
  - Recommends a remediation strategy (retry with X, split into Y, escalate to human)
  - Stores diagnosis in DB for pattern analysis
- **Why:** Without diagnosis, retries are blind. With diagnosis, the system can make intelligent decisions about how to recover — and learn from patterns over time.
- **Dependencies:** Run analytics (1.5), error classification (exists in `recovery.py`)
- **Complexity:** L
- **Existing foundation:** `recovery.py` error classification (`ErrorType`: transient/permanent/quality/resource/timeout/rate_limit), `ErrorSeverity`, circuit breaker, model escalation logic

### 3.2 Adaptive Retry Strategies

- **What:** Based on diagnosis (3.1), automatically retry with adjusted parameters:
  | Failure Type | Strategy |
  |---|---|
  | Quality too low | Escalate model tier (Haiku → Sonnet → Opus) |
  | Context insufficient | Inject additional files, expand `context_files` |
  | Task too complex | Split into sub-tasks, spawn child pipelines |
  | Flaky test | Retry unchanged (up to 2x) |
  | Rate limit / infra | Exponential backoff (already implemented) |
  | Prompt unclear | Rephrase prompt with failure context |
- **Why:** Most pipeline failures are recoverable with a different approach. Human-level problem-solving applied to pipeline execution.
- **Dependencies:** Diagnosis (3.1), pipeline composition (2.2) for task splitting, cost tracking (2.4) for budget-aware retries
- **Complexity:** L
- **Existing foundation:** `recovery.py` model escalation, `PhaseDefinition.retries` + `retry_delay_seconds`, `fallback.py` executor fallback chain

### 3.3 Regression Detection and Auto-Fix

- **What:** Monitor CI status after merges. When CI fails on `main`:
  1. Identify the breaking commit (via `git bisect` or blame analysis)
  2. Create a GitHub issue with diagnosis
  3. Spawn a fix pipeline targeting the regression
  4. If the fix pipeline succeeds with high confidence → auto-merge the fix
  5. If not → alert human with full context
- **Why:** This closes the loop. The factory not only builds — it maintains. Regressions caught and fixed in minutes, not hours.
- **Dependencies:** GitHub App (2.5), webhook triggers (2.1), confidence-based routing (2.3), coding pipeline v1.1
- **Complexity:** XL
- **Existing foundation:** `git_integration.py`, coding pipeline with review loops, CI scenario testing infrastructure

### 3.4 Fleet Monitoring Dashboard

- **What:** Unified view of all pipeline runs across all repos and templates:
  - Active runs with live progress
  - Historical success/failure rates with trend lines
  - Cost burn rate and budget utilization
  - Failure pattern clustering (same error across runs → systemic issue)
  - Queue depth and throughput metrics
- **Why:** At scale, you need fleet-level visibility. Individual run monitoring doesn't work when you have 50 runs/day across 10 repos.
- **Dependencies:** Run analytics (1.5), cost tracking (2.4), multi-run DB queries
- **Complexity:** L
- **Existing foundation:** Web UI dashboard, SSE streaming, DB run records, `progress.py`

### 3.5 Stale Detection and Proactive Maintenance

- **What:** Periodically scan repos for:
  - Outdated dependencies (via Dependabot-style analysis)
  - Stale documentation (docstrings that don't match function signatures)
  - TODO/FIXME comments older than N days
  - Test coverage gaps
  Automatically create issues and (optionally) spawn fix pipelines for low-risk items.
- **Why:** Maintenance work is the first thing humans defer. An autonomous system can handle the tedious upkeep that keeps codebases healthy.
- **Dependencies:** Webhook triggers for scheduled runs (cron-style), coding pipeline, confidence-based routing (2.3)
- **Complexity:** L
- **Existing foundation:** Content pipeline's fact-checking phase (pattern for verification), `context_files` for repo scanning

---

## Phase 4: Level 5 — Dark Factory (Months 4-8)

*The factory runs in the dark. Issues become deployments. Pipelines spawn pipelines. Trust is earned and calibrated automatically.*

### 4.1 Issue → Pipeline Automation

- **What:** When an issue is created (or labeled), Orchemist:
  1. Classifies the issue (bug, feature, docs, refactor, research)
  2. Selects the appropriate pipeline template based on classification
  3. Extracts structured inputs from the issue body (repo path, affected files, requirements)
  4. Spawns the pipeline with extracted inputs
  5. Posts a comment: "🏭 Pipeline started — tracking in run #abc123"
  6. On completion, creates a PR (code) or posts output (content/research)
- **Why:** This is the core promise of Level 5. An issue is the specification. Everything else is automated.
- **Dependencies:** GitHub App (2.5), confidence-based routing (2.3), all pipeline templates, issue classification model
- **Complexity:** XL
- **Existing foundation:** Coding pipeline with `config_schema` for structured inputs, template selection by category/tags, `TaskType` enum for classification

### 4.2 Meta-Orchestration (Pipelines Spawning Pipelines)

- **What:** A meta-pipeline that:
  - Receives a high-level goal ("Build a REST API for user management")
  - Decomposes it into sub-tasks (data model, endpoints, auth, tests, docs)
  - Spawns individual pipelines for each sub-task with dependency ordering
  - Aggregates results and resolves conflicts between sub-task outputs
  - Produces a final integrated result
  
  Implementation: A new `meta` phase type that can spawn child pipeline runs and wait for their completion.
- **Why:** Real software work isn't one pipeline. It's 5-20 coordinated pipelines. Meta-orchestration handles work that's too large or complex for a single pipeline.
- **Dependencies:** Pipeline composition (2.2), adaptive retry (3.2), cost tracking (2.4) with aggregate budgets
- **Complexity:** XL
- **Existing foundation:** `PhaseSequencer` with parallel wave execution, `depends_on` dependency graphs, `on_phase_complete` callbacks, daemon with DB-backed status tracking

### 4.3 Deployment Integration

- **What:** Pipeline hooks for CI/CD:
  ```yaml
  deploy:
    staging:
      trigger: on_merge
      pipeline: deploy-staging
      gates: [smoke_tests, health_check]
    production:
      trigger: manual  # or: on_staging_success after 1h soak
      pipeline: deploy-production
      gates: [staging_soak_1h, smoke_tests, rollback_ready]
      rollback:
        trigger: on_error_rate_spike
        pipeline: rollback-production
  ```
  Supports: Docker builds, Kubernetes deploys, serverless functions, static sites. Rollback on failure.
- **Why:** Autonomy means nothing if the code doesn't ship. Deployment is the last mile between "PR merged" and "value delivered."
- **Dependencies:** Pipeline composition (2.2), regression detection (3.3), confidence routing (2.3)
- **Complexity:** L
- **Existing foundation:** `command_executor.py` for shell commands, pipeline chaining concept, git integration

### 4.4 Trust Calibration Engine

- **What:** A system that automatically adjusts confidence thresholds based on historical performance:
  - Per-repo: repos with high merge success rates get lower auto-merge thresholds
  - Per-template: templates that consistently produce good results earn more autonomy
  - Per-task-type: bug fixes might auto-merge at 0.90, new features at 0.95
  - Decay: trust decays if a repo has regressions or reverted PRs
  - Bootstrapping: new repos start with conservative thresholds (human-review everything) and earn autonomy over time
  
  Stored as a trust profile per (repo, template, task_type) tuple. Updated after every run.
- **Why:** Static thresholds are either too conservative (humans review everything = no autonomy) or too aggressive (bad code merges = broken production). Trust must be earned and calibrated dynamically.
- **Dependencies:** Run analytics (1.5), confidence-based routing (2.3), regression detection (3.3)
- **Complexity:** L
- **Existing foundation:** Scoring system with numeric scores, `ScenarioRunner` grading infrastructure, `recovery.py` circuit breaker pattern (same concept: trust that adjusts based on failure rates)

### 4.5 Audit Trail and Compliance

- **What:** Full provenance chain for every automated action:
  - Which issue triggered which pipeline
  - Every LLM call with prompt, response, model, tokens, cost
  - Every scoring decision with rubric, score, verdict
  - Every routing decision (auto-merge vs. human review) with justification
  - Every approval (human or automated) with timestamp and actor
  - Immutable audit log (append-only, signed entries)
  - Export as PDF/JSON for compliance review
- **Why:** You can't run a dark factory without accountability. When something goes wrong (and it will), you need a complete record of what happened and why. Also required for SOC2, ISO 27001, and regulated industries.
- **Dependencies:** All previous phases (this captures everything)
- **Complexity:** L
- **Existing foundation:** DB run records, phase outputs stored to disk, `_final_output.json`, executor results with token metadata, supervisor transcripts

### 4.6 Multi-Repo Orchestration

- **What:** Manage pipelines across multiple repositories:
  - Cross-repo dependency tracking (repo A's API change requires repo B's client update)
  - Coordinated PRs across repos (opened together, merged together or not at all)
  - Monorepo support (different pipeline configs per workspace/package)
  - Polyrepo support (central Orchemist instance managing N repos)
  - Shared configuration and secrets management
- **Why:** Real organizations have 10-100 repos. An orchestration engine that only handles one repo at a time isn't a factory — it's a workbench.
- **Dependencies:** GitHub App (2.5), issue automation (4.1), deployment integration (4.3)
- **Complexity:** XL
- **Existing foundation:** `git_integration.py` is repo-aware, template resolution supports project-local configs

---

## Risks & Guardrails

### What Could Go Wrong

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| **Runaway costs** — retry loops burn $1000+ | 🔴 Critical | High | Budget limits (2.4) with hard caps. Circuit breaker kills pipelines that exceed thresholds. Per-run, per-day, per-org limits. |
| **Bad code auto-merged** — regression in production | 🔴 Critical | Medium | Confidence routing (2.3) with conservative initial thresholds. Trust calibration (4.4) that demotes repos after regressions. Mandatory CI gate. Rollback pipelines (4.3). |
| **Infinite pipeline loops** — pipeline A triggers B triggers A | 🟡 High | Medium | Depth limits on pipeline composition. DAG validation on trigger chains. Run ID propagation to detect cycles. Max spawned children per root run. |
| **Security — LLM injection via issue text** | 🔴 Critical | Medium | Sandboxed execution environments. Input sanitization. No `eval()` of LLM output. Restricted `command_executor` allowlist. Template-level permission scoping. |
| **Stale context — agent edits outdated code** | 🟡 High | High | Always `git pull` before pipeline start. Lock mechanism to prevent concurrent pipelines on same branch. Conflict detection in `git_integration.py`. |
| **Alert fatigue** — too many notifications | 🟠 Medium | High | Batched notifications. Escalation tiers (silent → Slack → email → page). Configurable notification preferences per severity. |
| **Hallucinated fixes** — agent "fixes" something that wasn't broken | 🟡 High | Medium | Hard gate: all fixes must pass existing test suite + new tests. Regression detection (3.3) as safety net. Human review for any change touching > N files. |
| **Single point of failure** — Orchemist daemon crashes | 🟠 Medium | Low | Daemon health checks. Auto-restart via systemd/supervisor. Run state persisted in DB (crash-safe). Orphan run detection and recovery. |

### Non-Negotiable Guardrails

1. **No auto-merge without CI green.** Period. The LLM judge score is necessary but not sufficient. CI must pass.
2. **No production deploy without staging soak.** Even with auto-deploy, there's a mandatory staging period with health checks.
3. **Human-reviewable audit trail for every merge.** Even auto-merged PRs have full provenance. A human can always audit after the fact.
4. **Budget hard caps are hard.** No override. No "just this once." The pipeline dies if the budget is exceeded.
5. **Trust starts at zero.** New repos, new templates, new task types all start fully human-reviewed. Trust is earned, never assumed.
6. **Kill switch.** One command (`orch factory stop`) pauses all autonomous triggers. Human oversight resumes immediately.
7. **No force push. Ever.** Already enforced in `git_integration.py`. This rule survives to Level 5.

---

## Metrics: How We Measure Progress

### Autonomy Index (Primary Metric)

**Definition:** Percentage of pipeline runs that complete without any human intervention.

| Level | Autonomy Index | What It Means |
|---|---|---|
| Level 4 (today) | 0% | Every pipeline manually triggered and reviewed |
| Level 4.5 | 20-40% | Event triggers work, but humans review most results |
| Level 4.8 | 50-70% | High-confidence work auto-merges, failures self-heal |
| Level 5 | 80-95% | Issues → deployments with minimal human touchpoints |

*Note: 100% is not the goal. Some work should always require human judgment.*

### Supporting Metrics

| Metric | Target (Level 5) | How to Measure |
|---|---|---|
| **Mean time from issue to merge** | < 30 min (bugs), < 2h (features) | Timestamp delta: issue created → PR merged |
| **Pipeline success rate** | > 85% first-attempt, > 95% with retries | Successful runs / total runs |
| **Auto-merge rate** | > 60% of PRs | Auto-merged / total PRs created by Orchemist |
| **Regression rate** | < 2% of auto-merged PRs | PRs that caused CI failures on main / total auto-merged |
| **Cost per issue resolved** | < $2 average (bugs), < $10 (features) | Total LLM cost / issues resolved |
| **Human review time** | < 5 min per escalated item | Time from escalation notification to human decision |
| **Trust calibration accuracy** | > 90% | Auto-merge decisions that humans would have also approved |
| **Fleet uptime** | > 99.5% | Daemon healthy hours / total hours |

### Tracking

- All metrics stored in DB alongside run records
- Weekly automated report (generated by — of course — a content pipeline)
- Trend dashboard in fleet monitoring UI (3.4)
- Alerts when metrics deviate > 2σ from rolling 30-day average

---

## Implementation Sequence (Critical Path)

```
Phase 1 (Weeks 1-4)           Phase 2 (Months 1-2)         Phase 3 (Months 2-4)        Phase 4 (Months 4-8)
─────────────────────          ───────────────────────       ──────────────────────       ──────────────────────
                                                              
1.3 HITL Approvals ──────────► 2.3 Confidence Routing ────► 3.3 Regression Detection ──► 4.1 Issue Automation
1.5 Run Analytics  ──────────► 2.1 Webhook Triggers ──────► 3.1 Failure Diagnosis ─────► 4.4 Trust Calibration
                               2.4 Cost Tracking ──────────► 3.2 Adaptive Retry ────────► 4.2 Meta-Orchestration
1.2 Monaco Editor              2.2 Pipeline Composition ──► 3.4 Fleet Dashboard         4.3 Deploy Integration
1.1 Visual Builder             2.5 GitHub App ─────────────► 3.5 Proactive Maintenance   4.5 Audit Trail
1.4 Template Marketplace                                                                 4.6 Multi-Repo
```

**The critical path is: HITL Approvals → Confidence Routing → Regression Detection → Issue Automation → Trust Calibration.**

Everything else is important but not blocking. The critical path is what turns a human-driven tool into an autonomous factory.

---

## What We're NOT Building

Saying no is as important as saying yes. These are explicitly out of scope:

- **Custom LLM training/fine-tuning** — We use frontier models via API. Fine-tuning is a different business.
- **IDE plugin** — Orchemist is infrastructure, not a developer tool. IDE integration is someone else's job.
- **Project management** — We consume issues, not manage them. Jira/Linear/GitHub Projects owns this.
- **Hosted SaaS platform** — Orchemist runs on your infra. We're not building a cloud service (yet).
- **Natural language pipeline definition** — "Build me a pipeline that..." sounds cool but produces unreliable templates. YAML is the source of truth. AI assists, but doesn't replace, template authoring.

---

## Getting Started

If you want to contribute to this roadmap:

1. Pick an item from Phase 1 or Phase 2
2. Open a GitHub issue with the item number (e.g., "Implement 2.1: Webhook Triggers")
3. Read `CONTRIBUTING.md` for pipeline conventions
4. Spec first, build second — every item starts with a spec phase

The roadmap is a living document. It will evolve as we learn what works. But the destination is clear: **the factory runs in the dark.**
