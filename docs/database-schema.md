# Database Schema Reference

> **Engine:** SQLite 3 with WAL journal mode
> **Location:** `~/.orchestration-engine/engine.db` (default)
> **Source:** [`src/orchestration_engine/db.py`](../src/orchestration_engine/db.py)

## Overview

Orchemist uses a single SQLite database for all persistent state. The schema is created on first access by the `Database` class and kept current through a numbered migration system (001–020 as of v0.9).

**Connection settings applied to every connection:**

| PRAGMA | Value | Purpose |
|---|---|---|
| `journal_mode` | `WAL` | Write-Ahead Logging — concurrent readers + single writer |
| `busy_timeout` | `5000` | Wait up to 5 s for a write lock before raising |
| `synchronous` | `NORMAL` | Durability trade-off (safe with WAL) |
| `cache_size` | `10000` | ~40 MB page cache |
| `temp_store` | `memory` | Temp tables in RAM |
| `foreign_keys` | `ON` | Enforce FK constraints at runtime |

**Thread safety:** Each thread gets its own connection via thread-local storage. For `:memory:` databases (tests / dry-run), a shared-cache URI (`file:memdb_<uuid>?mode=memory&cache=shared`) is used so all threads see the same data, with a `threading.Lock` serialising writes.

---

## Table of Contents

1. [pipeline_runs](#1-pipeline_runs) — Async pipeline run state
2. [tasks](#2-tasks) — Task queue entries
3. [task_runs](#3-task_runs) — Individual execution attempts
4. [orchestras](#4-orchestras) — Multi-task workflows
5. [dead_letter_queue](#5-dead_letter_queue) — Permanently failed tasks
6. [triggers](#6-triggers) — Webhook trigger configuration
7. [webhook_invocations](#7-webhook_invocations) — Rate-limit tracking
8. [pipeline_run_events](#8-pipeline_run_events) — SSE live-progress events
9. [routing_decisions](#9-routing_decisions) — Confidence-based routing outcomes
10. [diagnosis_results](#10-diagnosis_results) — Failure diagnosis records
11. [failure_patterns](#11-failure_patterns) — Systemic failure detection
12. [regressions](#12-regressions) — CI regression events
13. [ci_green_shas](#13-ci_green_shas) — Last-known-green CI SHAs
14. [review_outcomes](#14-review_outcomes) — Review phase results
15. [reviewer_calibration](#15-reviewer_calibration) — Longitudinal reviewer accuracy
16. [trust_profiles](#16-trust_profiles) — Per-(repo, template, task_type) trust state
17. [trust_adjustments](#17-trust_adjustments) — Trust score audit log
18. [issue_pipeline_map](#18-issue_pipeline_map) — Issue → pipeline classification
19. [cost_tracking](#19-cost_tracking) — Per-phase cost records
20. [sprint_chain_state](#20-sprint_chain_state) — Post-merge chain automation
21. [migrations](#21-migrations) — Migration tracking

---

## 1. `pipeline_runs`

Core table for async pipeline execution tracking. Created by Issue #267.

| Column | Type | Default | Description |
|---|---|---|---|
| `run_id` | TEXT | **PK** | 8-char UUID prefix |
| `template_path` | TEXT | NOT NULL | Absolute path to the template YAML |
| `template_id` | TEXT | NOT NULL | Template `id` field |
| `input_json` | TEXT | NOT NULL | JSON-encoded pipeline input variables |
| `mode` | TEXT | NOT NULL | `standalone`, `openclaw`, or `dry-run` |
| `output_dir` | TEXT | NOT NULL | Output directory path |
| `status` | TEXT | `'pending'` | See [Run Statuses](#run-statuses) |
| `current_phase` | TEXT | NULL | Currently executing phase ID |
| `completed_phases` | TEXT | `'[]'` | JSON array of completed phase IDs |
| `phase_outputs` | TEXT | `'{}'` | JSON map of phase outputs |
| `pid` | INTEGER | NULL | Daemon process ID |
| `started_at` | TIMESTAMP | NULL | Execution start time |
| `completed_at` | TIMESTAMP | NULL | Terminal state reached at |
| `error_message` | TEXT | NULL | Error details on failure |
| `gateway_url` | TEXT | NULL | OpenClaw gateway URL |
| `skip_scoring` | INTEGER | `0` | Whether auto-scoring was skipped |
| `scoring_status` | TEXT | NULL | `passed`, `failed`, `error` |
| `scoring_score` | REAL | NULL | Composite score [0.0–1.0] |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | Row creation time |
| `review_reason` | TEXT | NULL | Why the run entered review |
| `reviewed_at` | TIMESTAMP | NULL | When review was completed |
| `reviewed_by` | TEXT | NULL | Reviewer identifier |
| `parent_run_id` | TEXT | NULL | Parent run (chained pipelines) |
| `chain_depth` | INTEGER | `0` | Depth in chain hierarchy (0 = root) |
| `retry_of_run_id` | TEXT | NULL | Run this is retrying |
| `retry_strategy` | TEXT | NULL | RetryStrategy enum value |

**Indexes:**

| Index | Columns | Notes |
|---|---|---|
| `idx_pipeline_runs_status` | `status, created_at` | Run listing & filtering |
| `idx_pipeline_runs_retry_of` | `retry_of_run_id` | Find all retries of a run |
| `idx_pipeline_runs_parent_run_id` | `parent_run_id` | Chain traversal |

### Run Statuses

Defined in `TERMINAL_STATUSES` (frozen set, single source of truth):

| Status | Terminal? | Description |
|---|---|---|
| `pending` | No | Queued, not yet started |
| `running` | No | Daemon actively executing phases |
| `success` | **Yes** | All phases completed successfully |
| `failed` | **Yes** | A phase failed fatally |
| `cancelled` | **Yes** | Cancelled by user (SIGTERM sent to daemon) |
| `crashed` | **Yes** | Daemon PID died without recording a terminal state |
| `scoring_failed` | **Yes** | Auto-scoring phase failed |
| `pending_review` | **Yes** | Confidence score fell in human-review tier |
| `rejected` | **Yes** | Human reviewer rejected the run |
| `escalated` | **Yes** | Retry was escalated — original run is terminal |

---

## 2. `tasks`

Task queue entries for the work-dispatch system.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | UUID |
| `type` | TEXT | NOT NULL | Task type identifier |
| `priority` | INTEGER | `3` | Lower = higher priority |
| `status` | TEXT | `'queued'` | `queued`, `running`, `retry`, `success`, `failed`, `permanently_failed`, `cancelled` |
| `payload` | JSON | NOT NULL | Task-specific JSON payload |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `started_at` | TIMESTAMP | NULL | First picked up by a worker |
| `completed_at` | TIMESTAMP | NULL | Reached terminal state |
| `next_retry_at` | TIMESTAMP | NULL | When to retry (for `retry` status) |
| `retry_count` | INTEGER | `0` | Current retry count |
| `max_retries` | INTEGER | `3` | Max retries before dead-lettering |
| `orchestra_id` | TEXT | NULL | FK → `orchestras.id` |
| `orchestra_phase` | TEXT | NULL | Phase within the orchestra |
| `min_confidence` | REAL | `0.7` | Minimum acceptable confidence |
| `preferred_model` | TEXT | NULL | Preferred model tier |
| `timeout_seconds` | INTEGER | `3600` | Per-task timeout |
| `cost_limit_usd` | DECIMAL(10,4) | NULL | Max spend per task |
| `created_by` | TEXT | NULL | Originating user/system |
| `tags` | JSON | `'[]'` | JSON array of tags |
| `metadata` | JSON | `'{}'` | Arbitrary metadata |

**Indexes:**

| Index | Columns | Notes |
|---|---|---|
| `idx_tasks_status_priority` | `status, priority DESC` | Worker dispatch |
| `idx_tasks_orchestra` | `orchestra_id, orchestra_phase` | Orchestra phase queries |
| `idx_tasks_retry` | `status, next_retry_at` | Partial: `WHERE status = 'retry'` |
| `idx_tasks_created_at` | `created_at` | Chronological listing |
| `idx_tasks_type_status` | `type, status` | Type-based filtering |
| `idx_tasks_cost_tracking` | `type, created_at, cost_limit_usd` | Analytics |

---

## 3. `task_runs`

Individual execution attempts for a task.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | UUID |
| `task_id` | TEXT | NOT NULL, FK → `tasks.id` | Parent task |
| `attempt_number` | INTEGER | NOT NULL | 1-based attempt counter |
| `model` | TEXT | NOT NULL | Model identifier used |
| `thinking_level` | TEXT | NULL | `low`, `medium`, `high` |
| `session_id` | TEXT | NULL | Conversation session ID |
| `worker_id` | TEXT | NULL | Worker that executed it |
| `started_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `completed_at` | TIMESTAMP | NULL | — |
| `status` | TEXT | NOT NULL | `running`, `success`, `failed` |
| `result` | JSON | NULL | LLM output / structured result |
| `confidence` | REAL | NULL | Confidence score [0.0–1.0] |
| `error_message` | TEXT | NULL | Error details |
| `error_type` | TEXT | NULL | Error classification |
| `tokens_used` | INTEGER | `0` | Total tokens consumed |
| `cost_usd` | DECIMAL(10,4) | NULL | Cost for this attempt |
| `peak_memory_mb` | INTEGER | NULL | Peak memory usage |

**Constraints:** `UNIQUE(task_id, attempt_number)`

**Indexes:**

| Index | Columns | Notes |
|---|---|---|
| `idx_task_runs_task` | `task_id, attempt_number` | Attempt lookup |
| `idx_task_runs_model_metrics` | `model, status, completed_at` | Model analytics |

---

## 4. `orchestras`

Multi-task workflow containers.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | UUID |
| `template` | TEXT | NOT NULL | Template name or ID |
| `name` | TEXT | NULL | Human-friendly name |
| `status` | TEXT | `'running'` | `running`, `completed`, `failed`, `cancelled` |
| `config` | JSON | NOT NULL | Full orchestra configuration |
| `priority` | INTEGER | `3` | Priority level |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `completed_at` | TIMESTAMP | NULL | — |
| `total_tasks` | INTEGER | `0` | Total tasks in the orchestra |
| `completed_tasks` | INTEGER | `0` | Successfully finished tasks |
| `failed_tasks` | INTEGER | `0` | — |
| `cancelled_tasks` | INTEGER | `0` | — |
| `cost_budget_usd` | DECIMAL(10,4) | NULL | Cost budget cap |
| `time_budget_hours` | INTEGER | NULL | Time budget cap |
| `cost_spent_usd` | DECIMAL(10,4) | `0.0` | Running spend total |
| `created_by` | TEXT | NULL | Originating user |
| `tags` | JSON | `'[]'` | — |
| `current_phase` | TEXT | NULL | Active phase |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_orchestras_status` | `status, created_at` |

---

## 5. `dead_letter_queue`

Permanently failed tasks retained for analysis.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | UUID |
| `original_task_id` | TEXT | NOT NULL | Original task ID |
| `task_type` | TEXT | NOT NULL | Task type |
| `failure_reason` | TEXT | NOT NULL | Why it was dead-lettered |
| `failure_count` | INTEGER | NOT NULL | Total failures |
| `payload` | JSON | NOT NULL | Full task payload |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `error_patterns` | JSON | `'[]'` | Extracted error signatures |
| `suggested_fixes` | JSON | `'[]'` | AI-suggested remediations |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_dead_letter_analysis` | `task_type, created_at` |

---

## 6. `triggers`

Webhook trigger configuration (Issue #329.1).

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | Trigger identifier |
| `template_id` | TEXT | NOT NULL | Pipeline template to run |
| `mode` | TEXT | `'async'` | `sync`, `async`, or `fire_and_forget` |
| `secret` | TEXT | NULL | HMAC-SHA256 shared secret (write-only in API) |
| `rate_limit` | INTEGER | `0` | Max requests/minute (0 = unlimited) |
| `input_map` | TEXT | `'{}'` | JSON payload-to-input mapping |
| `filters` | TEXT | `'[]'` | JSON array of filter conditions |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `updated_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `enabled` | INTEGER | `1` | 1 = active, 0 = disabled |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_triggers_template_id` | `template_id` |
| `idx_triggers_mode_created` | `mode, created_at` |

---

## 7. `webhook_invocations`

Per-trigger invocation records for rate-limit enforcement (Issue #329.2). Rows older than 60 seconds are used in the sliding-window calculation, then naturally age out.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `trigger_id` | TEXT | NOT NULL | FK → `triggers.id` |
| `invoked_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_webhook_invocations_trigger_time` | `trigger_id, invoked_at` |

---

## 8. `pipeline_run_events`

SSE live-progress events emitted during phase transitions (Issue #258). Polled by the `/api/v1/runs/{run_id}/stream` endpoint.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | Monotonic event ID |
| `run_id` | TEXT | NOT NULL, FK → `pipeline_runs.run_id` | — |
| `event_type` | TEXT | NOT NULL | `phase_started`, `phase_completed`, `status_changed`, `error` |
| `phase_id` | TEXT | NULL | Phase identifier |
| `tokens_consumed` | INTEGER | NULL | Tokens used in this phase |
| `cost_usd` | REAL | NULL | Cost for this phase |
| `state` | TEXT | NULL | Phase outcome state |
| `metadata_json` | TEXT | `'{}'` | Additional event metadata |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns | Notes |
|---|---|---|
| `idx_pipeline_run_events_run_id` | `run_id, id` | Ordered event retrieval for SSE polling |

---

## 9. `routing_decisions`

Records the confidence-based routing outcome for each pipeline run (Issue #331.3). One row per run, created when the scoring phase completes.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `run_id` | TEXT | NOT NULL, FK → `pipeline_runs.run_id` | — |
| `confidence_score` | REAL | NOT NULL | Composite confidence [0.0–1.0] |
| `tier_name` | TEXT | NOT NULL | Routing tier: `auto_merge`, `human_review`, `auto_reject` |
| `action` | TEXT | NOT NULL | Action taken: `auto_merge`, `human_review`, `reject` |
| `justification` | TEXT | NULL | Human-readable explanation |
| `signals_json` | TEXT | `'{}'` | JSON object of individual scoring signals |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_routing_decisions_run_id` | `run_id` |

---

## 10. `diagnosis_results`

Failure diagnosis records from the LLM-powered diagnosis subsystem (Issue #3.1.1). One row per diagnosis attempt.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `run_id` | TEXT | NOT NULL, FK → `pipeline_runs.run_id` | Failed run |
| `failure_class` | TEXT | NOT NULL | Classified failure type |
| `remediation` | TEXT | NOT NULL | Suggested fix |
| `confidence` | REAL | NOT NULL | Diagnosis confidence [0.0–1.0] |
| `explanation` | TEXT | NULL | Detailed explanation |
| `model_used` | TEXT | NULL | Model that performed diagnosis |
| `tokens_consumed` | INTEGER | `0` | Tokens used for diagnosis |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_diagnosis_results_run_id` | `run_id` |

---

## 11. `failure_patterns`

Systemic failure detection (Issue #3.1.3). Tracks recurring failure signatures per template. A pattern is marked `is_systemic = 1` when it recurs more than `SYSTEMIC_THRESHOLD` times within `SYSTEMIC_WINDOW_DAYS`.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `pattern_hash` | TEXT | NOT NULL | Hash of the failure signature |
| `template_id` | TEXT | NOT NULL | Template where the pattern occurs |
| `failure_class` | TEXT | NOT NULL | Failure classification |
| `occurrence_count` | INTEGER | `1` | How many times seen |
| `is_systemic` | INTEGER | `0` | 1 = systemic pattern |
| `first_seen_at` | TEXT | NOT NULL | ISO timestamp |
| `last_seen_at` | TEXT | NOT NULL | ISO timestamp |

**Constraints:** `UNIQUE(pattern_hash, template_id)`

**Indexes:**

| Index | Columns |
|---|---|
| `idx_failure_patterns_template` | `template_id, last_seen_at` |

---

## 12. `regressions`

CI regression event tracking (Issue #3.3a.1). One row per regression detected by the CI webhook handler.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | TEXT | **PK** | UUID |
| `commit_sha` | TEXT | NOT NULL | Git commit that caused the regression |
| `ci_run_url` | TEXT | NOT NULL | CI run URL |
| `failure_type` | TEXT | NOT NULL | Classification of the failure |
| `affected_files` | TEXT | `'[]'` | JSON array of affected file paths |
| `diagnosis` | TEXT | NULL | LLM diagnosis (filled after analysis) |
| `fix_run_id` | TEXT | NULL | Pipeline run launched to fix it |
| `status` | TEXT | `'detected'` | `detected`, `fixing`, `fixed`, `wont_fix` |
| `fix_attempt_count` | INTEGER | `0` | Fix attempts made |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_regressions_status_created` | `status, created_at` |
| `idx_regressions_commit_sha` | `commit_sha` |

---

## 13. `ci_green_shas`

Last-known-green CI SHA per repository (Issue #3.3a.3). Used to determine what changed when a regression is detected.

| Column | Type | Default | Description |
|---|---|---|---|
| `repo_slug` | TEXT | **PK** | `"owner/repo"` |
| `sha` | TEXT | NOT NULL | Last passing commit SHA |
| `updated_at` | TEXT | NOT NULL | ISO timestamp |

---

## 14. `review_outcomes`

Durable storage of review phase results (Issue #4.1.2). One row per review execution within a pipeline run.

| Column | Type | Default | Description |
|---|---|---|---|
| `review_id` | TEXT | **PK** | UUID |
| `run_id` | TEXT | NOT NULL, FK → `pipeline_runs.run_id` | — |
| `phase_id` | TEXT | NOT NULL | Phase ID (e.g. `"review"`) |
| `reviewer_model` | TEXT | NULL | Model used for the review |
| `verdict` | TEXT | NULL | `APPROVE` or `REQUEST_CHANGES` |
| `issues_found` | TEXT | `'[]'` | JSON array of issue objects |
| `fix_verified` | INTEGER | `0` | 1 = subsequent fix verified |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_review_outcomes_run_id` | `run_id, created_at` |

---

## 15. `reviewer_calibration`

Longitudinal accuracy tracking of AI reviewer models (Issue #4.1.5). Periodic snapshots of per-model accuracy metrics.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `reviewer_model` | TEXT | NOT NULL | Model name (e.g. `"opus"`) |
| `total_reviews` | INTEGER | `0` | Total outcomes observed |
| `approve_count` | INTEGER | `0` | APPROVE verdicts |
| `request_changes_count` | INTEGER | `0` | REQUEST_CHANGES verdicts |
| `approve_held_up_count` | INTEGER | `0` | Approves where no fix was needed |
| `request_changes_valid_count` | INTEGER | `0` | RCs confirmed by a verified fix |
| `approve_accuracy` | REAL | NULL | `approve_held_up / approve_count` |
| `request_changes_accuracy` | REAL | NULL | `rc_valid / rc_count` |
| `overall_accuracy` | REAL | NULL | Combined accuracy |
| `computed_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | Snapshot timestamp |
| `aggregation_window` | TEXT | NULL | e.g. `"30d"`, `"all-time"` |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_reviewer_calibration_model` | `reviewer_model, computed_at DESC` |

---

## 16. `trust_profiles`

Per-(repo, template, task_type) trust state for confidence-based routing (Issue #4.2.1). The trust score calibrates auto-merge and human-review thresholds.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `repo` | TEXT | NOT NULL | Git repository slug |
| `template_id` | TEXT | NOT NULL | Pipeline template ID |
| `task_type` | TEXT | NOT NULL | Task classification |
| `auto_merge_threshold` | REAL | `0.85` | Score needed for auto-merge |
| `human_review_threshold` | REAL | `0.70` | Score needed to skip review |
| `trust_score` | REAL | `0.5` | Current trust score [0.0–1.0] |
| `total_runs` | INTEGER | `0` | Total runs attributed |
| `successful_merges` | INTEGER | `0` | Auto-merges without revert |
| `regressions` | INTEGER | `0` | Regressions after auto-merge |
| `reverted_prs` | INTEGER | `0` | PRs reverted |
| `last_run_at` | TEXT | NULL | ISO timestamp |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |
| `updated_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Constraints:** `UNIQUE(repo, template_id, task_type)`

**Indexes:**

| Index | Columns |
|---|---|
| `idx_trust_profiles_repo_template` | `repo, template_id` |

---

## 17. `trust_adjustments`

Trust score change audit log (Issue #4.2.1). Every score change — automatic or manual — is recorded here.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `profile_id` | INTEGER | NOT NULL, FK → `trust_profiles.id` | — |
| `delta` | REAL | NOT NULL | Score change (positive = increase) |
| `reason` | TEXT | NOT NULL | e.g. `successful_merge`, `regression_detected`, `manual_override:username` |
| `run_id` | TEXT | NULL | Pipeline run that triggered the change |
| `score_before` | REAL | NOT NULL | Trust score before |
| `score_after` | REAL | NOT NULL | Trust score after |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_trust_adjustments_profile_id` | `profile_id, created_at DESC` |

---

## 18. `issue_pipeline_map`

LLM-based issue classification results (Issue #5.1.1). Records which pipeline was selected for a GitHub issue and the classification confidence.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `issue_number` | INTEGER | NOT NULL | GitHub issue number |
| `repo` | TEXT | NOT NULL | Repository slug |
| `classification_type` | TEXT | NOT NULL | `bug`, `feature`, `docs`, `refactor`, `research`, `content` |
| `confidence` | REAL | NOT NULL | Classification confidence [0.0–1.0] |
| `template_id` | TEXT | NULL | Recommended pipeline template |
| `run_id` | TEXT | NULL | Pipeline run launched (null until triggered) |
| `status` | TEXT | `'classified'` | Lifecycle status |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_issue_pipeline_map_issue_repo` | `issue_number, repo` |
| `idx_issue_pipeline_map_repo_created` | `repo, created_at` |

---

## 19. `cost_tracking`

Per-phase cost records for pipeline runs (Issue #5.2.1). Queried by `GET /api/v1/costs/summary` and `GET /api/v1/costs/run/{run_id}`.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `run_id` | TEXT | NOT NULL, FK → `pipeline_runs.run_id` | — |
| `phase_id` | TEXT | NOT NULL | Phase identifier |
| `model` | TEXT | NOT NULL | Model used |
| `input_tokens` | INTEGER | `0` | Prompt tokens |
| `output_tokens` | INTEGER | `0` | Completion tokens |
| `cost_usd` | REAL | `0.0` | Computed USD cost |
| `created_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Indexes:**

| Index | Columns |
|---|---|
| `idx_cost_tracking_run_id` | `run_id, created_at` |

---

## 20. `sprint_chain_state`

Post-merge chain automation state (Issue #514). Tracks which issues have been processed by the sprint chain so the system can deduplicate and resume.

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `repo` | TEXT | NOT NULL | Repository slug |
| `issue_number` | INTEGER | NOT NULL | GitHub issue number |
| `status` | TEXT | `'processed'` | `processed` or `paused` |
| `run_id` | TEXT | NULL | Pipeline run ID |
| `score` | REAL | NULL | Confidence score at processing time |
| `processed_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

**Constraints:** `UNIQUE(repo, issue_number)`

**Indexes:**

| Index | Columns |
|---|---|
| `idx_sprint_chain_repo` | `repo, processed_at` |

---

## 21. `migrations`

Migration tracking table (auto-created by `_run_migrations`).

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER | **PK AUTOINCREMENT** | — |
| `name` | TEXT | NOT NULL, UNIQUE | Migration identifier |
| `applied_at` | TIMESTAMP | `CURRENT_TIMESTAMP` | — |

---

## Migration History

All migrations are idempotent (use `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN` with try/except, and `CREATE INDEX IF NOT EXISTS`).

| # | Name | Issue | Description |
|---|---|---|---|
| 001 | `add_scoring_status` | #287 | Add `scoring_status`, `scoring_score` to `pipeline_runs` |
| 002 | `add_pipeline_run_events` | #258 | Create `pipeline_run_events` table |
| 003 | `add_triggers_table` | #329.1 | Create `triggers` table |
| 004 | `add_webhook_invocations` | #329.2 | Create `webhook_invocations` table |
| 005 | `add_trigger_enabled` | #329.2 | Add `enabled` column to `triggers` |
| 006 | `add_chain_columns` | #330.1 | Add `parent_run_id`, `chain_depth` to `pipeline_runs` |
| 007 | `add_routing_decisions` | #331.3 | Create `routing_decisions` table |
| 008 | `add_review_columns` | #331.4 | Add `review_reason`, `reviewed_at`, `reviewed_by` to `pipeline_runs` |
| 009 | `add_diagnosis_tables` | #3.1.1 | Create `diagnosis_results` table |
| 010 | `add_failure_patterns_table` | #3.1.3 | Create `failure_patterns` table |
| 011 | `add_retry_columns` | #3.2.1 | Add `retry_of_run_id`, `retry_strategy` to `pipeline_runs` + index |
| 012 | `add_regressions_table` | #3.3a.1 | Create `regressions` table |
| 013 | `add_ci_green_shas_table` | #3.3a.3 | Create `ci_green_shas` table |
| 014 | `add_review_outcomes_table` | #4.1.2 | Create `review_outcomes` table |
| 015 | `add_reviewer_calibration_table` | #4.1.5 | Create `reviewer_calibration` table |
| 016 | `add_trust_tables` | #4.2.1 | Create `trust_profiles` + `trust_adjustments` |
| 017 | `add_issue_pipeline_map` | #5.1.1 | Create `issue_pipeline_map` table |
| 018 | `add_cost_tracking_table` | #5.2.1 | Create `cost_tracking` table |
| 019 | `add_parent_run_id_index` | #508 | Add index on `pipeline_runs(parent_run_id)` |
| 020 | `add_sprint_chain_state_table` | #514 | Create `sprint_chain_state` table |

---

## Entity Relationship Summary

```
pipeline_runs ─────────┬──── pipeline_run_events
  │                     ├──── routing_decisions
  │                     ├──── diagnosis_results
  │                     ├──── review_outcomes
  │                     ├──── cost_tracking
  │                     └──── regressions (via fix_run_id)
  │
  ├─ parent_run_id ───→ pipeline_runs (self-referential chain)
  ├─ retry_of_run_id ─→ pipeline_runs (self-referential retry)
  │
  └─ template_id ─────→ triggers.template_id (logical, not FK)

tasks ─────────────────┬──── task_runs
  │                     └──── dead_letter_queue (via original_task_id)
  └─ orchestra_id ────→ orchestras

trust_profiles ────────┬──── trust_adjustments
                        └──── (referenced by routing_decisions.tier_name logic)

issue_pipeline_map ────→ (logical link to pipeline_runs via run_id)

sprint_chain_state ────→ (logical link to pipeline_runs via run_id)
```

---

## Backup & Maintenance

**Backup:** Copy the `.db`, `.db-wal`, and `.db-shm` files together while the engine is idle, or use the SQLite `.backup` command.

**Vacuuming:** Run `VACUUM` periodically to reclaim space from deleted rows. This is not done automatically.

**WAL checkpointing:** SQLite auto-checkpoints when the WAL reaches 1000 pages (~4 MB). Manual checkpoint: `PRAGMA wal_checkpoint(TRUNCATE)`.

**Rate-limit cleanup:** `webhook_invocations` rows accumulate over time. Consider periodic pruning of rows older than 1 hour.
