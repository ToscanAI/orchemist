# Forensic Finding 06 — Database Schema Drift

> **Severity:** 🟠 Medium  
> **Impact:** Documentation describes 4 tables; the actual database has 22+ tables. Contributors cannot understand the data model from docs alone.  
> **Generated:** 2026-03-13

---

## Summary

`docs/task-queue.md` documents the original 4-table schema. The actual `db.py` module creates **22+ tables** across multiple initialization methods. The documentation gap means:

1. New contributors don't know what data is persisted
2. There's no schema migration strategy documented
3. The table relationships are only discoverable by reading `db.py` (2,400+ lines)

---

## Documented Tables (4)

These tables are described in `docs/task-queue.md`:

| Table | Purpose | Documented? |
|-------|---------|:-----------:|
| `tasks` | Core task queue | ✅ |
| `task_runs` | Execution attempt log | ✅ |
| `orchestras` | Multi-task pipeline workflows | ✅ |
| `dead_letter_queue` | Permanently failed tasks | ✅ |

---

## Undocumented Tables (18+)

These tables exist in `db.py` but appear in no documentation:

### Pipeline Execution Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `pipeline_runs` | `_create_tables()` L195 | Async pipeline run records (Issue #267) |
| `pipeline_run_events` | `_create_tables_pipeline_run_events()` L394 | SSE live-progress events (Issue #258) |

### Webhook & Trigger Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `triggers` | `_create_tables()` L348 | Webhook trigger configurations (Issue #329.1) |
| `webhook_invocations` | `_create_tables()` L364 | Rate-limit enforcement for webhooks (Issue #329.2) |

### Diagnosis & Recovery Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `diagnosis_results` | `_create_tables()` L373 | LLM-powered failure diagnosis (Issue #3.1.1) |
| `failure_patterns` | `_create_tables_pipeline_run_events()` L440 | Systemic failure pattern tracking (Issue #3.1.3) |

### Routing & Scoring Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `routing_decisions` | `_create_tables_pipeline_run_events()` L415 | Confidence routing outcomes (Issue #331.3) |

### Regression Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `regressions` | Separate method ~L465 | Regression tracking with lifecycle states (Issue #3.3a.1) |
| `ci_green_shas` | Separate method ~L495 | Last-known-green CI commit SHAs (Issue #3.3a.3) |

### Review & Calibration Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `review_outcomes` | ~L520, duplicate at ~L2141 | Review result storage (Issue #4.1.2) |
| `reviewer_calibration` | ~L562, duplicate at ~L2165 | Reviewer accuracy tracking (Issue #4.1.5) |

### Trust Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `trust_profiles` | ~L2209 | Per-(repo, template, task_type) trust state (Issue #4.2.1) |
| `trust_adjustments` | ~L2252 | Trust score change audit trail (Issue #4.2.1) |

### Issue Automation Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `issue_pipeline_map` | ~L2298 | Maps GitHub issues to pipeline runs (Issue #5.1.1) |

### Cost Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `cost_tracking` | ~L2345 | Per-phase cost records (Issue #5.2.1) |

### Sprint Chain Tables

| Table | Created In | Purpose |
|-------|-----------|---------|
| `sprint_chain_state` | ~L2401 | Sprint runner execution state (Issue #514) |

### Error Recovery Tables (from `recovery.py`)

| Table | Created In | Purpose |
|-------|-----------|---------|
| `retry_attempts` | `recovery.py` | Retry attempt log per task |
| `circuit_breaker_state` | `recovery.py` | Circuit breaker state per task_type:model_tier |
| `error_patterns` | `recovery.py` | Error pattern frequency tracking |

---

## Potential Issues

### 1. Duplicate Table Definitions

`review_outcomes` and `reviewer_calibration` appear to have duplicate `CREATE TABLE` statements at two different locations in `db.py`:
- First occurrence: ~L520 and ~L562
- Second occurrence: ~L2141 and ~L2165

This is not a bug (SQLite's `IF NOT EXISTS` prevents errors), but it suggests the table creation was added in two different development passes and not consolidated.

### 2. No Migration Strategy

The codebase uses `CREATE TABLE IF NOT EXISTS` exclusively — no schema migration system (Alembic, custom versioning, etc.). This means:
- Column additions require manual ALTER TABLE or DB recreation
- Schema changes between versions could silently leave old columns in place
- No way to track which schema version a given `engine.db` file is at

For a tool approaching open-source release, this should be addressed.

### 3. Table Creation Spread Across Multiple Methods

Tables are created in at least 6 different methods/locations within `db.py`:
- `_create_tables()` — core tables + triggers + webhooks + diagnosis
- `_create_tables_pipeline_run_events()` — events + routing + failure patterns
- Separate methods for regressions, ci_green_shas
- Separate methods for review_outcomes, reviewer_calibration, trust, issue_pipeline_map, cost_tracking, sprint_chain_state

This makes it difficult to get a complete picture of the schema without reading the entire file.

---

## Recommendation

1. **Create `docs/database-schema.md`** — Document all 22+ tables with columns, types, relationships, and which feature each table supports
2. **Consolidate table creation** — Consider a single `_create_all_tables()` method or a list-driven approach
3. **Add schema versioning** — A simple `schema_version` table with a version integer, checked on startup
4. **Remove duplicate definitions** — Consolidate the `review_outcomes` and `reviewer_calibration` duplicates
5. **Add an ER diagram** — Even a text-based one showing which modules read/write which tables
