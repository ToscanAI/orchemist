# System Architecture

The Orchestration Engine is a **meta-coordination layer** that sits on top of OpenClaw to provide a reliable, observable task queue and scenario-based quality evaluation for multi-agent workflows.

## Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION ENGINE (Week 2)               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                  CLI (cli.py)                           │   │
│  │  orch submit / status / list / cancel / retry          │   │
│  │  orch dead-letter / health                             │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────┐  ┌─────────────────────┐  ┌────────────────┐   │
│  │ Config     │  │   Task Queue        │  │ Progress       │   │
│  │ (config.py)│  │   (queue.py)        │  │ Tracking       │   │
│  │ TOML+env   │  │   submit/list/cancel│  │ (progress.py)  │   │
│  └────────────┘  └─────────────────────┘  └────────────────┘   │
│                           │                        │            │
│  ┌────────────────────────▼────────────────────────▼────────┐   │
│  │                  Database (db.py)                        │   │
│  │  SQLite • WAL mode • Thread-safe • Foreign keys          │   │
│  │  Tables: tasks, task_runs, orchestras, dead_letter_queue │   │
│  │          workers, retry_attempts, circuit_breaker_state  │   │
│  │          error_patterns, progress_events                 │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────▼─────────────────────────────────┐  │
│  │              Task Runner (runner.py)                      │  │
│  │  Polls queue • Assigns workers • Manages lifecycle        │  │
│  │                                                           │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐  │  │
│  │  │ DryRun      │  │ Local        │  │ OpenClaw        │  │  │
│  │  │ Executor    │  │ Executor     │  │ Executor        │  │  │
│  │  │ (mock/test) │  │ (shlex-safe  │  │ (file-based     │  │  │
│  │  │             │  │  subprocess) │  │  contract)      │  │  │
│  │  └─────────────┘  └──────────────┘  └─────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─────────────────────┐  ┌─────────────────────────────────┐  │
│  │ Worker Pool         │  │ Recovery Manager                 │  │
│  │ (concurrency.py)    │  │ (recovery.py)                   │  │
│  │ • Thread-safe       │  │ • Error classification          │  │
│  │ • Heartbeat monitor │  │ • Exponential backoff           │  │
│  │ • Stale detection   │  │ • Circuit breakers              │  │
│  │ • Resource limits   │  │ • Model tier escalation         │  │
│  └─────────────────────┘  └─────────────────────────────────┘  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│              SCENARIO RUNNER (scenario_runner/)                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  ScenarioRunner — loads YAML, grades pipeline output    │   │
│  │                                                         │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │   │
│  │  │ Assertion    │  │ LLM Judge    │  │ URL Check    │  │   │
│  │  │ Grader       │  │ Grader       │  │ Grader       │  │   │
│  │  │ restricted   │  │ holdout      │  │ HTTP 200     │  │   │
│  │  │ eval         │  │ enforced     │  │ checks       │  │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  scenarios/content-pipeline/  — 3 YAML scenario files          │
│  scenarios/shared/rubrics/    — 4 shared rubric markdown files  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### Database Layer (`db.py`)

SQLite-backed persistent storage. All connections use WAL mode for concurrency.

**Pragmas applied per connection:**
```sql
PRAGMA journal_mode = WAL
PRAGMA synchronous = NORMAL
PRAGMA cache_size = 10000
PRAGMA temp_store = memory
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = 5000
```

**Core tables:**
- `tasks` — main queue (id, type, priority, status, payload, retry_count, etc.)
- `task_runs` — individual execution attempts with results
- `orchestras` — multi-task workflow groups
- `dead_letter_queue` — permanently failed tasks

**Recovery tables (created by RecoveryManager):**
- `retry_attempts` — scheduled retry history
- `circuit_breaker_state` — per-(task_type:model_tier) breaker state
- `error_patterns` — frequency-tracked error message patterns

**Progress tables (created by ProgressTracker):**
- `progress_events` — per-task event log
- `task_progress_summary` — current state snapshot

### Task Queue (`queue.py`)

High-level queue operations on top of the database.

**Key operations:**
- `submit_task(TaskSpec) → str` — enqueue and return task ID
- `get_task_status(task_id) → TaskStatus`
- `list_tasks(TaskFilters) → List[TaskSummary]`
- `cancel_task(task_id) → bool`
- `retry_task(task_id) → bool`
- `get_queue_stats() → QueueStats`
- `get_dead_letter_tasks() → List[DeadLetterTask]`

**Task state machine:**
```
[queued] → [running] → [success]
    ↑           ↓
    ←── [retry] ←── [failed]
                        ↓
           [permanently_failed] → [dead_letter_queue]
                        ↓
                    [cancelled]
```

### Task Runner (`runner.py`)

Central execution loop. Polls queue, assigns workers, runs tasks, handles outcomes.

**Three executors:**

| Executor | Purpose |
|----------|---------|
| `DryRunExecutor` | Testing — returns mock results with configurable failure rate |
| `LocalExecutor` | Runs shell commands via `shlex`-safe subprocess |
| `OpenClawExecutor` | File-based contract with OpenClaw sub-agents |

**OpenClaw execution contract:**
1. Write task prompt to `{workdir}/task_{id}.txt`
2. Write status to `{workdir}/status_{id}.json`
3. Poll for result at `{workdir}/result_{id}.json`
4. Parse structured JSON result

### Worker Pool (`concurrency.py`)

Thread-safe worker lifecycle management.

**Key classes:**
- `WorkerPool` — creates, assigns, tracks, and terminates workers
- `ResourceLimits` — enforces max session count and daily cost budget
- `WorkerInfo` — per-worker state (idle/assigned/running/stale/terminated)

**Configurable limits (from TOML config):**
- `max_workers` — max concurrent tasks (default: 4)
- `max_sessions` — max OpenClaw sessions open at once
- `daily_budget_usd` — hard spending cap per day

### Recovery Manager (`recovery.py`)

Intelligent failure handling with thread-safe state.

**Components:**
- `ErrorClassifier` — keyword-pattern matching → `(ErrorType, ErrorSeverity)`
- `CircuitBreakerState` — per `task_type:model_tier` key, stored in DB
- `TaskRetryState` — in-memory retry tracking per task
- `RecoveryManager` — coordinates all of the above, uses `threading.Lock`

**Error types:** `TRANSIENT | PERMANENT | QUALITY | RESOURCE | TIMEOUT | RATE_LIMIT`

**Escalation paths (per task type):**
```python
CONTENT:     haiku → sonnet → opus
CODE:        sonnet → opus → opus
RESEARCH:    haiku → sonnet → opus
TRANSLATION: sonnet → opus → opus
REVIEW:      sonnet → opus → opus
```

See `docs/error-recovery.md` for full details.

### Progress Tracker (`progress.py`)

SQLite-backed event log for all task lifecycle transitions.

**Event types:** `queued | started | progress_update | model_selected | session_created | session_ended | retry_scheduled | escalated | completed | failed | cancelled | timeout | resource_limit | circuit_breaker`

**Helper methods:** `task_queued()`, `task_started()`, `task_progress()`, `task_completed()`, `task_failed()`, `task_retry_scheduled()`, `model_escalated()`

### Configuration (`config.py`)

TOML-based configuration with environment variable overrides.

- Default config file: `~/.orchestration-engine/config.toml`
- Env vars: `ORCH_MAX_WORKERS`, `ORCH_DB_PATH`, `ORCH_LOG_LEVEL`, etc.
- Config sections: `[engine]`, `[retry]`, `[models]`, `[logging]`

### Pydantic V2 Schemas (`schemas.py`)

Type-safe models for all data structures. Uses **Pydantic V2** API.

**Key differences from V1:**
- `@model_validator(mode='after')` instead of `@validator`
- `model_dump()` instead of `.dict()`
- `ConfigDict` for model configuration

See `docs/structured-schemas.md` for full schema catalog.

### Scenario Runner (`scenario_runner/`)

Standalone quality evaluation system. Loads YAML scenario files and grades pipeline output against acceptance criteria.

See `scenario_runner/README.md` for full details.

## Data Flow

1. **CLI** → `TaskQueue.submit_task(TaskSpec)` → SQLite `tasks` table
2. **TaskRunner** polls `tasks` for `queued` or ready-to-retry tasks
3. **WorkerPool** assigns a worker thread
4. **Executor** runs the task (DryRun / Local / OpenClaw)
5. **ProgressTracker** records events at each lifecycle step
6. On success → `task_runs` updated, task marked `success`
7. On failure → **RecoveryManager** classifies error, schedules retry or dead-letters
8. **Circuit breakers** trip after N consecutive unique-task failures per `task_type:model_tier`

## What Is NOT Implemented

The following are designed/documented but have **no code yet**:

| Feature | Status | Planned |
|---------|--------|---------|
| MCP integration | Deferred | v1.0+ |
| Memory system (episodic/semantic/procedural) | Deferred | v1.0+ |
| Advanced metrics dashboard | Deferred | Week 4-5 |
| Template engine / phase sequencer | Not started | Week 3 |
| Scenario runner CLI (`orch scenario run`) | Not started | Week 6 |
| Digital twin / mock service layer | Deferred | TBD |
| LangGraph integration | Deferred | TBD |
| CI/CD scenario integration | Deferred | TBD |
| REST API | Not planned (MVP) | TBD |
| Web dashboard | Not planned (MVP) | TBD |
