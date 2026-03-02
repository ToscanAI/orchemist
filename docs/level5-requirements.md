# Level 5 Requirements Document — Phases 2, 3, 4

> **Generated:** 2026-03-02
> **Source:** ROADMAP.md analysis against current codebase
> **Scope:** Every item in Phases 2 (4.5), 3 (4.8), and 4 (5.0)
> **Phase 1 items excluded** — those GitHub issues already exist.

---

## Table of Contents

- [Phase 2: Autonomous Triggers](#phase-2-autonomous-triggers)
  - [2.1 Webhook-Driven Pipeline Triggers](#21--webhook-driven-pipeline-triggers)
  - [2.2 Pipeline Composition (Chaining)](#22--pipeline-composition-chaining)
  - [2.3 Confidence-Based Routing](#23--confidence-based-routing)
  - [2.4 Cost Tracking and Budget Limits](#24--cost-tracking-and-budget-limits)
  - [2.5 GitHub App Integration](#25--github-app-integration)
- [Phase 3: Self-Healing](#phase-3-self-healing)
  - [3.1 Failed Pipeline Diagnosis](#31--failed-pipeline-diagnosis)
  - [3.2 Adaptive Retry Strategies](#32--adaptive-retry-strategies)
  - [3.3 Regression Detection and Auto-Fix](#33--regression-detection-and-auto-fix)
  - [3.4 Fleet Monitoring Dashboard](#34--fleet-monitoring-dashboard)
  - [3.5 Stale Detection and Proactive Maintenance](#35--stale-detection-and-proactive-maintenance)
- [Phase 4: Dark Factory](#phase-4-dark-factory)
  - [4.1 Issue → Pipeline Automation](#41--issue--pipeline-automation)
  - [4.2 Meta-Orchestration](#42--meta-orchestration-pipelines-spawning-pipelines)
  - [4.3 Deployment Integration](#43--deployment-integration)
  - [4.4 Trust Calibration Engine](#44--trust-calibration-engine)
  - [4.5 Audit Trail and Compliance](#45--audit-trail-and-compliance)
  - [4.6 Multi-Repo Orchestration](#46--multi-repo-orchestration)

---

## Phase 2: Autonomous Triggers

---

### 2.1 — Webhook-Driven Pipeline Triggers

**Epic:** Yes
**Estimated sub-issues:** 4
1. Webhook receiver endpoint + trigger config schema
2. Event matching engine (filter by branch, labels, event type)
3. Input mapping / template variable interpolation
4. Trigger management API (CRUD + enable/disable)

**Phase:** 2 (Level 4.5 — Autonomous Triggers)
**Complexity:** L
**Dependencies:**
- Roadmap: None (first item in Phase 2, but benefits from 1.5 Run Analytics for observability)
- Code: `web/api.py` (FastAPI app, route registration), `daemon.py` (subprocess launch pattern), `templates.py` (`TemplateEngine.resolve_template`, `load_template`), `db.py` (`insert_pipeline_run`), `schemas.py` (`TaskType` for classification)
**Priority within phase:** 1 (critical path — every other Phase 2 item depends on event-driven triggers)

**Functional Requirements:**
1. FR-1: The system SHALL expose a `POST /api/v1/webhooks/{trigger_id}` endpoint that accepts arbitrary JSON payloads and matches them against registered trigger configurations.
2. FR-2: Trigger configurations SHALL be defined in YAML (either inline in a pipeline template or in a separate `triggers.yaml` file) with the schema: `event`, `filter` (branch, labels, etc.), `template`, and `input_map`.
3. FR-3: The `input_map` SHALL support Jinja2-style or `{{payload.path.to.field}}` variable interpolation against the incoming webhook payload.
4. FR-4: A single webhook endpoint SHALL support multiple trigger configurations — the first matching trigger fires.
5. FR-5: The system SHALL return `202 Accepted` with the `run_id` when a trigger fires, or `200 OK` with `{"matched": false}` when no trigger matches.
6. FR-6: Triggers SHALL be individually enable-able/disable-able without removing configuration.
7. FR-7: The system SHALL support GitHub webhook signature verification (`X-Hub-Signature-256`) for security.
8. FR-8: The system SHALL enforce a configurable rate limit per trigger (e.g., max 10 runs/hour) to prevent webhook storms.

**Technical Requirements:**
1. TR-1: Add a new module `src/orchestration_engine/webhooks.py` containing: `TriggerConfig` (dataclass), `TriggerMatcher` (evaluates filters against payloads), and `WebhookRouter` (maps events to pipeline launches).
2. TR-2: Register webhook routes in `web/api.py` under `create_api_app()` — add `POST /api/v1/webhooks/{trigger_id}` and `POST /api/v1/webhooks` (generic, matches by event type header).
3. TR-3: Reuse the exact launch pattern from `api.py::launch_run()` — resolve template via `_resolve_template()`, validate, persist to DB via `db.insert_pipeline_run()`, spawn daemon subprocess.
4. TR-4: Add a `triggers` table to `db.py` with columns: `id`, `event_type`, `filter_json`, `template_id`, `input_map_json`, `enabled`, `rate_limit`, `last_fired_at`, `fire_count`, `created_at`.
5. TR-5: Implement `{{payload.x.y}}` interpolation using a simple recursive dict-walk — do NOT use `eval()` or `exec()`. Sanitize all interpolated values against prompt injection (strip control characters, limit length to 10KB per field).
6. TR-6: Add GitHub signature verification using `hmac.compare_digest()` with a per-trigger configurable secret.
7. TR-7: Add CRUD endpoints: `GET /api/v1/triggers`, `POST /api/v1/triggers`, `PUT /api/v1/triggers/{id}`, `DELETE /api/v1/triggers/{id}`.

**Acceptance Criteria:**
1. AC-1: A `POST` to `/api/v1/webhooks/my-trigger` with a JSON payload matching the trigger's filter launches a pipeline and returns `202` with a valid `run_id`.
2. AC-2: A `POST` with a non-matching payload returns `200 {"matched": false}`.
3. AC-3: Input map interpolation correctly extracts nested payload fields (e.g., `{{payload.issue.title}}`).
4. AC-4: Invalid GitHub signature returns `403 Forbidden`.
5. AC-5: A trigger with `enabled: false` does not fire.
6. AC-6: Rate limiting blocks the 11th invocation within an hour when configured at 10/hour.
7. AC-7: End-to-end test: simulated `github.issues.opened` webhook → pipeline spawns → status shows `running`.

**Risks:**
- Risk: Webhook storms from CI (many pushes in quick succession) → Mitigation: Per-trigger rate limiting with configurable burst, plus deduplication by payload hash within a time window.
- Risk: Payload injection via crafted webhook bodies → Mitigation: Input map interpolation is string-only (no code eval), values are length-capped, and control characters are stripped.

**Existing Code to Leverage:**
- `web/api.py`: `launch_run()` — the entire launch flow (template resolution, DB insert, daemon spawn) can be extracted into a helper function `_launch_pipeline_from_config()` and reused by both the REST API and the webhook handler.
- `web/api.py`: `_resolve_template()` — template name-to-path resolution, reuse directly.
- `daemon.py`: `run_daemon()` — the subprocess spawning pattern via `subprocess.Popen` is already proven.
- `templates.py`: `TemplateEngine.load_template()` + `validate_template()` — input validation.
- `db.py`: `Database.insert_pipeline_run()` — run record persistence.

**Estimated Token Cost:** High (L complexity, new module, new DB table, new API routes, tests)

---

### 2.2 — Pipeline Composition (Chaining)

**Epic:** Yes
**Estimated sub-issues:** 3
1. `on_complete` declarative config schema + template parser extension
2. Chain executor (fires next pipeline on completion, passes output as input)
3. Cycle detection (DAG validation, depth limits, run-ID propagation)

**Phase:** 2 (Level 4.5)
**Complexity:** M
**Dependencies:**
- Roadmap: 2.1 (webhook triggers provide the internal event bus alternative) or can use direct daemon-to-daemon spawning
- Code: `daemon.py` (`run_daemon` — the completion hook point), `templates.py` (`PipelineTemplate` dataclass needs `on_complete` field), `db.py` (`insert_pipeline_run`), `sequencer.py` (`on_pipeline_complete` callback)
**Priority within phase:** 2 (required for real multi-step workflows; blocks 3.2 task splitting and 4.2 meta-orchestration)

**Functional Requirements:**
1. FR-1: Pipeline templates SHALL support an `on_complete` block with `success` and `failed` sub-keys, each containing a list of downstream pipeline definitions.
2. FR-2: Each downstream definition SHALL specify `template` (name or path) and `input_map` (mapping from parent run outputs to child inputs).
3. FR-3: The `input_map` SHALL support interpolating `{{output_dir}}`, `{{final_output.*}}`, `{{run_id}}`, and `{{status}}` from the parent run.
4. FR-4: Child pipeline runs SHALL be tracked in the DB with a `parent_run_id` foreign key pointing to the triggering run.
5. FR-5: The system SHALL enforce a maximum chain depth (configurable, default 5) to prevent infinite loops.
6. FR-6: The system SHALL detect and reject cycles in static trigger chain definitions (DAG validation at config time).
7. FR-7: A parent run's status page SHALL list all child runs it spawned.

**Technical Requirements:**
1. TR-1: Extend `PipelineTemplate` in `templates.py` with an `on_complete: Optional[Dict[str, List[Dict]]]` field. Parse it in `_load_template_from_dict()`.
2. TR-2: Extend `pipeline_runs` table in `db.py` with `parent_run_id TEXT DEFAULT NULL` and `chain_depth INTEGER DEFAULT 0` columns. Add migration.
3. TR-3: In `daemon.py::run_daemon()`, after the success/failure status update, check `template.on_complete`. For each matching downstream entry, call a new `_spawn_child_run()` helper that: resolves the template, interpolates `input_map`, inserts a new `pipeline_runs` row with `parent_run_id` set, and spawns a new daemon subprocess.
4. TR-4: Implement DAG validation in `templates.py` — when loading a template with `on_complete`, trace the chain (resolving template names) and reject if any template ID appears twice in a chain path.
5. TR-5: Propagate `chain_depth` from parent to child (+1). Reject spawning if `chain_depth >= max_chain_depth`.
6. TR-6: Add `GET /api/v1/runs/{run_id}/children` endpoint in `api.py` returning child run records.

**Acceptance Criteria:**
1. AC-1: A template with `on_complete.success: [{template: deploy-staging}]` spawns the deploy pipeline after successful completion.
2. AC-2: A template with `on_complete.failed: [{template: notify-team}]` spawns the notification pipeline on failure.
3. AC-3: `input_map` interpolation correctly passes `{{final_output.build_path}}` to child pipeline input.
4. AC-4: Chain depth of 6 (when max is 5) is rejected with a clear error message logged.
5. AC-5: Circular chain (A → B → A) is detected at config-load time and raises a validation error.
6. AC-6: `GET /api/v1/runs/{parent_id}/children` returns the spawned child run records.

**Risks:**
- Risk: Infinite loops if A's failure triggers B, and B's failure triggers A → Mitigation: Static DAG validation at template load + runtime chain depth limit + run-ID-based cycle detection.
- Risk: Cascading failures — a broken child pipeline shouldn't affect parent status → Mitigation: Child runs are independent; parent status is set before children spawn.

**Existing Code to Leverage:**
- `daemon.py`: `run_daemon()` lines ~200-230 — the post-completion block where `_final_status` is set is the injection point for chain evaluation. The `subprocess.Popen` daemon spawn pattern is already there.
- `daemon.py`: `_write_summary()` — `result.get('final_output')` extraction logic for populating child input_maps.
- `templates.py`: `_load_template_from_dict()` — extend to parse `on_complete`.
- `web/api.py`: `_run_to_dict()` — extend to include `parent_run_id`.

**Estimated Token Cost:** Medium

---

### 2.3 — Confidence-Based Routing

**Epic:** Yes
**Estimated sub-issues:** 3
1. Routing config schema + integration with scoring output
2. Routing decision engine (threshold evaluation, action dispatch)
3. Notification dispatch (Slack, GitHub PR comment, email stubs)

**Phase:** 2 (Level 4.5)
**Complexity:** M
**Dependencies:**
- Roadmap: 1.3 (HITL Approvals — for the `human_review` routing tier), 1.5 (Run Analytics — for threshold calibration data)
- Code: `scoring.py` (`run_scoring` — provides `weighted_score` and `passed`), `daemon.py` (post-scoring decision point), `recovery.py` (model escalation logic for `auto_retry` action), `schemas.py` (`ModelTier` for escalation)
**Priority within phase:** 2 (critical path — core trust mechanism, blocks 3.3 regression detection, 4.1 issue automation, 4.4 trust calibration)

**Functional Requirements:**
1. FR-1: Pipeline templates SHALL support a `routing` block defining score-based action tiers: `auto_merge`, `human_review`, `auto_retry`, and `reject`.
2. FR-2: Each tier SHALL define `min_score` and/or `max_score` thresholds (0.0–1.0 range).
3. FR-3: The `auto_merge` tier SHALL support additional boolean requirements (e.g., `requires: [tests_pass, no_security_findings]`).
4. FR-4: The `auto_retry` tier SHALL support retry strategies: `escalate_model`, `split_task`, `add_context`, with a `max_retries` cap.
5. FR-5: The `human_review` tier SHALL support notification channel configuration (`notify: [slack, github_pr_comment]`).
6. FR-6: The `reject` tier SHALL close the pipeline and optionally post a comment (e.g., on a GitHub issue).
7. FR-7: When no `routing` block is defined, the system SHALL default to: ≥0.90 → auto_merge, 0.70–0.90 → human_review, <0.70 → reject.
8. FR-8: Every routing decision SHALL be logged with: score, tier matched, action taken, and timestamp.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/routing.py` with: `RoutingConfig` (Pydantic model parsed from template YAML), `RoutingDecision` (dataclass: tier, action, score, justification), and `RoutingEngine.evaluate(score: float, context: dict) -> RoutingDecision`.
2. TR-2: Extend `PipelineTemplate` in `templates.py` with `routing: Optional[RoutingConfig]` field. Parse in `_load_template_from_dict()`.
3. TR-3: In `daemon.py::run_daemon()`, after the scoring block (line ~220 where `scoring_passed` is set), invoke `RoutingEngine.evaluate()` with the `scoring_score`. Based on the returned `RoutingDecision`:
   - `auto_merge`: proceed to git merge (requires 2.5 GitHub App or `git_integration.py`)
   - `human_review`: pause the run (set status to `awaiting_review`), post notifications
   - `auto_retry`: re-spawn the pipeline with adjusted parameters (escalated model, etc.)
   - `reject`: set status to `rejected`, post closing comment
4. TR-4: Add a `routing_decisions` table in `db.py`: `id`, `run_id`, `score`, `tier`, `action`, `justification`, `created_at`.
5. TR-5: Add `awaiting_review` and `rejected` as valid run statuses in the DB and API response models.
6. TR-6: Implement a lightweight notification dispatcher in `routing.py` with pluggable backends (start with `log` and `github_comment` via `gh` CLI).

**Acceptance Criteria:**
1. AC-1: A run scoring 0.96 with `auto_merge.min_score: 0.95` triggers the auto-merge action.
2. AC-2: A run scoring 0.82 with `human_review` band 0.70–0.95 sets status to `awaiting_review`.
3. AC-3: A run scoring 0.65 with `auto_retry` configured and `max_retries: 2` spawns a retry run with an escalated model tier.
4. AC-4: A run scoring 0.35 with `reject.max_score: 0.40` sets status to `rejected`.
5. AC-5: Every routing decision is queryable via `GET /api/v1/runs/{run_id}` (includes `routing_decision` in response).
6. AC-6: Default routing thresholds apply when no `routing` block is in the template.

**Risks:**
- Risk: Overly aggressive auto-merge thresholds merge bad code → Mitigation: Default thresholds are conservative (0.90+). Trust calibration (4.4) will adjust dynamically. CI must still be green (non-negotiable guardrail).
- Risk: Auto-retry loops burn tokens → Mitigation: `max_retries` cap per routing config + budget limits (2.4) as hard stop.

**Existing Code to Leverage:**
- `scoring.py`: `run_scoring()` returns `(passed, weighted_score)` — this is the input to the routing engine.
- `daemon.py`: Lines ~200-230 — the post-scoring block is the injection point. Currently it sets `_final_status` to `scoring_failed`; routing replaces this with a multi-path decision.
- `recovery.py`: `RecoveryManager.handle_task_failure()` — the model escalation logic (`select_model_tier()`, `escalation_path`) can be reused for `auto_retry.strategy: escalate_model`.
- `recovery.py`: `ErrorClassifier.get_retry_config()` — retry parameter computation.
- `git_integration.py`: `GitContext` — for `auto_merge` action implementation.

**Estimated Token Cost:** Medium

---

### 2.4 — Cost Tracking and Budget Limits

**Epic:** Yes
**Estimated sub-issues:** 3
1. Cost tracking model + pricing table + per-phase cost computation
2. Budget enforcement (per-run, per-day, per-org hard caps)
3. Cost reporting API endpoints + dashboard data

**Phase:** 2 (Level 4.5)
**Complexity:** M
**Dependencies:**
- Roadmap: None (can be built independently)
- Code: `openclaw_executor.py` (`tokens_consumed` in `TaskResult`), `daemon.py` (`_on_phase_complete` — captures `tokens_consumed` and `cost_usd`), `config.py` (`ResourceConfig.daily_budget_usd` — already exists but unused), `db.py` (`pipeline_run_events` table already captures `tokens_consumed` and `cost_usd`)
**Priority within phase:** 3 (safety mechanism — important but doesn't block other items directly; 2.3 auto_retry and 3.2 need it for budget-aware retries)

**Functional Requirements:**
1. FR-1: The system SHALL maintain a pricing table mapping model identifiers to per-token input/output costs.
2. FR-2: After each phase, the system SHALL compute `cost_usd` from `tokens_consumed × token_price` and persist it to the DB.
3. FR-3: Templates SHALL support a `budget` block with: `max_cost_per_run` (USD), `max_cost_per_day` (USD), and `alert_threshold` (0.0–1.0 fraction).
4. FR-4: When a run's cumulative cost exceeds `max_cost_per_run`, the pipeline SHALL abort cleanly with status `budget_exceeded`.
5. FR-5: When the daily aggregate cost across all runs exceeds `max_cost_per_day`, no new runs SHALL be launched (return `429` with explanation).
6. FR-6: When cumulative cost reaches `alert_threshold × max_cost`, the system SHALL emit a warning log and (optionally) a notification.
7. FR-7: The API SHALL expose `GET /api/v1/costs` returning aggregate cost by day, by template, and by model.
8. FR-8: The API SHALL expose `GET /api/v1/runs/{run_id}/costs` returning per-phase cost breakdown.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/cost_tracker.py` with: `PricingTable` (dict mapping model string to `CostPerToken(input, output)` — load from `pricing.yaml` or hardcode with override), `CostTracker` (accumulates cost per run, checks against budget, returns `BudgetStatus`).
2. TR-2: Extend `PipelineTemplate` in `templates.py` with `budget: Optional[BudgetConfig]`. Parse in `_load_template_from_dict()`.
3. TR-3: In `daemon.py::_on_phase_complete()`, after writing the phase event, call `CostTracker.record_phase_cost(run_id, phase_id, model, tokens_consumed)`. If `CostTracker.check_budget(run_id)` returns `EXCEEDED`, set a flag that causes the sequencer to abort.
4. TR-4: The abort mechanism: add a `_budget_exceeded` flag checked by the sequencer between phases. In `PhaseSequencer.execute()` (sequencer.py), check this flag before each wave. Alternatively, use the existing `_shutdown_requested` pattern from daemon.py as a model.
5. TR-5: Add `cost_tracking` table in `db.py`: `id`, `run_id`, `phase_id`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `created_at`. Add `daily_costs` view or query aggregating by date.
6. TR-6: Extend `config.py::ResourceConfig` to surface `daily_budget_usd` (already exists) and add `per_run_budget_usd`. Wire these as global fallbacks when templates don't specify budgets.
7. TR-7: In `web/api.py::launch_run()`, before spawning the daemon, check `CostTracker.check_daily_budget()`. Return `429` if exceeded.
8. TR-8: Add API endpoints in `api.py`: `GET /api/v1/costs` and `GET /api/v1/runs/{run_id}/costs`.

**Acceptance Criteria:**
1. AC-1: A pipeline with `max_cost_per_run: 5.00` aborts cleanly when phase costs total $5.01, with status `budget_exceeded`.
2. AC-2: A daily budget of $50 blocks new launches after the aggregate daily cost reaches $50.
3. AC-3: `cost_usd` is correctly computed from tokens × pricing table for each phase and persisted.
4. AC-4: `GET /api/v1/costs` returns daily aggregates with correct sums.
5. AC-5: Alert threshold at 0.80 emits a warning when 80% of budget is consumed.
6. AC-6: Budget enforcement works even if the daemon crashes and restarts (costs are read from DB, not in-memory).

**Risks:**
- Risk: Inaccurate token counts from `sessions_list` (gateway may not report tokens perfectly) → Mitigation: Use the best available data; add a multiplier safety margin (e.g., 1.2×). Log discrepancies.
- Risk: Token pricing changes (Anthropic updates pricing) → Mitigation: Externalize pricing to `pricing.yaml` (not hardcoded). Provide CLI command to update.

**Existing Code to Leverage:**
- `openclaw_executor.py`: `_run_session()` already extracts `total_tokens` from `sessions_list` tool invoke (~line 350). The `estimate_cost()` method has a rough per-tier cost map — this is the seed for the pricing table.
- `daemon.py`: `_on_phase_complete()` callback already receives `tokens_consumed` and `cost_usd` and writes them to the `pipeline_run_events` table via `_write_phase_event()`.
- `config.py`: `ResourceConfig.daily_budget_usd` — field already exists, just needs wiring.
- `db.py`: `pipeline_run_events` table already has `tokens_consumed` and `cost_usd` columns.

**Estimated Token Cost:** Medium

---

### 2.5 — GitHub App Integration

**Epic:** Yes
**Estimated sub-issues:** 5
1. GitHub App manifest + authentication (JWT + installation tokens)
2. Webhook receiver integration (connect to 2.1 webhook system)
3. PR comment / issue comment posting
4. Commit status checks API integration
5. Documentation + setup wizard

**Phase:** 2 (Level 4.5)
**Complexity:** L
**Dependencies:**
- Roadmap: 2.1 (webhook triggers — the App receives webhooks and feeds them to the trigger system), 2.2 (pipeline composition — App posts results from chained pipelines)
- Code: `git_integration.py` (`GitContext` — branch/commit/push, `gh` CLI usage), `web/api.py` (webhook route registration)
**Priority within phase:** 4 (enabler for Phase 3 and 4 features, but not on the critical path for Phase 2 demos)

**Functional Requirements:**
1. FR-1: The system SHALL provide a GitHub App manifest (`app.yaml` or equivalent) for one-click installation on GitHub organizations/repos.
2. FR-2: The App SHALL authenticate using JWT → installation token flow (not personal access tokens).
3. FR-3: The App SHALL receive and forward these GitHub events to the webhook trigger system (2.1): `push`, `pull_request`, `issues`, `issue_comment`, `check_suite`, `status`.
4. FR-4: The App SHALL post PR review comments with pipeline results (score, verdict, link to run details).
5. FR-5: The App SHALL post issue comments when a pipeline is triggered by an issue event (e.g., "🏭 Pipeline started — run #abc123").
6. FR-6: The App SHALL create/update commit status checks (`pending`, `success`, `failure`) reflecting pipeline progress.
7. FR-7: The App SHALL support multi-repo installation (one App, many repos).
8. FR-8: Authentication tokens SHALL be scoped to the minimum required permissions: `contents: read`, `issues: write`, `pull_requests: write`, `checks: write`, `statuses: write`.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/github_app.py` with: `GitHubAppAuth` (JWT generation from private key, installation token caching), `GitHubClient` (REST API wrapper for comments, statuses, checks), `GitHubWebhookHandler` (signature verification + event routing to 2.1).
2. TR-2: Store the App's private key and App ID in a config file (`~/.orchestration-engine/github-app.pem` + `config.toml` section). Never log or expose the key.
3. TR-3: Register a `POST /api/v1/github/webhook` route in `api.py` that delegates to `GitHubWebhookHandler`. This handler verifies the signature, extracts the event type from `X-GitHub-Event` header, and calls the 2.1 trigger matcher.
4. TR-4: In `daemon.py`, after run completion, if the run was triggered by a GitHub event (detectable via `parent_trigger_type` field), call `GitHubClient.post_pr_comment()` or `GitHubClient.post_issue_comment()` with the run summary.
5. TR-5: Implement status check updates: `pending` on run start (in `_on_phase_start`), `success`/`failure` on run completion (in the daemon's final status block).
6. TR-6: Extend `pipeline_runs` table with `trigger_type TEXT`, `trigger_payload_json TEXT` to preserve provenance.

**Acceptance Criteria:**
1. AC-1: A GitHub App can be installed on a test repository via the manifest.
2. AC-2: A `push` event to `main` on the installed repo triggers a configured pipeline.
3. AC-3: Pipeline completion posts a PR comment with score and verdict.
4. AC-4: Pipeline start/completion updates the commit status check (visible in GitHub UI).
5. AC-5: Multi-repo: installing the App on 2 repos triggers separate pipelines per repo config.
6. AC-6: Invalid webhook signatures are rejected with `403`.

**Risks:**
- Risk: GitHub App private key compromise → Mitigation: Key stored in restricted file permissions (0600), never logged, never included in DB or API responses.
- Risk: Rate limiting by GitHub API (5000 requests/hour for App installations) → Mitigation: Cache installation tokens (valid for 1h), batch status updates, minimize API calls.
- Risk: Complex setup (private key, App ID, webhook URL configuration) → Mitigation: Provide a setup wizard (`orch github setup`) that guides through App creation.

**Existing Code to Leverage:**
- `git_integration.py`: `GitContext._run_git()` — subprocess-based git operations. The `gh pr create` pattern (line ~380) shows existing CLI integration that could be replaced by direct API calls via `GitHubClient`.
- `web/api.py`: Route registration pattern — all CRUD endpoints are examples.
- `webhooks.py` (2.1): `TriggerMatcher` — the GitHub webhook handler delegates to this for trigger evaluation.

**Estimated Token Cost:** High (L complexity, external API integration, auth flow, multiple interaction points)

---

## Phase 3: Self-Healing

---

### 3.1 — Failed Pipeline Diagnosis

**Epic:** Yes
**Estimated sub-issues:** 3
1. Diagnosis agent prompt + run transcript assembler
2. Failure classification engine (extends `recovery.py` ErrorClassifier)
3. Diagnosis persistence + pattern analysis queries

**Phase:** 3 (Level 4.8 — Self-Healing)
**Complexity:** L
**Dependencies:**
- Roadmap: 1.5 (Run Analytics — provides historical failure data), 2.4 (Cost Tracking — diagnosis runs cost tokens too)
- Code: `recovery.py` (`ErrorClassifier`, `ErrorType`, `ErrorSeverity` — existing classification), `daemon.py` (`_extract_output_text`, phase output reading), `scoring.py` (`_load_pipeline_output` — assembles run artifacts), `db.py` (`pipeline_run_events` — failure event data)
**Priority within phase:** 1 (critical path — 3.2 adaptive retry depends on diagnosis output)

**Functional Requirements:**
1. FR-1: When a pipeline fails, the system SHALL spawn a lightweight diagnosis agent that analyzes the full failure context.
2. FR-2: The diagnosis agent SHALL receive: run transcript (all phase outputs), error logs, error messages, model used, template configuration, and any partial output.
3. FR-3: The diagnosis SHALL classify the failure into one of: `bad_prompt`, `insufficient_context`, `wrong_model`, `flaky_test`, `infra_issue`, `quality_gap`, `timeout`, `budget_exceeded`.
4. FR-4: The diagnosis SHALL recommend a remediation strategy: `retry_same`, `retry_escalated_model`, `retry_with_context`, `split_task`, `escalate_to_human`, `no_action`.
5. FR-5: Diagnosis results SHALL be persisted in the DB for pattern analysis.
6. FR-6: The system SHALL detect recurring failure patterns (same error across multiple runs on the same template/repo) and flag them as systemic issues.
7. FR-7: Diagnosis SHALL complete within 60 seconds and cost < $0.10 (use Haiku or Sonnet, not Opus).
8. FR-8: The API SHALL expose `GET /api/v1/runs/{run_id}/diagnosis` returning the diagnosis result.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/diagnosis.py` with: `DiagnosisEngine` (assembles context, spawns diagnosis agent, parses result), `DiagnosisResult` (dataclass: failure_class, remediation, confidence, explanation), `FailurePattern` (for recurring pattern detection).
2. TR-2: The diagnosis agent prompt SHALL be a hardcoded template in `diagnosis.py` that includes: the phase outputs (truncated to 4000 chars each), the error messages, the template ID, and instructions to classify + recommend.
3. TR-3: Use `OpenClawExecutor` (or `AnthropicExecutor` in standalone mode) to spawn the diagnosis agent — reuse the same executor as the pipeline. Set model to Haiku, thinking to off, timeout to 60s.
4. TR-4: In `daemon.py::run_daemon()`, after the failure status update (line ~200 where `aborted = True`), call `DiagnosisEngine.diagnose(run_id, db)`. Store the result via `db.insert_diagnosis()`.
5. TR-5: Add `diagnosis_results` table in `db.py`: `id`, `run_id`, `failure_class`, `remediation`, `confidence`, `explanation`, `model_used`, `tokens_consumed`, `created_at`.
6. TR-6: Add `failure_patterns` table: `pattern_hash` (hash of error message template), `template_id`, `failure_class`, `occurrence_count`, `first_seen`, `last_seen`, `is_systemic` (bool, set when count > 3 within 7 days).
7. TR-7: Extend `recovery.py::ErrorClassifier` with a new method `classify_with_llm(error_message, context) -> DiagnosisResult` that delegates to the diagnosis agent for errors that don't match keyword patterns.
8. TR-8: Add `GET /api/v1/runs/{run_id}/diagnosis` and `GET /api/v1/diagnosis/patterns` endpoints.

**Acceptance Criteria:**
1. AC-1: A pipeline failing with "confidence too low" is diagnosed as `quality_gap` with remediation `retry_escalated_model`.
2. AC-2: A pipeline failing with "connection reset" is diagnosed as `infra_issue` with remediation `retry_same`.
3. AC-3: A pipeline failing because the prompt was unclear is diagnosed as `bad_prompt` with remediation `escalate_to_human`.
4. AC-4: Diagnosis completes in < 60s and consumes < 10K tokens.
5. AC-5: After 4 runs failing with the same error on the same template within 7 days, the pattern is flagged as `is_systemic = True`.
6. AC-6: `GET /api/v1/runs/{run_id}/diagnosis` returns the diagnosis result with `failure_class` and `remediation`.

**Risks:**
- Risk: Diagnosis agent hallucinates wrong failure class → Mitigation: Include structured output format in prompt (force JSON with enum values). Validate response against enum. Fall back to `ErrorClassifier` keyword matching if LLM output is unparseable.
- Risk: Diagnosis itself fails (meta-failure) → Mitigation: Wrap in try/except, log warning, fall back to `recovery.py` keyword-based classification. Diagnosis failure must never block the pipeline failure reporting.

**Existing Code to Leverage:**
- `recovery.py`: `ErrorClassifier.classify()` — the keyword-based classification is the fallback when LLM diagnosis fails or is too slow. The `ErrorType` enum maps directly to `failure_class` values.
- `recovery.py`: `RecoveryManager.handle_task_failure()` — the retry decision logic. Diagnosis output feeds into this.
- `scoring.py`: `_load_pipeline_output()` — assembles phase outputs from disk. Reuse for building the diagnosis agent's context.
- `daemon.py`: `_extract_output_text()` — extracts readable text from phase output dicts. Use for truncating phase outputs in the diagnosis prompt.
- `openclaw_executor.py`: `OpenClawExecutor.execute()` — spawn the diagnosis agent as a lightweight sub-agent.

**Estimated Token Cost:** Medium

---

### 3.2 — Adaptive Retry Strategies

**Epic:** Yes
**Estimated sub-issues:** 3
1. Strategy engine (maps diagnosis → retry parameters)
2. Context enrichment (inject additional files, expand context_files)
3. Task splitting (decompose into child pipelines via 2.2 composition)

**Phase:** 3 (Level 4.8)
**Complexity:** L
**Dependencies:**
- Roadmap: 3.1 (diagnosis — provides failure classification), 2.2 (pipeline composition — for task splitting), 2.4 (cost tracking — budget-aware retries)
- Code: `recovery.py` (`RecoveryManager`, `TaskRetryState`, model escalation path), `daemon.py` (retry spawning), `sequencer.py` (`PhaseDefinition.retries`, `retry_delay_seconds`), `config.py` (`RetryConfig`)
**Priority within phase:** 2 (depends on 3.1; enables self-healing loop)

**Functional Requirements:**
1. FR-1: Based on the diagnosis result (3.1), the system SHALL automatically select and execute a retry strategy without human intervention.
2. FR-2: Supported strategies:
   - `escalate_model`: Re-run with the next model tier (Haiku → Sonnet → Opus).
   - `add_context`: Re-run with additional context files injected into the prompt.
   - `split_task`: Decompose the task into sub-tasks and spawn child pipelines (via 2.2).
   - `rephrase_prompt`: Re-run with the failure context appended to the original prompt.
   - `retry_unchanged`: Re-run with identical parameters (for flaky/transient failures).
   - `increase_timeout`: Re-run with 2× the original timeout.
3. FR-3: Each strategy SHALL respect the cost budget (2.4) — abort if the retry would exceed remaining budget.
4. FR-4: The system SHALL track retry attempts and cap total retries per run (configurable, default 3).
5. FR-5: If all retry strategies are exhausted, the system SHALL escalate to human review.
6. FR-6: Retry decisions and outcomes SHALL be logged for future strategy optimization.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/adaptive_retry.py` with: `RetryStrategy` (enum of strategies), `AdaptiveRetryEngine` (maps `DiagnosisResult` → `RetryStrategy` + parameters), `RetryPlan` (dataclass: strategy, model_tier, extra_context_files, split_tasks, timeout_override).
2. TR-2: The mapping from diagnosis to strategy SHALL be configurable (default mapping in code, overridable via template YAML):
   - `quality_gap` → `escalate_model`
   - `insufficient_context` → `add_context`
   - `bad_prompt` → `rephrase_prompt`
   - `flaky_test` → `retry_unchanged` (max 2)
   - `infra_issue` → `retry_unchanged` with exponential backoff
   - `timeout` → `increase_timeout`
   - `wrong_model` → `escalate_model`
3. TR-3: In `daemon.py`, after diagnosis (3.1), invoke `AdaptiveRetryEngine.plan(diagnosis_result, run_context)`. If the plan is actionable and budget allows, spawn a retry run.
4. TR-4: For `escalate_model`: modify the `input_json` to override `model_tier` for the failing phase. Reuse `recovery.py::select_model_tier()` for the escalation path.
5. TR-5: For `add_context`: use `DiagnosisResult.suggested_files` to extend the template's `context_files` list. Create a modified template in-memory (don't mutate the original file).
6. TR-6: For `split_task`: use 2.2 pipeline composition to spawn child pipelines. The `AdaptiveRetryEngine` generates the sub-task definitions based on the original task + diagnosis.
7. TR-7: Extend `pipeline_runs` table with `retry_of_run_id TEXT` and `retry_strategy TEXT` to track retry provenance.
8. TR-8: Integrate with `CostTracker` (2.4): before spawning retry, call `CostTracker.estimate_retry_cost(strategy, model)` and compare against remaining budget.

**Acceptance Criteria:**
1. AC-1: A run diagnosed with `quality_gap` automatically retries with the next model tier.
2. AC-2: A run diagnosed with `insufficient_context` retries with additional files in the prompt.
3. AC-3: A run diagnosed with `timeout` retries with 2× the original phase timeout.
4. AC-4: A retry that would exceed the budget cap is skipped, and the run escalates to human review.
5. AC-5: After 3 failed retries (max_retries=3), the system stops retrying and sets status to `escalated`.
6. AC-6: `GET /api/v1/runs/{original_run_id}` shows linked retry runs with their strategies and outcomes.

**Risks:**
- Risk: Retry strategies themselves fail, burning tokens without progress → Mitigation: Budget cap (2.4) as hard limit. Each retry costs at most 2× the original (escalation). Cap at 3 retries total.
- Risk: `split_task` produces wrong decomposition → Mitigation: Start with simple strategies (escalate_model, retry_unchanged). Task splitting is the most complex strategy — implement last, only for Opus-tier diagnosis confidence > 0.85.

**Existing Code to Leverage:**
- `recovery.py`: `RecoveryManager.handle_task_failure()` — the retry decision infrastructure (backoff, model escalation, circuit breaker). Extend rather than replace. `TaskRetryState.escalation_path` and `select_model_tier()` for model escalation.
- `recovery.py`: `ErrorClassifier.get_retry_config()` — base retry parameters per error type.
- `daemon.py`: `run_daemon()` — the post-failure block. Chain: failure → diagnosis (3.1) → adaptive retry (3.2) → spawn.
- `sequencer.py`: `PhaseDefinition.retries` and `retry_delay_seconds` — per-phase retry config. The adaptive engine can override these.
- `templates.py`: `PhaseDefinition.context_files` — the list to extend for `add_context` strategy.

**Estimated Token Cost:** Medium

---

### 3.3 — Regression Detection and Auto-Fix

**Epic:** Yes
**Estimated sub-issues:** 5
1. CI status monitor (webhook listener for `check_suite` / `status` events)
2. Breaking commit identification (`git bisect` / blame analysis)
3. Issue creation + fix pipeline spawning
4. Auto-merge gate for regression fixes (high-confidence + CI green)
5. End-to-end integration test

**Phase:** 3 (Level 4.8)
**Complexity:** XL
**Dependencies:**
- Roadmap: 2.5 (GitHub App — receives CI status webhooks), 2.1 (webhook triggers — event routing), 2.3 (confidence routing — auto-merge gate for fixes), 2.2 (pipeline composition — fix pipeline spawning)
- Code: `git_integration.py` (`GitContext._run_git()` for `git bisect`, `git blame`, `git log`), `daemon.py` (pipeline spawning), `scoring.py` (fix validation scoring), `web/api.py` (webhook endpoints from 2.1)
**Priority within phase:** 2 (critical path to Level 5 — closes the build-break-fix loop)

**Functional Requirements:**
1. FR-1: The system SHALL monitor CI status on the `main` branch after every merge (via GitHub `check_suite.completed` or `status` webhook events).
2. FR-2: When CI fails on `main`, the system SHALL identify the breaking commit(s) by analyzing the commit range since the last green CI.
3. FR-3: For each breaking commit, the system SHALL create a GitHub issue with: commit SHA, CI failure logs, affected files, and diagnosis.
4. FR-4: The system SHALL automatically spawn a coding pipeline targeting the regression (fix the specific CI failure).
5. FR-5: If the fix pipeline scores above the `auto_merge` threshold (2.3) AND CI passes on the fix branch, the system SHALL auto-merge the fix.
6. FR-6: If the fix pipeline scores below the threshold, the system SHALL create a PR and notify the team for human review.
7. FR-7: The system SHALL NOT attempt to fix regressions caused by dependency updates or infrastructure changes (classify these as `infra_issue` and escalate).
8. FR-8: The system SHALL track regression rate metrics (regressions detected, auto-fixed, escalated).

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/regression.py` with: `RegressionDetector` (listens for CI failure events, identifies breaking commits), `RegressionFixer` (spawns fix pipelines, manages the fix lifecycle).
2. TR-2: `RegressionDetector.identify_breaking_commit()`: Run `git log --oneline <last_green_sha>..HEAD` to get the commit range. For each commit, run `git show --stat <sha>` to get affected files. Use a lightweight LLM call (Haiku) to correlate CI error logs with changed files.
3. TR-3: `RegressionFixer.spawn_fix_pipeline()`: Create a `pipeline_runs` entry with: `trigger_type: regression`, `parent_run_id` (optional), and `input_json` containing the regression context (failing test, error message, affected files, suspect commit).
4. TR-4: Use the existing coding pipeline template (`coding-pipeline-v1.yaml`) with the regression context as input. The spec phase receives the CI failure analysis; the implement phase generates the fix.
5. TR-5: After the fix pipeline completes, invoke the routing engine (2.3). For fixes scoring ≥ auto_merge threshold: create branch, push, create PR, wait for CI, merge if green. Use `git_integration.py::GitContext` for all git operations.
6. TR-6: Add `regressions` table in `db.py`: `id`, `commit_sha`, `ci_run_url`, `failure_type`, `affected_files_json`, `diagnosis`, `fix_run_id`, `status` (detected/fixing/fixed/escalated), `created_at`.
7. TR-7: Wire `RegressionDetector` to the webhook trigger system (2.1) — register a trigger for `check_suite.completed` events where `conclusion = failure` and `branch = main`.
8. TR-8: Add `GET /api/v1/regressions` and `GET /api/v1/regressions/{id}` API endpoints.

**Acceptance Criteria:**
1. AC-1: A CI failure on `main` after a merge triggers regression detection within 60 seconds of the webhook.
2. AC-2: The breaking commit is correctly identified (matches the commit that introduced the failure).
3. AC-3: A GitHub issue is created with commit SHA, failure logs, and affected files.
4. AC-4: A coding pipeline is spawned targeting the regression.
5. AC-5: A high-confidence fix (score ≥ 0.95) auto-merges after CI passes on the fix branch.
6. AC-6: A low-confidence fix creates a PR for human review.
7. AC-7: Regressions from dependency updates are escalated (not auto-fixed).

**Risks:**
- Risk: Fix introduces a new regression (fix-of-fix loop) → Mitigation: Cap at 1 auto-fix attempt per regression. If the fix itself fails CI, escalate immediately. Track `fix_attempt_count` in `regressions` table.
- Risk: `git bisect` is slow on large repos → Mitigation: Use `git log` + commit-range analysis first (fast). Only fall back to `git bisect` if the range has > 10 commits.
- Risk: CI failure is flaky (not a real regression) → Mitigation: Wait for 2 consecutive failures on the same commit before triggering regression detection. Check if the failure is in the known flaky test list.

**Existing Code to Leverage:**
- `git_integration.py`: `GitContext._run_git()` — subprocess wrapper for git commands. `GitContext.create_branch()`, `commit_phase()`, `push()` — branch lifecycle for fix PRs.
- `daemon.py`: `run_daemon()` — pipeline execution for the fix. The entire daemon infrastructure handles the fix pipeline.
- `scoring.py`: `run_scoring()` — validates the fix quality before auto-merge.
- `recovery.py`: `ErrorClassifier` — classify CI failure type (test failure vs. build failure vs. infra).
- `webhooks.py` (2.1): `TriggerMatcher` — event routing for CI status webhooks.

**Estimated Token Cost:** High (XL complexity, multiple new modules, LLM calls for diagnosis, end-to-end integration)

---

### 3.4 — Fleet Monitoring Dashboard

**Epic:** Yes
**Estimated sub-issues:** 3
1. Fleet API endpoints (aggregate queries across all runs)
2. Dashboard UI (React/Vue component in existing web UI)
3. Real-time update via SSE (extend existing SSE infrastructure)

**Phase:** 3 (Level 4.8)
**Complexity:** L
**Dependencies:**
- Roadmap: 1.5 (Run Analytics — provides the data layer), 2.4 (Cost Tracking — cost burn rate data)
- Code: `web/api.py` (API endpoints, SSE streaming), `db.py` (aggregate queries), `daemon.py` (event emission for `_write_phase_event`)
**Priority within phase:** 3 (important for operational visibility but doesn't block autonomous features)

**Functional Requirements:**
1. FR-1: The dashboard SHALL show a real-time view of all active pipeline runs with live progress (phase, status, elapsed time).
2. FR-2: The dashboard SHALL show historical success/failure rates with configurable time ranges (24h, 7d, 30d).
3. FR-3: The dashboard SHALL show cost burn rate: current day's spend, trend line, budget utilization percentage.
4. FR-4: The dashboard SHALL show failure pattern clustering: group runs by error type, template, model — highlight systemic issues.
5. FR-5: The dashboard SHALL show queue depth (pending runs) and throughput (runs completed/hour).
6. FR-6: The dashboard SHALL support filtering by template, status, date range, and repository.
7. FR-7: Data SHALL refresh automatically via SSE (no manual page reload needed).

**Technical Requirements:**
1. TR-1: Add fleet aggregate API endpoints in `api.py`:
   - `GET /api/v1/fleet/status` — active runs count, queue depth, throughput
   - `GET /api/v1/fleet/metrics` — success rate, failure rate, avg duration, cost by time period
   - `GET /api/v1/fleet/failures` — failure pattern clusters (group by error_type + template_id)
   - `GET /api/v1/fleet/stream` — SSE endpoint for fleet-wide events (run started, run completed, errors)
2. TR-2: Add aggregate query methods in `db.py`:
   - `get_fleet_status() -> dict` — counts by status, queue depth
   - `get_fleet_metrics(period: str) -> dict` — aggregated metrics
   - `get_failure_clusters(period: str) -> List[dict]` — group failures by pattern
3. TR-3: The fleet SSE endpoint SHALL multiplex events from all active runs. Extend the existing `stream_run` SSE pattern from `api.py` but across all runs (subscribe by query filter, not by run_id).
4. TR-4: Dashboard UI: Add a new page/tab in the existing web UI (referenced in ROADMAP as already shipped). Create fleet overview components with charts (success rate over time, cost trend, active run cards).
5. TR-5: Use the existing `pipeline_run_events` table for real-time data and `pipeline_runs` for historical aggregates.
6. TR-6: Add caching (in-memory TTL cache, 30s) for expensive aggregate queries to avoid hammering SQLite on every dashboard refresh.

**Acceptance Criteria:**
1. AC-1: Fleet status API returns correct active run count and queue depth.
2. AC-2: Metrics API shows accurate success/failure rates matching actual DB data.
3. AC-3: Cost burn rate reflects real-time cost accumulation from active runs.
4. AC-4: Failure clustering groups 5 runs with "rate_limit" errors under the same template into a single cluster.
5. AC-5: SSE stream emits events within 2s of a run status change.
6. AC-6: Dashboard loads in < 3s with 1000 historical runs in the DB.

**Risks:**
- Risk: Expensive aggregate queries slow down SQLite under load → Mitigation: In-memory caching with 30s TTL. Add indexes on `status`, `template_id`, `created_at`. Consider materialized view (pre-computed summary table updated by daemon on each event).
- Risk: SSE connection management at scale (many connected dashboards) → Mitigation: Use a single "fleet event bus" in the server process that broadcasts to all SSE connections. Limit max connections.

**Existing Code to Leverage:**
- `web/api.py`: `stream_run()` SSE endpoint — exact pattern to extend for fleet-wide streaming.
- `db.py`: `list_pipeline_runs_filtered()`, `count_pipeline_runs()` — existing filtered query methods. Extend with aggregate versions.
- `daemon.py`: `_write_phase_event()` — event emission pattern. Fleet stream reads from the same `pipeline_run_events` table.

**Estimated Token Cost:** High (L complexity, UI work, real-time streaming, multiple API endpoints)

---

### 3.5 — Stale Detection and Proactive Maintenance

**Epic:** Yes
**Estimated sub-issues:** 4
1. Repo scanner (dependency staleness, doc drift, TODO/FIXME, coverage)
2. Scheduled scan trigger (cron-style via 2.1 webhooks or internal scheduler)
3. Issue creation + pipeline spawning for maintenance tasks
4. Configuration (scan rules, thresholds, auto-fix opt-in)

**Phase:** 3 (Level 4.8)
**Complexity:** L
**Dependencies:**
- Roadmap: 2.1 (webhook triggers — for scheduled/cron triggers), 2.3 (confidence routing — for auto-fix gating), 2.2 (pipeline composition — spawn fix pipelines for low-risk items)
- Code: `templates.py` (`PhaseDefinition.context_files` — for repo scanning), `daemon.py` (pipeline spawning), `git_integration.py` (repo operations)
**Priority within phase:** 5 (nice-to-have — valuable for codebase health but not on the critical path)

**Functional Requirements:**
1. FR-1: The system SHALL support periodic repo scans (configurable interval, default: weekly) that detect: outdated dependencies, stale documentation, TODO/FIXME comments older than N days, and test coverage gaps.
2. FR-2: Each scan type SHALL be independently configurable (enable/disable, thresholds, auto-fix opt-in).
3. FR-3: Scan results SHALL be persisted and compared against previous scans to detect deltas (new stale items, resolved items).
4. FR-4: For each detected issue, the system SHALL create a GitHub issue with: description, severity, affected file(s), and suggested remediation.
5. FR-5: For low-risk items (e.g., dependency patch update, stale TODO removal), the system SHALL optionally spawn a fix pipeline (opt-in per scan type).
6. FR-6: Fix pipelines SHALL go through confidence routing (2.3) before any changes are merged.
7. FR-7: The system SHALL provide a scan history API for trend analysis ("are we getting more or less stale over time?").

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/maintenance.py` with: `MaintenanceScanner` (orchestrates scan types), `DependencyScanner` (parses `requirements.txt`/`pyproject.toml`/`package.json`, checks PyPI/npm for updates), `DocScanner` (compares function signatures with docstrings using AST parsing), `TodoScanner` (grep-based, filters by git blame age), `CoverageScanner` (parses `coverage.xml`/`.coverage` if available).
2. TR-2: Add a `maintenance_scans` table in `db.py`: `id`, `repo_path`, `scan_type`, `findings_json`, `delta_from_previous_json`, `auto_fix_triggered`, `created_at`.
3. TR-3: Implement scan scheduling via one of:
   - (a) Internal scheduler: a lightweight timer in the daemon that fires scan pipelines at configured intervals.
   - (b) Webhook trigger with cron expression: extend 2.1 triggers with a `schedule` type that fires on a cron pattern.
   Option (b) is preferred for consistency with the webhook-driven architecture.
4. TR-4: For issue creation, use `GitHubClient` (from 2.5) or `gh` CLI to create issues with structured labels (`maintenance`, `stale-dependency`, etc.).
5. TR-5: For fix pipelines, use pipeline composition (2.2): scan pipeline's `on_complete.success` triggers a fix pipeline for each low-risk finding.
6. TR-6: Add `GET /api/v1/maintenance/scans` and `GET /api/v1/maintenance/scans/{id}` endpoints.

**Acceptance Criteria:**
1. AC-1: A weekly scan detects an outdated dependency (e.g., `requests` 2.28 → 2.31) and creates a GitHub issue.
2. AC-2: A scan detects a function whose docstring doesn't match its current signature and flags it.
3. AC-3: A TODO comment older than 30 days (configurable) is flagged with the original author and date.
4. AC-4: Low-risk dependency patch update triggers a fix pipeline when `auto_fix: true` is configured.
5. AC-5: Scan delta correctly shows "3 new findings, 1 resolved" compared to the previous scan.
6. AC-6: Scan history API shows trend data over the last 10 scans.

**Risks:**
- Risk: False positives in staleness detection (e.g., a TODO that's intentional) → Mitigation: Allow `# NOFIX` annotation to suppress specific findings. Configurable severity thresholds.
- Risk: Dependency scanner triggers unnecessary updates → Mitigation: Only flag major/minor version bumps by default. Patch-only updates are opt-in. Never auto-merge dependency updates (always human review).

**Existing Code to Leverage:**
- `templates.py`: `PhaseDefinition.context_files` — pattern for reading repo files into pipeline context.
- `git_integration.py`: `GitContext._run_git()` — git blame for TODO age detection.
- `webhooks.py` (2.1): `TriggerConfig` — extend with `schedule` type for cron-based triggers.
- Content pipeline's fact-checking phase — pattern for verification and analysis pipelines.

**Estimated Token Cost:** High (L complexity, multiple scanner implementations, scheduling, issue creation)

---

## Phase 4: Dark Factory

---

### 4.1 — Issue → Pipeline Automation

**Epic:** Yes
**Estimated sub-issues:** 5
1. Issue classifier (bug/feature/docs/refactor/research) using LLM
2. Template selector (maps classification to appropriate pipeline template)
3. Structured input extractor (parses issue body into pipeline input fields)
4. Pipeline launcher + GitHub comment posting
5. Result router (creates PR for code, posts output for content/research)

**Phase:** 4 (Level 5 — Dark Factory)
**Complexity:** XL
**Dependencies:**
- Roadmap: 2.5 (GitHub App — receives issue events), 2.3 (confidence routing — gates auto-merge of results), 2.1 (webhook triggers — event-driven launch), 3.1 (diagnosis — for failed pipeline recovery)
- Code: `schemas.py` (`TaskType` enum — maps to classification categories), `templates.py` (`TemplateEngine.list_templates()` — template discovery by tags/category), `daemon.py` (pipeline execution), `git_integration.py` (PR creation for code results)
**Priority within phase:** 1 (critical path — this IS the Level 5 promise)

**Functional Requirements:**
1. FR-1: When a GitHub issue is created (or labeled with a trigger label like `orchemist`), the system SHALL classify it into: `bug`, `feature`, `docs`, `refactor`, `research`, `content`.
2. FR-2: Classification SHALL use an LLM (Haiku for speed) analyzing the issue title, body, and labels, outputting a structured JSON with: `type`, `confidence`, `affected_files` (if identifiable), `requirements` (extracted from the issue body).
3. FR-3: Based on the classification, the system SHALL select the most appropriate pipeline template (e.g., `coding-pipeline-v1` for bugs/features, `content-pipeline` for docs, `research-pipeline` for research).
4. FR-4: The system SHALL extract structured inputs from the issue body and map them to the selected template's `config_schema` fields.
5. FR-5: The system SHALL post a GitHub comment on the issue: "🏭 Orchemist pipeline started — tracking in run #abc123" with a link to the run details.
6. FR-6: On pipeline completion:
   - Code results → create a PR linked to the issue.
   - Content/research results → post the output as an issue comment.
   - Failed pipeline → post failure summary and diagnosis.
7. FR-7: The system SHALL NOT process issues that are already being processed (deduplication by issue number).
8. FR-8: Classification confidence below a configurable threshold (default 0.70) SHALL escalate to human review instead of auto-processing.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/issue_automation.py` with: `IssueClassifier` (LLM-based classification), `TemplateSelector` (maps classification to template), `InputExtractor` (parses issue body into structured input), `IssueAutomation` (orchestrates the full flow).
2. TR-2: `IssueClassifier`: Use a Haiku-tier agent with a structured prompt that outputs JSON: `{"type": "bug|feature|docs|refactor|research", "confidence": 0.91, "affected_files": ["export.py"], "requirements": ["fix truncation at 10K rows"], "complexity_estimate": "S|M|L"}`.
3. TR-3: `TemplateSelector`: Maintain a mapping (configurable in `config.toml` or a `template-mappings.yaml`):
   - `bug` → `coding-pipeline-v1` (with `task_type: bug_fix` context)
   - `feature` → `coding-pipeline-v1` (with `task_type: feature` context)
   - `docs` → `content-pipeline` (or a dedicated docs pipeline)
   - `research` → `research-pipeline`
4. TR-4: `InputExtractor`: Parse the issue body using LLM-assisted extraction. The prompt includes the template's `config_schema` and the issue body, outputting a JSON that conforms to the schema.
5. TR-5: In the GitHub App webhook handler (2.5), when an `issues.opened` or `issues.labeled` event is received: call `IssueAutomation.process(issue_payload)`. This: classifies → selects template → extracts input → launches pipeline → posts comment.
6. TR-6: Add `issue_pipeline_map` table in `db.py`: `issue_number`, `repo`, `classification_type`, `classification_confidence`, `template_id`, `run_id`, `status`, `created_at`. Dedup check: reject if row exists for this `issue_number + repo` with status != `failed`.
7. TR-7: After pipeline completion (in daemon), if `trigger_type = github_issue`: create PR (code) or post comment (content) via `GitHubClient` (2.5).
8. TR-8: Add `GET /api/v1/issues` and `GET /api/v1/issues/{number}/runs` endpoints.

**Acceptance Criteria:**
1. AC-1: An issue titled "CSV export truncates rows > 10,000" is classified as `bug` with confidence ≥ 0.85.
2. AC-2: The bug classification selects `coding-pipeline-v1` and extracts `affected_files: ["export.py"]` from the issue body.
3. AC-3: A "🏭 Pipeline started" comment appears on the issue within 30 seconds of creation.
4. AC-4: On successful pipeline completion, a PR is created linked to the issue (with `Fixes #892` in the description).
5. AC-5: On failed pipeline, a failure summary with diagnosis is posted as an issue comment.
6. AC-6: A duplicate issue trigger (same issue number) is rejected.
7. AC-7: Classification confidence of 0.60 escalates to human review (no auto-processing).

**Risks:**
- Risk: LLM misclassifies issue type (e.g., a bug classified as a feature) → Mitigation: Conservative confidence threshold (0.70+). Allow human override via issue labels. Track classification accuracy metrics.
- Risk: Security: malicious issue body could inject into the pipeline prompt → Mitigation: Sanitize issue body (strip HTML, limit length to 5000 chars, escape template markers). Never include raw issue body in executable commands.
- Risk: Pipeline changes wrong files (misidentified affected_files) → Mitigation: CI must pass. Review pipeline includes file-scope validation. Confidence routing (2.3) gates the merge.

**Existing Code to Leverage:**
- `schemas.py`: `TaskType` enum — existing classification categories (CONTENT, CODE, RESEARCH, REVIEW, TRIAGE, ANALYSIS). Extend or map issue types to TaskTypes.
- `templates.py`: `TemplateEngine.list_templates()` — returns template metadata including `tags` which can be used for template selection.
- `templates.py`: `PipelineTemplate.config_schema` — the schema for structured input extraction.
- `openclaw_executor.py`: `OpenClawExecutor` — spawn the classification agent.
- `git_integration.py`: `GitContext.create_pr()` — PR creation with `gh pr create`.
- `github_app.py` (2.5): `GitHubClient.post_issue_comment()` — GitHub API interaction.

**Estimated Token Cost:** High (XL complexity, multiple LLM calls, full end-to-end integration)

---

### 4.2 — Meta-Orchestration (Pipelines Spawning Pipelines)

**Epic:** Yes
**Estimated sub-issues:** 4
1. Meta-pipeline template format (`meta` phase type, task decomposition schema)
2. Task decomposer agent (breaks high-level goals into sub-tasks)
3. Child pipeline orchestrator (spawns, tracks, waits for child pipelines)
4. Result aggregator (merges outputs from multiple child pipelines, resolves conflicts)

**Phase:** 4 (Level 5)
**Complexity:** XL
**Dependencies:**
- Roadmap: 2.2 (pipeline composition — child pipeline spawning), 3.2 (adaptive retry — handles child pipeline failures), 2.4 (cost tracking — aggregate budget across children)
- Code: `sequencer.py` (`PhaseSequencer` — parallel wave execution, dependency graphs), `daemon.py` (DB-backed status tracking), `templates.py` (`PhaseDefinition`, `PipelineTemplate`), `db.py` (`pipeline_runs` with `parent_run_id` from 2.2)
**Priority within phase:** 3 (powerful capability but requires solid foundation from 2.2 and 3.2)

**Functional Requirements:**
1. FR-1: The system SHALL support a new phase type `meta` that decomposes a high-level goal into sub-tasks.
2. FR-2: The `meta` phase SHALL use an LLM (Opus-tier for complex decomposition) to analyze the goal and produce a list of sub-tasks with: title, description, dependencies (which sub-tasks must complete first), template to use, and estimated complexity.
3. FR-3: For each sub-task, the system SHALL spawn an independent child pipeline with the appropriate template and inputs.
4. FR-4: Child pipelines SHALL execute with dependency ordering — a child blocked on another child waits until the dependency completes.
5. FR-5: The system SHALL aggregate results from all child pipelines into a unified output.
6. FR-6: When child pipelines produce conflicting outputs (e.g., both modify the same file), the system SHALL detect the conflict and either resolve it automatically (if possible) or escalate.
7. FR-7: The aggregate cost of all child pipelines SHALL be tracked under the parent run's budget.
8. FR-8: If any critical child pipeline fails, the parent meta-pipeline SHALL pause and offer retry/skip/abort options.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/meta.py` with: `TaskDecomposer` (LLM-based goal decomposition), `MetaOrchestrator` (spawns and manages child pipelines), `ResultAggregator` (merges child outputs).
2. TR-2: Extend `PhaseDefinition` in `templates.py` with `phase_type: str = "standard"` (values: `standard`, `meta`, `command`). When `phase_type == "meta"`, the sequencer delegates to `MetaOrchestrator` instead of the normal executor.
3. TR-3: `TaskDecomposer` prompt: Include the goal, available templates (from `TemplateEngine.list_templates()`), and existing codebase context. Output: JSON array of `{"id": "sub-1", "title": "...", "template": "coding-pipeline-v1", "input": {...}, "depends_on": ["sub-0"]}`.
4. TR-4: `MetaOrchestrator.execute()`: Build a DAG from sub-task dependencies. Execute in topological waves (reuse the wave execution pattern from `PhaseSequencer._execute_wave_parallel()`). For each sub-task, launch a child pipeline via `daemon.py` subprocess and poll for completion.
5. TR-5: `ResultAggregator`: For code tasks, collect all file changes and detect conflicts (same file modified by multiple children). For content tasks, concatenate outputs with section headers.
6. TR-6: Extend `pipeline_runs` table: add `meta_task_id TEXT` (links child runs to their sub-task definition), `depends_on_run_ids TEXT` (JSON array of run IDs this child depends on).
7. TR-7: Budget propagation: parent's remaining budget is divided among children based on estimated complexity. Each child enforces its allocated sub-budget via `CostTracker` (2.4).
8. TR-8: Add `GET /api/v1/runs/{run_id}/subtasks` endpoint showing decomposition and child statuses.

**Acceptance Criteria:**
1. AC-1: A meta-pipeline with goal "Build a REST API for user management" decomposes into ≥ 3 sub-tasks (e.g., data model, endpoints, tests).
2. AC-2: Sub-tasks with dependencies execute in correct order (data model before endpoints).
3. AC-3: Independent sub-tasks execute in parallel.
4. AC-4: A file conflict between two sub-tasks is detected and reported.
5. AC-5: Aggregate cost across all children is tracked under the parent run.
6. AC-6: A failed critical sub-task pauses the meta-pipeline (does not auto-continue).

**Risks:**
- Risk: LLM produces bad decomposition (wrong sub-tasks, wrong dependencies) → Mitigation: Include a decomposition review phase (LLM reviews its own decomposition). Limit max sub-tasks to 10. Start with Opus for decomposition quality.
- Risk: Combinatorial explosion of child pipelines → Mitigation: Max 10 children per meta-pipeline. Budget cap enforced per-child and aggregate.
- Risk: Conflict resolution between child outputs is extremely hard for non-trivial code changes → Mitigation: v1 detects conflicts and escalates. Automated conflict resolution is a future enhancement.

**Existing Code to Leverage:**
- `sequencer.py`: `PhaseSequencer._execute_wave_parallel()` — the parallel wave execution pattern with `ThreadPoolExecutor`, dependency ordering via `TemplateEngine.get_execution_order()`. The meta-orchestrator uses the same DAG pattern but at pipeline-run level instead of phase level.
- `sequencer.py`: `PhaseSequencer.phase_outputs` — the output accumulation pattern. `ResultAggregator` follows the same pattern.
- `daemon.py`: `run_daemon()` — subprocess spawning for child pipelines. `_on_phase_complete()` pattern for tracking child progress.
- `db.py`: `pipeline_runs` — child run tracking with `parent_run_id` (from 2.2).
- `templates.py`: `TemplateEngine.list_templates()` — template discovery for the decomposer's "available templates" context.

**Estimated Token Cost:** High (XL complexity, Opus-tier decomposition, multiple child pipelines, conflict detection)

---

### 4.3 — Deployment Integration

**Epic:** Yes
**Estimated sub-issues:** 4
1. Deploy pipeline template format (trigger conditions, gates, rollback config)
2. Deploy executor (Docker, K8s, serverless, static site adapters)
3. Health check + smoke test runner (post-deploy validation)
4. Rollback mechanism (automatic rollback on health check failure)

**Phase:** 4 (Level 5)
**Complexity:** L
**Dependencies:**
- Roadmap: 2.2 (pipeline composition — deploy chains), 3.3 (regression detection — triggers rollback), 2.3 (confidence routing — deploy gating)
- Code: `templates.py` (`PhaseDefinition` with `command` task_type — for shell-based deploy commands), `sequencer.py` (command phase execution), `daemon.py` (pipeline lifecycle)
**Priority within phase:** 4 (last-mile delivery; valuable but the system provides most value even without automated deploy)

**Functional Requirements:**
1. FR-1: Pipeline templates SHALL support a `deploy` block defining staging and production deployment configurations.
2. FR-2: Each deploy stage SHALL support: `trigger` (on_merge, on_staging_success, manual), `pipeline` (template to run), `gates` (health_check, smoke_tests, soak_time), and `rollback` configuration.
3. FR-3: The deploy pipeline SHALL execute: build → deploy → health check → smoke test → (optional soak) → promote.
4. FR-4: If health checks or smoke tests fail after deployment, the system SHALL automatically execute the rollback pipeline.
5. FR-5: Rollback SHALL be triggered by: health check failure, error rate spike (if monitoring is configured), or manual trigger.
6. FR-6: The system SHALL enforce the non-negotiable guardrail: no production deploy without staging soak period.
7. FR-7: Deploy status SHALL be tracked in the DB and visible in the fleet dashboard (3.4).
8. FR-8: The system SHALL support multiple deployment targets: Docker (via `docker build/push/deploy`), shell commands (generic), and webhook-based triggers (for external CD systems).

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/deploy.py` with: `DeployConfig` (parsed from template YAML), `DeployExecutor` (runs deploy commands), `HealthChecker` (runs health check endpoints), `RollbackManager` (executes rollback on failure).
2. TR-2: Extend `PipelineTemplate` in `templates.py` with `deploy: Optional[DeployConfig]`.
3. TR-3: Deploy execution uses the existing `command` phase type from `PhaseDefinition` (see `command_executor.py`). Each deploy step is a command phase with security allowlist.
4. TR-4: `HealthChecker`: HTTP GET to a configurable endpoint, expect 200. Retry up to N times with configurable interval. Report as pass/fail.
5. TR-5: `RollbackManager`: On health check failure, invoke a rollback pipeline (defined in `deploy.rollback.pipeline`). The rollback pipeline is a standard coding/command pipeline that reverts the deployment.
6. TR-6: Soak timer: After staging deploy + health check, wait N minutes (configurable, default 60) before promoting to production. Implement as a `wait` phase with a timer.
7. TR-7: Add `deployments` table in `db.py`: `id`, `run_id`, `stage` (staging/production), `target`, `status` (deploying/deployed/healthy/unhealthy/rolled_back), `health_check_result`, `deployed_at`, `rolled_back_at`.
8. TR-8: Wire into pipeline composition (2.2): after a code pipeline merges, its `on_complete.success` triggers the deploy pipeline.

**Acceptance Criteria:**
1. AC-1: A code pipeline completion triggers a staging deploy pipeline via `on_complete`.
2. AC-2: Health check confirms the deployment is healthy (HTTP 200 from `/health`).
3. AC-3: Smoke tests pass against the staging environment.
4. AC-4: After a 60-minute soak, the production deploy is triggered.
5. AC-5: A failed health check triggers automatic rollback and sets deployment status to `rolled_back`.
6. AC-6: Production deploy without prior staging soak is rejected.

**Risks:**
- Risk: Deploy command execution is security-sensitive (arbitrary shell commands) → Mitigation: Use `PhaseDefinition.allowed_commands` security allowlist. Never run unsanitized LLM output as deploy commands. Deploy commands are human-authored in templates, not LLM-generated.
- Risk: Health check false positives (app reports healthy but is actually broken) → Mitigation: Support multiple health check endpoints. Add smoke tests as a second validation layer. Soak period catches delayed failures.
- Risk: Rollback itself fails → Mitigation: Track rollback status separately. If rollback fails, alert immediately (critical priority notification). Consider keeping the previous deployment artifact available for manual intervention.

**Existing Code to Leverage:**
- `templates.py`: `PhaseDefinition.command` and `allowed_commands` — the command execution infrastructure already exists for shell-based deploy steps.
- `sequencer.py`: Command phase execution — `PhaseSequencer` already handles `task_type=command` phases via `command_executor.py`.
- `daemon.py`: Pipeline composition (2.2) — deploy pipelines are triggered as child pipelines after merge.
- `recovery.py`: Circuit breaker pattern — could be adapted for deployment circuit breaking (too many failed deploys → stop auto-deploying).

**Estimated Token Cost:** Medium (L complexity but mostly configuration/plumbing, minimal LLM usage)

---

### 4.4 — Trust Calibration Engine

**Epic:** Yes
**Estimated sub-issues:** 3
1. Trust profile data model + initial calibration algorithm
2. Dynamic threshold adjustment (per-repo, per-template, per-task-type)
3. Trust decay + bootstrapping logic

**Phase:** 4 (Level 5)
**Complexity:** L
**Dependencies:**
- Roadmap: 1.5 (Run Analytics — historical data), 2.3 (confidence routing — trust profiles feed into routing thresholds), 3.3 (regression detection — regressions decay trust)
- Code: `scoring.py` (`weighted_score` — the metric trust calibrates against), `recovery.py` (`CircuitBreakerState` — conceptual pattern for adaptive trust), `db.py` (historical run data)
**Priority within phase:** 2 (critical path — without dynamic trust, thresholds are either too conservative or too aggressive)

**Functional Requirements:**
1. FR-1: The system SHALL maintain a trust profile for each unique (repo, template, task_type) tuple.
2. FR-2: Trust profiles SHALL include: `auto_merge_threshold`, `human_review_threshold`, `current_trust_score`, `total_runs`, `successful_merges`, `regressions`, `reverted_prs`, `last_updated`.
3. FR-3: After each pipeline run, the trust profile SHALL be updated:
   - Successful merge (no regression within N days) → trust increases.
   - Regression detected → trust decreases significantly.
   - Reverted PR → trust decreases.
   - Scoring above threshold but human overrode to reject → trust threshold increases.
4. FR-4: New repos/templates SHALL start with conservative defaults: `auto_merge_threshold: 0.98`, `human_review_threshold: 0.85`. Trust must be earned over ≥ 10 successful runs before thresholds relax.
5. FR-5: Trust SHALL decay over time if a repo has no activity (idle decay rate: 0.01 per week, capped at baseline).
6. FR-6: The system SHALL expose trust profiles via API for transparency and auditing.
7. FR-7: Humans SHALL be able to manually override trust profiles (raise/lower thresholds).
8. FR-8: Trust calibration SHALL log every adjustment with reason and timestamp for auditing.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/trust.py` with: `TrustProfile` (dataclass stored in DB), `TrustCalibrator` (adjusts profiles based on outcomes), `TrustConfig` (global calibration parameters: learning rate, decay rate, bootstrap threshold).
2. TR-2: Add `trust_profiles` table in `db.py`: `id`, `repo`, `template_id`, `task_type`, `auto_merge_threshold`, `human_review_threshold`, `trust_score`, `total_runs`, `successful_merges`, `regressions`, `reverted_prs`, `last_run_at`, `created_at`, `updated_at`. Unique constraint on `(repo, template_id, task_type)`.
3. TR-3: Add `trust_adjustments` table: `id`, `profile_id`, `adjustment_type` (run_success, regression, revert, human_override, decay), `old_threshold`, `new_threshold`, `reason`, `created_at`.
4. TR-4: `TrustCalibrator.update_after_run(run_id, outcome)`: Called after pipeline completion (in `daemon.py`). Computes new trust score using an exponential moving average: `new_score = α × outcome_score + (1 - α) × old_score` where `α = learning_rate` (default 0.1).
5. TR-5: Threshold adjustment formula: `auto_merge_threshold = 1.0 - (trust_score × trust_range)` where `trust_range` is the gap between the most conservative (0.98) and most aggressive (0.85) thresholds. Higher trust → lower threshold → more auto-merges.
6. TR-6: In `routing.py` (2.3), the `RoutingEngine` SHALL query the trust profile for the current (repo, template, task_type) tuple and use the profile's thresholds instead of the template's static thresholds (if a profile exists).
7. TR-7: Decay: Run a periodic job (daily, via cron or heartbeat) that applies `trust_score -= decay_rate` for profiles with no activity in the last 7 days. Cap at a baseline minimum (0.3).
8. TR-8: Add API endpoints: `GET /api/v1/trust/profiles`, `GET /api/v1/trust/profiles/{id}`, `PUT /api/v1/trust/profiles/{id}` (manual override), `GET /api/v1/trust/adjustments`.

**Acceptance Criteria:**
1. AC-1: A new repo starts with `auto_merge_threshold: 0.98` — first 10 runs require human review.
2. AC-2: After 15 successful merges with no regressions, `auto_merge_threshold` decreases to ≤ 0.93.
3. AC-3: A regression event increases `auto_merge_threshold` by at least 0.03.
4. AC-4: An idle repo's trust decays over 4 weeks (trust_score decreases).
5. AC-5: Routing engine uses trust profile thresholds when available (overrides template defaults).
6. AC-6: Manual override via API correctly adjusts thresholds and logs the adjustment.
7. AC-7: Trust adjustments are auditable via `GET /api/v1/trust/adjustments`.

**Risks:**
- Risk: Trust calibration is too slow (takes too many runs to earn trust) → Mitigation: Configurable `learning_rate`. Start with 0.1 (moderate speed). Allow teams to override for trusted repos.
- Risk: A single regression over-penalizes trust → Mitigation: Severity-weighted penalties. A flaky test regression penalizes less than a production outage. Allow "regression dismissed" action that doesn't penalize trust.
- Risk: Gaming (manually approving everything to build trust) → Mitigation: Trust is verified by post-merge outcomes (no regressions), not just approvals. Human overrides that bypass scoring are tracked separately.

**Existing Code to Leverage:**
- `recovery.py`: `CircuitBreakerState` — the same concept of adaptive thresholds based on failure rate. `record_success()` / `record_failure()` pattern maps directly to trust adjustment.
- `scoring.py`: `weighted_score` — the metric that trust thresholds are applied to.
- `routing.py` (2.3): `RoutingEngine.evaluate()` — the integration point. Currently uses static thresholds from template config; extend to query trust profiles first.
- `db.py`: `pipeline_runs` — historical run data for computing initial trust profiles.

**Estimated Token Cost:** Medium

---

### 4.5 — Audit Trail and Compliance

**Epic:** Yes
**Estimated sub-issues:** 4
1. Audit event model + append-only log implementation
2. LLM call logging (prompt, response, model, tokens, cost per call)
3. Decision provenance chain (trigger → classification → pipeline → scoring → routing → merge)
4. Export (PDF/JSON) + API endpoints

**Phase:** 4 (Level 5)
**Complexity:** L
**Dependencies:**
- Roadmap: All previous phases (this captures everything they produce)
- Code: `openclaw_executor.py` (LLM call metadata), `scoring.py` (scoring decisions), `daemon.py` (pipeline lifecycle events), `db.py` (existing event tables), `routing.py` (2.3 — routing decisions), `git_integration.py` (merge decisions)
**Priority within phase:** 3 (required for production use in regulated environments; not on the critical path for autonomous capability)

**Functional Requirements:**
1. FR-1: The system SHALL maintain an immutable, append-only audit log of every automated action.
2. FR-2: Audit events SHALL include: trigger events (what started the pipeline), every LLM call (prompt hash, response hash, model, tokens, cost), every scoring decision (rubric, score, verdict), every routing decision (tier, action, justification), every approval (human or automated), and every git operation (commit, push, merge, PR creation).
3. FR-3: Each audit event SHALL include: `event_id` (UUID), `timestamp`, `run_id`, `actor` (system/human/agent), `action`, `details`, `parent_event_id` (for provenance chain).
4. FR-4: The audit log SHALL be tamper-evident: each entry includes a hash of the previous entry (hash chain).
5. FR-5: The system SHALL support exporting the full audit trail for a run as JSON and PDF.
6. FR-6: The system SHALL support querying the audit trail by: run_id, time range, actor, action type.
7. FR-7: LLM call logging SHALL NOT store full prompt/response text by default (privacy/size concerns) — store hashes and word counts. Full text logging SHALL be opt-in via configuration.
8. FR-8: Audit data SHALL be retained for a configurable period (default: 1 year).

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/audit.py` with: `AuditLogger` (singleton, append-only writes), `AuditEvent` (dataclass: event_id, timestamp, run_id, actor, action, details_json, prev_hash, current_hash), `AuditExporter` (JSON/PDF export).
2. TR-2: Add `audit_log` table in `db.py`: `event_id TEXT PRIMARY KEY`, `timestamp TIMESTAMP`, `run_id TEXT`, `actor TEXT`, `action TEXT`, `details_json TEXT`, `prev_hash TEXT`, `current_hash TEXT`. **This table has NO UPDATE or DELETE operations.** Add a trigger (SQLite trigger) that prevents UPDATE/DELETE.
3. TR-3: Instrument `OpenClawExecutor.execute()`: Before each `_run_session()` call, emit an audit event with: `action: llm_call_start`, `details: {model, prompt_hash, prompt_length}`. After completion: `action: llm_call_complete`, `details: {model, response_hash, response_length, tokens, cost}`.
4. TR-4: Instrument `daemon.py`: Emit audit events for: `pipeline_start`, `phase_start`, `phase_complete`, `scoring_complete`, `routing_decision`, `pipeline_complete`.
5. TR-5: Instrument `git_integration.py`: Emit audit events for: `branch_created`, `commit`, `push`, `pr_created`, `merge`.
6. TR-6: Hash chain implementation: `current_hash = SHA256(prev_hash + event_id + timestamp + action + details_hash)`. The first event in a run uses a null prev_hash.
7. TR-7: `AuditExporter.to_json(run_id)`: Returns the full ordered event chain for a run. `AuditExporter.to_pdf(run_id)`: Generates a formatted PDF (use `weasyprint` or `reportlab` — or simpler: generate Markdown → PDF via `pandoc`).
8. TR-8: Add API endpoints: `GET /api/v1/audit/{run_id}`, `GET /api/v1/audit` (with query params), `GET /api/v1/audit/{run_id}/export?format=json|pdf`.
9. TR-9: Add retention job: periodic cleanup of audit events older than the retention period. Use soft-delete (mark as archived) rather than hard delete to maintain hash chain integrity.

**Acceptance Criteria:**
1. AC-1: A complete pipeline run produces a full audit trail with ≥ 10 events (start, phases, scoring, routing, complete).
2. AC-2: The hash chain is verifiable: recomputing hashes from events matches stored hashes.
3. AC-3: Tampering with an audit event (modifying `details_json`) is detectable by hash chain verification.
4. AC-4: JSON export for a run contains the complete ordered event list with all metadata.
5. AC-5: PDF export is human-readable with: run summary, phase timeline, scoring results, routing decision, git operations.
6. AC-6: `audit_log` table rejects UPDATE and DELETE operations (SQLite trigger fires).
7. AC-7: LLM call events include prompt hash and token count (but not full prompt text by default).

**Risks:**
- Risk: Audit logging adds latency to every operation → Mitigation: Use async/buffered writes. Batch audit events and flush periodically (every 1s or every 10 events). The audit write must never block pipeline execution.
- Risk: Audit table grows very large → Mitigation: Retention policy with archival. Consider partitioning by month. Hashes allow verification even after archival.
- Risk: PDF generation dependency adds complexity → Mitigation: Start with JSON-only export. PDF is a v2 enhancement. Or use markdown-to-PDF via pandoc (already available on most systems).

**Existing Code to Leverage:**
- `db.py`: `pipeline_run_events` table — the existing event table is a lightweight precursor. Audit events are a superset with hash chain and broader coverage.
- `daemon.py`: `_write_phase_event()` — the event emission pattern. Extend this to also emit audit events, or have `AuditLogger` subscribe to the same lifecycle callbacks.
- `openclaw_executor.py`: `_run_session()` — returns `(output_text, tokens_consumed)`. The entry/exit points are the instrumentation hooks for LLM call logging.
- `scoring.py`: `run_scoring()` — returns `(passed, weighted_score)`. Emit audit event after scoring with verdict and score.
- `git_integration.py`: `GitContext.commit_phase()`, `push()` — each method is an audit event source.

**Estimated Token Cost:** Medium (L complexity, but mostly plumbing/instrumentation — minimal LLM usage)

---

### 4.6 — Multi-Repo Orchestration

**Epic:** Yes
**Estimated sub-issues:** 5
1. Repo registry + configuration (register repos with their pipeline configs)
2. Cross-repo dependency tracker (API change in A → client update in B)
3. Coordinated PR management (atomic PR groups)
4. Monorepo workspace support (per-package pipeline configs)
5. Shared secrets management

**Phase:** 4 (Level 5)
**Complexity:** XL
**Dependencies:**
- Roadmap: 2.5 (GitHub App — multi-repo installation), 4.1 (issue automation — per-repo issue handling), 4.3 (deployment integration — per-repo deploy configs)
- Code: `git_integration.py` (`GitContext` — currently single-repo, needs multi-repo support), `templates.py` (`TemplateEngine` — template search paths are single-project), `daemon.py` (pipeline execution), `db.py` (run records)
**Priority within phase:** 5 (nice-to-have for the initial Level 5 milestone; most value comes from single-repo automation first)

**Functional Requirements:**
1. FR-1: The system SHALL maintain a registry of managed repositories, each with: repo URL, default branch, pipeline templates, trigger configurations, trust profile, and deploy configuration.
2. FR-2: The system SHALL detect cross-repo dependencies: when a pipeline modifies a public API (function signature, REST endpoint, schema), it SHALL identify downstream repos that depend on it.
3. FR-3: For cross-repo changes, the system SHALL create coordinated PRs: a PR in each affected repo, opened together, with cross-references.
4. FR-4: Coordinated PRs SHALL enforce atomic merge: all PRs merge together or none merge (to prevent breaking changes from partial merges).
5. FR-5: The system SHALL support monorepo workspaces: different pipeline configurations per package/workspace within the same repo.
6. FR-6: The system SHALL provide a centralized secrets manager for pipeline credentials (API keys, deploy tokens), scoped per repo.
7. FR-7: The fleet dashboard (3.4) SHALL show per-repo status with cross-repo dependency visualization.

**Technical Requirements:**
1. TR-1: Create `src/orchestration_engine/multi_repo.py` with: `RepoRegistry` (CRUD for repo configurations), `DependencyTracker` (cross-repo dependency analysis), `CoordinatedPRManager` (atomic PR groups), `SecretsManager` (encrypted key-value store).
2. TR-2: Add `repos` table in `db.py`: `id`, `url`, `name`, `default_branch`, `template_config_json`, `trigger_config_json`, `trust_profile_id`, `deploy_config_json`, `created_at`, `updated_at`.
3. TR-3: Add `cross_repo_deps` table: `id`, `source_repo_id`, `target_repo_id`, `dependency_type` (api, schema, package), `source_path`, `target_path`, `detected_at`.
4. TR-4: Add `coordinated_prs` table: `group_id`, `repo_id`, `pr_number`, `status` (pending/merged/rejected), `created_at`. Constraint: all PRs in a group must have the same final status.
5. TR-5: `DependencyTracker`: Use LLM analysis (Sonnet-tier) to detect API surface changes by comparing `git diff` against known dependency contracts. Store discovered dependencies in `cross_repo_deps`.
6. TR-6: `CoordinatedPRManager`: Create PRs in all affected repos. Add cross-reference comments. Before merge, check all PRs in the group are approved and CI green. Merge all atomically (sequential merges with rollback on failure).
7. TR-7: Extend `git_integration.py::GitContext` with `repo_path` parameter (currently defaults to CWD). Support creating a `GitContext` per managed repo.
8. TR-8: `SecretsManager`: Store secrets in an encrypted SQLite table (use `cryptography.fernet` with a master key stored in a file with 0600 permissions). Scope secrets by repo_id. Inject secrets as environment variables when spawning daemon processes.
9. TR-9: Monorepo support: Extend `TemplateEngine` to accept a `workspace` parameter. Different template search paths per workspace within a monorepo. Pipeline triggers can be scoped to specific workspace paths (e.g., `paths: ["packages/api/**"]`).
10. TR-10: Add API endpoints: `GET/POST/PUT/DELETE /api/v1/repos`, `GET /api/v1/repos/{id}/deps`, `GET /api/v1/repos/{id}/runs`, `POST /api/v1/coordinated-prs`.

**Acceptance Criteria:**
1. AC-1: A repo can be registered via `POST /api/v1/repos` with its pipeline configuration.
2. AC-2: A change to `repo-A/api.py` that modifies a public function signature triggers a coordinated PR in `repo-B` (the downstream consumer).
3. AC-3: Coordinated PRs are either all merged or all rejected (atomic guarantee).
4. AC-4: In a monorepo, a change to `packages/api/` triggers the API pipeline, not the frontend pipeline.
5. AC-5: Secrets stored via the API are encrypted at rest and accessible only to pipelines for the scoped repo.
6. AC-6: Fleet dashboard shows per-repo status and cross-repo dependency graph.

**Risks:**
- Risk: Cross-repo dependency detection is imprecise (LLM may miss or hallucinate dependencies) → Mitigation: Start with explicit dependency declarations (human-configured). LLM-detected dependencies are "suggested" and require human confirmation before auto-triggering.
- Risk: Atomic merge across repos is extremely hard (GitHub doesn't support cross-repo transactions) → Mitigation: Implement as sequential merges with rollback. If merge N+1 fails, revert merges 1..N. This is not truly atomic but is the best possible with GitHub's API.
- Risk: Secrets management is security-critical → Mitigation: Use established encryption libraries (`cryptography.fernet`). Master key in file with restricted permissions. Never log secrets. Audit all secret access.

**Existing Code to Leverage:**
- `git_integration.py`: `GitContext` — the entire class needs to be extended to accept `repo_path` as a parameter instead of defaulting to CWD. Methods `_run_git()`, `create_branch()`, `commit_phase()`, `push()` all need the `cwd=repo_path` parameter.
- `templates.py`: `TemplateEngine.get_search_paths()` — currently returns fixed directories. Extend to include per-repo template directories.
- `github_app.py` (2.5): `GitHubClient` — multi-repo installation. The App can post to any installed repo.
- `db.py`: `pipeline_runs` — extend with `repo_id` field to associate runs with repos.
- `trust.py` (4.4): `TrustProfile` — already keyed by (repo, template, task_type). Multi-repo is a natural extension.

**Estimated Token Cost:** High (XL complexity, multiple new systems, security-sensitive, cross-repo coordination)

---

## Summary: Issue Breakdown by Phase

| Phase | Item | Complexity | Sub-Issues | Priority | Dependencies |
|-------|------|-----------|------------|----------|-------------|
| **2** | 2.1 Webhook Triggers | L | 4 | 1 | — |
| **2** | 2.2 Pipeline Composition | M | 3 | 2 | 2.1 |
| **2** | 2.3 Confidence Routing | M | 3 | 2 | 1.3, 1.5 |
| **2** | 2.4 Cost Tracking | M | 3 | 3 | — |
| **2** | 2.5 GitHub App | L | 5 | 4 | 2.1, 2.2 |
| **3** | 3.1 Failure Diagnosis | L | 3 | 1 | 1.5 |
| **3** | 3.2 Adaptive Retry | L | 3 | 2 | 3.1, 2.2, 2.4 |
| **3** | 3.3 Regression Detection | XL | 5 | 2 | 2.5, 2.1, 2.3 |
| **3** | 3.4 Fleet Dashboard | L | 3 | 3 | 1.5, 2.4 |
| **3** | 3.5 Proactive Maintenance | L | 4 | 5 | 2.1, 2.3, 2.2 |
| **4** | 4.1 Issue Automation | XL | 5 | 1 | 2.5, 2.3, 2.1 |
| **4** | 4.2 Meta-Orchestration | XL | 4 | 3 | 2.2, 3.2, 2.4 |
| **4** | 4.3 Deploy Integration | L | 4 | 4 | 2.2, 3.3, 2.3 |
| **4** | 4.4 Trust Calibration | L | 3 | 2 | 1.5, 2.3, 3.3 |
| **4** | 4.5 Audit Trail | L | 4 | 3 | All previous |
| **4** | 4.6 Multi-Repo | XL | 5 | 5 | 2.5, 4.1, 4.3 |

**Total sub-issues across all items: ~61**

## Critical Path (in implementation order)

```
2.1 Webhook Triggers ──► 2.2 Pipeline Composition ──► 2.3 Confidence Routing ──► 2.4 Cost Tracking
                                                                                        │
     ┌──────────────────────────────────────────────────────────────────────────────────┘
     ▼
2.5 GitHub App ──► 3.1 Failure Diagnosis ──► 3.2 Adaptive Retry ──► 3.3 Regression Detection
                                                                            │
     ┌──────────────────────────────────────────────────────────────────────┘
     ▼
4.1 Issue Automation ──► 4.4 Trust Calibration ──► 4.2 Meta-Orchestration
                                                          │
                                                          ▼
                                                   4.3 Deploy Integration ──► 4.5 Audit Trail ──► 4.6 Multi-Repo
```

## New Files to Create (Summary)

| Module | Phase | Purpose |
|--------|-------|---------|
| `webhooks.py` | 2.1 | Webhook receiver, trigger matching, event routing |
| `routing.py` | 2.3 | Confidence-based routing engine |
| `cost_tracker.py` | 2.4 | Cost computation, budget enforcement |
| `github_app.py` | 2.5 | GitHub App auth, client, webhook handler |
| `diagnosis.py` | 3.1 | Failed pipeline diagnosis engine |
| `adaptive_retry.py` | 3.2 | Strategy-based retry engine |
| `regression.py` | 3.3 | Regression detection and auto-fix |
| `maintenance.py` | 3.5 | Proactive repo maintenance scanning |
| `issue_automation.py` | 4.1 | Issue classification, template selection, input extraction |
| `meta.py` | 4.2 | Meta-orchestration, task decomposition |
| `deploy.py` | 4.3 | Deployment integration, health checks, rollback |
| `trust.py` | 4.4 | Trust profile management, calibration |
| `audit.py` | 4.5 | Immutable audit logging, export |
| `multi_repo.py` | 4.6 | Repo registry, cross-repo deps, coordinated PRs |

## DB Schema Extensions (Summary)

| Table | Phase | Key Columns |
|-------|-------|-------------|
| `triggers` | 2.1 | id, event_type, filter_json, template_id, input_map_json, enabled |
| `pipeline_runs` + `parent_run_id`, `chain_depth` | 2.2 | Foreign key + depth tracking |
| `routing_decisions` | 2.3 | run_id, score, tier, action, justification |
| `cost_tracking` | 2.4 | run_id, phase_id, model, input_tokens, output_tokens, cost_usd |
| `pipeline_runs` + `trigger_type`, `trigger_payload_json` | 2.5 | Provenance fields |
| `diagnosis_results` | 3.1 | run_id, failure_class, remediation, confidence |
| `failure_patterns` | 3.1 | pattern_hash, template_id, occurrence_count, is_systemic |
| `pipeline_runs` + `retry_of_run_id`, `retry_strategy` | 3.2 | Retry provenance |
| `regressions` | 3.3 | commit_sha, ci_run_url, fix_run_id, status |
| `maintenance_scans` | 3.5 | repo_path, scan_type, findings_json |
| `issue_pipeline_map` | 4.1 | issue_number, repo, classification_type, run_id |
| `pipeline_runs` + `meta_task_id`, `depends_on_run_ids` | 4.2 | Meta-orchestration fields |
| `deployments` | 4.3 | run_id, stage, target, status, health_check_result |
| `trust_profiles` | 4.4 | repo, template_id, task_type, thresholds, trust_score |
| `trust_adjustments` | 4.4 | profile_id, adjustment_type, old/new_threshold, reason |
| `audit_log` | 4.5 | event_id, run_id, actor, action, details_json, hash chain |
| `repos` | 4.6 | url, name, template_config_json, trigger_config_json |
| `cross_repo_deps` | 4.6 | source_repo_id, target_repo_id, dependency_type |
| `coordinated_prs` | 4.6 | group_id, repo_id, pr_number, status |
