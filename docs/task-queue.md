# Task Queue Design

The task queue is the **heart** of the orchestration engine — a SQLite-backed, persistent, concurrent task scheduling system with state management and retry logic.

## Core Principles

- **Persistence**: Survives process restarts
- **Concurrency**: Up to 8 parallel workers (configurable)
- **Priority**: Critical, high, normal, low priority levels
- **Retry Logic**: Exponential backoff with model tier escalation
- **Dead Letter Queue**: Permanent failure handling
- **State Tracking**: Complete audit trail

## SQLite Schema

### Core Tables

```sql
-- Main task table
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,                    -- UUID v4
    type TEXT NOT NULL,                     -- 'content', 'code', 'research', 'translation', 'review'
    priority INTEGER DEFAULT 3,            -- 1=critical, 2=high, 3=normal, 4=low
    status TEXT DEFAULT 'queued',           -- State machine (see below)
    payload JSON NOT NULL,                 -- Task-specific input data
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,                  -- When first worker picked it up
    completed_at TIMESTAMP,                -- Final completion (success/failure)
    next_retry_at TIMESTAMP,               -- When retry should happen
    
    -- Retry management
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,         -- Configurable per task type
    
    -- Orchestra integration
    orchestra_id TEXT,                     -- Parent workflow ID
    orchestra_phase TEXT,                  -- Phase within orchestra
    
    -- Quality & routing
    min_confidence FLOAT DEFAULT 0.7,     -- Minimum acceptable confidence
    preferred_model TEXT,                  -- Model tier preference
    
    -- Constraints
    timeout_seconds INTEGER DEFAULT 3600, -- Max execution time
    cost_limit_usd DECIMAL(10,4),         -- Budget limit
    
    FOREIGN KEY(orchestra_id) REFERENCES orchestras(id)
);

-- Individual execution attempts
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,       -- 1, 2, 3...
    
    -- Execution context
    model TEXT NOT NULL,                   -- 'haiku-4-5', 'sonnet-4', 'opus-4-6'
    thinking_level TEXT,                   -- 'off', 'low', 'medium', 'high'
    session_id TEXT,                       -- OpenClaw session identifier
    worker_id TEXT,                        -- Worker process ID
    
    -- Timing
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    
    -- Results
    status TEXT NOT NULL,                  -- 'running', 'success', 'failed'
    result JSON,                           -- Structured output (TaskResult schema)
    confidence FLOAT,                      -- Quality score (0.0-1.0)
    error_message TEXT,                    -- Human-readable error
    error_type TEXT,                       -- 'transient', 'permanent', 'quality'
    
    -- Resource usage
    tokens_used INTEGER,
    cost_usd DECIMAL(10,4),
    peak_memory_mb INTEGER,
    
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    UNIQUE(task_id, attempt_number)
);

-- Multi-task workflows
CREATE TABLE orchestras (
    id TEXT PRIMARY KEY,
    template TEXT NOT NULL,               -- 'content-pipeline', 'code-sprint', etc.
    name TEXT,                           -- Human-readable name
    status TEXT DEFAULT 'running',       -- 'running', 'completed', 'failed', 'cancelled'
    
    -- Configuration
    config JSON NOT NULL,                -- Template-specific parameters
    priority INTEGER DEFAULT 3,         -- Inherited by child tasks
    
    -- Progress tracking
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_tasks INTEGER DEFAULT 0,
    completed_tasks INTEGER DEFAULT 0,
    failed_tasks INTEGER DEFAULT 0,
    
    -- Resource limits
    cost_budget_usd DECIMAL(10,4),
    time_budget_hours INTEGER,
    cost_spent_usd DECIMAL(10,4) DEFAULT 0.0
);

-- Dead letter queue for permanently failed tasks
CREATE TABLE dead_letter_queue (
    id TEXT PRIMARY KEY,
    original_task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    failure_count INTEGER NOT NULL,      -- How many times it failed
    payload JSON NOT NULL,               -- Original task data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY(original_task_id) REFERENCES tasks(id)
);
```

### Indexes for Performance

```sql
-- Core query patterns
CREATE INDEX idx_tasks_status_priority ON tasks(status, priority DESC);
CREATE INDEX idx_tasks_orchestra ON tasks(orchestra_id, orchestra_phase);
CREATE INDEX idx_tasks_retry ON tasks(status, next_retry_at) WHERE status = 'retry';
CREATE INDEX idx_task_runs_task ON task_runs(task_id, attempt_number);
CREATE INDEX idx_orchestras_status ON orchestras(status, created_at);

-- Analytics indexes
CREATE INDEX idx_task_runs_model_metrics ON task_runs(model, status, completed_at);
CREATE INDEX idx_tasks_cost_tracking ON tasks(type, created_at, cost_limit_usd);
```

## Task State Machine

```ascii
    [queued] ──worker_pickup──▶ [running]
        ▲                           │
        │                           ▼
        │                    ┌─[success]
        │                    │
        │                    └─[failed] ──check_retries──┐
        │                                                 │
        │                                                 ▼
   [retry] ◀──backoff_delay─────────────── retry_count < max_retries?
        │                                                 │
        │                                                 ▼
        │                                          [permanently_failed]
        │                                                 │
        └────────────────────────────────────────────────▼
                                                  [dead_letter_queue]
```

### State Definitions

- **queued**: Ready for pickup by next available worker
- **running**: Currently executing in OpenClaw sub-agent
- **success**: Completed successfully, passed quality gates
- **failed**: Failed this attempt, evaluating for retry
- **retry**: Scheduled for retry after backoff delay
- **permanently_failed**: Exceeded max retries, moved to dead letter queue

## Priority Levels

```python
class Priority(IntEnum):
    CRITICAL = 1    # Process immediately, bypass normal queue
    HIGH = 2        # Process before normal priority tasks
    NORMAL = 3      # Standard priority (default)
    LOW = 4         # Process when no higher priority work
```

**Priority Processing Rules**:
1. Workers always pick highest priority tasks first
2. Within same priority, FIFO (first in, first out)
3. Critical priority can interrupt low-priority long-running tasks
4. Orchestra tasks inherit parent orchestra priority

## Retry Logic

### Exponential Backoff

```python
def calculate_retry_delay(attempt_number: int) -> int:
    """Calculate delay in seconds for next retry attempt."""
    base_delay = 1  # 1 second base
    max_delay = 60  # 1 minute maximum
    
    delay = min(base_delay * (2 ** (attempt_number - 1)), max_delay)
    return delay

# Examples:
# Attempt 1 -> 1 second
# Attempt 2 -> 2 seconds  
# Attempt 3 -> 4 seconds
# Attempt 4 -> 8 seconds
# Attempt 5+ -> 60 seconds (capped)
```

### Model Tier Escalation

```python
def select_model_tier(task_type: str, attempt_number: int) -> str:
    """Escalate model capability with each retry."""
    
    escalation_paths = {
        'content': ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
        'code': ['sonnet-4', 'opus-4-6', 'opus-4-6'],  # Start higher for code
        'research': ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
        'translation': ['sonnet-4', 'opus-4-6', 'opus-4-6'],  # Quality critical
        'review': ['sonnet-4', 'opus-4-6', 'opus-4-6']
    }
    
    path = escalation_paths.get(task_type, ['haiku-4-5', 'sonnet-4', 'opus-4-6'])
    index = min(attempt_number - 1, len(path) - 1)
    return path[index]
```

### Max Retries Per Task Type

```python
DEFAULT_MAX_RETRIES = {
    'content': 3,      # Research -> Write -> Fact-check retries
    'code': 2,         # Build -> Test retries (usually deterministic)
    'research': 3,     # Multiple sources, citation verification
    'translation': 4,  # Back-translation, cultural nuance important
    'review': 2,       # Subjective, usually passes or fails clearly
}
```

## Concurrency Control

### Worker Pool Management

```python
class WorkerPool:
    def __init__(self, max_workers: int = 8):
        self.max_workers = max_workers
        self.active_workers = {}  # worker_id -> task_id
        self.worker_heartbeats = {}  # worker_id -> last_seen
        
    def can_spawn_worker(self) -> bool:
        return len(self.active_workers) < self.max_workers
        
    def cleanup_stale_workers(self):
        """Remove workers that haven't sent heartbeat in 5 minutes."""
        cutoff = datetime.now() - timedelta(minutes=5)
        stale_workers = [
            worker_id for worker_id, last_seen in self.worker_heartbeats.items()
            if last_seen < cutoff
        ]
        for worker_id in stale_workers:
            self.release_worker(worker_id)
```

### Task Assignment Algorithm

```sql
-- Worker picks next task to execute
SELECT id FROM tasks 
WHERE status = 'queued' 
   OR (status = 'retry' AND next_retry_at <= CURRENT_TIMESTAMP)
ORDER BY 
    CASE 
        WHEN status = 'retry' THEN priority - 0.5  -- Slight boost for retries
        ELSE priority 
    END ASC,
    created_at ASC  -- FIFO within priority
LIMIT 1;
```

### Resource Contention Handling

```python
def check_resource_limits(task: Task) -> bool:
    """Ensure we don't exceed orchestra or system limits."""
    
    # Check orchestra budget
    if task.orchestra_id:
        orchestra = get_orchestra(task.orchestra_id)
        if orchestra.cost_budget_usd:
            if orchestra.cost_spent_usd >= orchestra.cost_budget_usd:
                return False
    
    # Check system-wide OpenClaw session limits
    active_sessions = count_active_openclaw_sessions()
    if active_sessions >= MAX_OPENCLAW_SESSIONS:
        return False
        
    return True
```

## Dead Letter Queue Management

### Automatic Dead Letter Handling

```sql
-- Move permanently failed tasks to dead letter queue
INSERT INTO dead_letter_queue (
    original_task_id,
    task_type,
    failure_reason,
    failure_count,
    payload
)
SELECT 
    id,
    type,
    'Exceeded maximum retry attempts',
    retry_count,
    payload
FROM tasks 
WHERE status = 'permanently_failed';

-- Clean up main tasks table
DELETE FROM tasks WHERE status = 'permanently_failed';
```

### Dead Letter Analysis

```sql
-- Most common failure patterns
SELECT 
    task_type,
    failure_reason,
    COUNT(*) as failure_count,
    AVG(failure_count) as avg_attempts
FROM dead_letter_queue 
GROUP BY task_type, failure_reason
ORDER BY failure_count DESC;

-- Failure trends over time
SELECT 
    DATE(created_at) as failure_date,
    task_type,
    COUNT(*) as daily_failures
FROM dead_letter_queue 
WHERE created_at > DATE('now', '-30 days')
GROUP BY failure_date, task_type
ORDER BY failure_date DESC;
```

## Queue Operations API

### Core Queue Methods

```python
class TaskQueue:
    def submit_task(self, task: TaskSpec) -> str:
        """Submit new task to queue, returns task ID."""
        
    def get_task_status(self, task_id: str) -> TaskStatus:
        """Get current status of specific task."""
        
    def list_tasks(self, filters: TaskFilters) -> List[TaskSummary]:
        """List tasks with optional filtering."""
        
    def cancel_task(self, task_id: str) -> bool:
        """Cancel queued or running task."""
        
    def retry_failed_task(self, task_id: str) -> bool:
        """Manually retry a failed task."""
        
    def get_queue_stats(self) -> QueueStats:
        """Get current queue statistics."""
```

### Queue Statistics

```sql
-- Real-time queue statistics
SELECT 
    status,
    priority,
    COUNT(*) as count,
    MIN(created_at) as oldest_task,
    AVG(JULIANDAY('now') - JULIANDAY(created_at)) * 24 as avg_age_hours
FROM tasks 
WHERE status IN ('queued', 'running', 'retry')
GROUP BY status, priority
ORDER BY priority, status;
```

## Performance Considerations

### SQLite Optimization

1. **WAL Mode**: Enable Write-Ahead Logging for better concurrency
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = 10000;
PRAGMA temp_store = memory;
```

2. **Connection Pool**: Use connection pooling to handle concurrent workers

3. **Batch Operations**: Group related operations in transactions

### Monitoring & Alerting

- **Queue Depth**: Alert when queued tasks > 50
- **Worker Stalls**: Alert when tasks stuck in 'running' > 30 minutes
- **Dead Letter Growth**: Alert when dead letter tasks > 10/day
- **Resource Usage**: Monitor SQLite file size, connection counts

### Scaling Considerations

- **Single Node**: SQLite handles up to ~1000 tasks/hour efficiently
- **Multi-Node**: Consider PostgreSQL + Redis for horizontal scaling
- **Archive Strategy**: Move completed tasks older than 30 days to archive table

## CLI Interface

```bash
# Queue management commands
orch queue status                    # Show queue statistics
orch queue list --status queued     # List queued tasks  
orch queue retry <task-id>          # Manually retry failed task
orch queue cancel <task-id>         # Cancel task
orch queue purge --older-than 30d   # Clean up old completed tasks
orch queue dead-letter list         # Show permanently failed tasks
orch queue workers                  # Show active worker status
```

The task queue provides **reliable, persistent, and scalable task coordination** that survives restarts, handles failures gracefully, and provides complete observability into work execution.