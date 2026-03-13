# Task Queue

The task queue is the **heart** of the orchestration engine — a SQLite-backed, persistent, concurrent task scheduling system with priority scheduling, state management, and retry logic.

> **Note:** The database has grown to **22+ tables** since this document was written. The 4 core tables below remain accurate, but the engine now also stores pipeline runs, SSE events, webhook triggers, cost tracking, trust profiles, diagnosis results, regressions, routing decisions, review outcomes, and more. See `db.py` for the complete schema.

## Core Principles

- **Persistence**: Survives process restarts (SQLite with WAL mode)
- **Concurrency**: Configurable worker pool (default: 4 workers)
- **Priority**: Critical, high, normal, low priority levels
- **Retry Logic**: Exponential backoff with model tier escalation
- **Dead Letter Queue**: Permanent failure handling
- **State Tracking**: Complete audit trail via `task_runs`

## SQLite Schema

### Core Tables

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                  -- 'content', 'code', 'research', 'translation', 'review'
    priority INTEGER DEFAULT 3,          -- 1=critical, 2=high, 3=normal, 4=low
    status TEXT DEFAULT 'queued',        -- state machine (see below)
    payload JSON NOT NULL,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    next_retry_at TIMESTAMP,
    
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    
    orchestra_id TEXT,                   -- parent workflow
    orchestra_phase TEXT,
    
    min_confidence REAL DEFAULT 0.7,
    preferred_model TEXT,
    
    timeout_seconds INTEGER DEFAULT 3600,
    cost_limit_usd DECIMAL(10,4),
    
    created_by TEXT,
    tags JSON DEFAULT '[]',
    metadata JSON DEFAULT '{}',
    
    FOREIGN KEY(orchestra_id) REFERENCES orchestras(id)
);

-- Individual execution attempts
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    
    model TEXT NOT NULL,                 -- 'haiku-4-5', 'sonnet-4', 'opus-4-6'
    thinking_level TEXT,
    session_id TEXT,
    worker_id TEXT,
    
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    
    status TEXT NOT NULL,                -- 'running', 'success', 'failed'
    result JSON,
    confidence REAL,
    error_message TEXT,
    error_type TEXT,                     -- 'transient', 'permanent', 'quality'
    
    tokens_used INTEGER DEFAULT 0,
    cost_usd DECIMAL(10,4),
    peak_memory_mb INTEGER,
    
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    UNIQUE(task_id, attempt_number)
);

-- Multi-task workflow groups
CREATE TABLE orchestras (
    id TEXT PRIMARY KEY,
    template TEXT NOT NULL,
    name TEXT,
    status TEXT DEFAULT 'running',
    config JSON NOT NULL,
    priority INTEGER DEFAULT 3,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_tasks INTEGER DEFAULT 0,
    completed_tasks INTEGER DEFAULT 0,
    failed_tasks INTEGER DEFAULT 0,
    cancelled_tasks INTEGER DEFAULT 0,
    
    cost_budget_usd DECIMAL(10,4),
    time_budget_hours INTEGER,
    cost_spent_usd DECIMAL(10,4) DEFAULT 0.0,
    
    created_by TEXT,
    tags JSON DEFAULT '[]',
    current_phase TEXT
);

-- Permanently failed tasks
CREATE TABLE dead_letter_queue (
    id TEXT PRIMARY KEY,
    original_task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    failure_count INTEGER NOT NULL,
    payload JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    error_patterns JSON DEFAULT '[]',
    suggested_fixes JSON DEFAULT '[]',
    
    FOREIGN KEY(original_task_id) REFERENCES tasks(id)
);
```

### Performance Indexes

```sql
CREATE INDEX idx_tasks_status_priority ON tasks(status, priority DESC);
CREATE INDEX idx_tasks_orchestra      ON tasks(orchestra_id, orchestra_phase);
CREATE INDEX idx_tasks_retry          ON tasks(status, next_retry_at);
CREATE INDEX idx_task_runs_task       ON task_runs(task_id, attempt_number);
CREATE INDEX idx_orchestras_status    ON orchestras(status, created_at);
```

### SQLite Configuration

Applied per connection:

```sql
PRAGMA journal_mode = WAL
PRAGMA synchronous = NORMAL
PRAGMA cache_size = 10000
PRAGMA temp_store = memory
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = 5000
```

## Task State Machine

```
[queued] ──worker_pickup──▶ [running]
    ▲                           │
    │                           ├──▶ [success]
    │                           │
    │                           └──▶ [failed] ──retry_count < max_retries──▶ [retry]
    │                                              │
    └──────────────────────────────────────────────┘ (backoff delay)
                                                   │
                                retry_count >= max_retries
                                                   │
                                                   ▼
                                     [permanently_failed]
                                                   │
                                                   ▼
                                          [dead_letter_queue]
                               
    [cancelled] ← user cancel (from any non-terminal state)
```

### State Definitions

| State | Meaning |
|-------|---------|
| `queued` | Ready for pickup by next available worker |
| `running` | Currently executing |
| `success` | Completed successfully |
| `failed` | Failed this attempt — evaluating for retry |
| `retry` | Scheduled for retry after backoff |
| `permanently_failed` | Exceeded max retries → moved to dead letter |
| `cancelled` | Cancelled by user |

## Priority Levels

```python
class Priority(IntEnum):
    CRITICAL = 1    # Process immediately
    HIGH     = 2    # Before normal tasks
    NORMAL   = 3    # Default
    LOW      = 4    # When no higher priority work
```

Workers always pick the highest priority ready task. Within same priority: FIFO by `created_at`.

## Retry Logic

### Exponential Backoff

```python
def calculate_retry_delay(attempt_number: int) -> int:
    base_delay = 1   # seconds
    max_delay  = 60  # seconds (cap)
    return min(base_delay * (2 ** (attempt_number - 1)), max_delay)

# Attempt 1 →  1s
# Attempt 2 →  2s
# Attempt 3 →  4s
# Attempt 4 →  8s
# Attempt 5+ → 60s
```

### Model Tier Escalation on Retry

```python
ESCALATION_PATHS = {
    'content':     ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
    'code':        ['sonnet-4',  'opus-4-6', 'opus-4-6'],
    'research':    ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
    'translation': ['sonnet-4',  'opus-4-6', 'opus-4-6'],
    'review':      ['sonnet-4',  'opus-4-6', 'opus-4-6'],
}
```

### Default Max Retries Per Task Type

| Task Type | Max Retries |
|-----------|------------|
| content | 3 |
| code | 2 |
| research | 3 |
| translation | 4 |
| review | 2 |

## CLI Operations

```bash
# Submit a task
orch submit --type content --payload '{"topic": "AI agents"}' --priority high

# Check queue / task status
orch status            # overall queue stats
orch status <task-id>  # specific task

# List tasks
orch list
orch list --state queued --type content --priority high
orch list --format json
orch list --limit 10 --offset 20

# Manage tasks
orch cancel <task-id>
orch retry <task-id>

# Dead letter queue
orch dead-letter

# System health
orch health
```

## Programmatic Usage

```python
from orchestration_engine import TaskQueue, TaskSpec, TaskType, Priority
from decimal import Decimal

queue = TaskQueue()

# Submit
task_id = queue.submit_task(TaskSpec(
    type=TaskType.RESEARCH,
    payload={"query": "AI trends 2025"},
    priority=Priority.HIGH,
    max_retries=3,
    min_confidence=0.8,
    tags=["research", "ai"]
))

# Check status
status = queue.get_task_status(task_id)
print(status.state.value)   # 'queued'
print(status.priority.name) # 'HIGH'

# List tasks
from orchestration_engine.schemas import TaskFilters, TaskState
tasks = queue.list_tasks(TaskFilters(
    states=[TaskState.QUEUED, TaskState.RUNNING],
    types=[TaskType.RESEARCH],
    limit=50
))

# Queue health
stats = queue.get_queue_stats()
print(f"Queued: {stats.queued}, Running: {stats.running}")
print(f"Worker utilization: {stats.worker_utilization:.0f}%")
```

## Database Location

Default: `~/.orchestration-engine/engine.db`

Override with `ORCH_DB_PATH` env var or `db_path` in `config.toml`.
