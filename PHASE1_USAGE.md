# Phase 1 Usage Examples

This document demonstrates the completed Phase 1 functionality: Task Queue and Structured Schemas.

## Installation

```bash
# Set up environment
export PYTHONPATH=./src

# Or install in development mode (requires venv)
pip install -e .
```

## CLI Usage

### Submit Tasks

```bash
# Submit a content generation task
python -m orchestration_engine.cli submit \
  --type content \
  --payload '{"topic": "AI orchestration", "word_count": 1000, "audience": "developers"}' \
  --priority high \
  --tag "blog" --tag "ai"

# Submit a code task with custom settings
python -m orchestration_engine.cli submit \
  --type code \
  --payload '{"language": "python", "task": "create REST API"}' \
  --priority normal \
  --max-retries 2 \
  --timeout 7200 \
  --min-confidence 0.9
```

### Check Queue Status

```bash
# Overall queue statistics
python -m orchestration_engine.cli status

# Specific task status
python -m orchestration_engine.cli status <task-id>
```

### List Tasks

```bash
# List all tasks
python -m orchestration_engine.cli list

# List only queued tasks
python -m orchestration_engine.cli list --state queued

# List content tasks with high priority
python -m orchestration_engine.cli list --type content --priority high

# List tasks in JSON format
python -m orchestration_engine.cli list --format json

# List with pagination
python -m orchestration_engine.cli list --limit 10 --offset 20
```

### Manage Tasks

```bash
# Cancel a task
python -m orchestration_engine.cli cancel <task-id>

# Retry a failed task
python -m orchestration_engine.cli retry <task-id>

# View dead letter queue
python -m orchestration_engine.cli dead-letter

# Check system health
python -m orchestration_engine.cli health
```

## Programmatic Usage

```python
from orchestration_engine import TaskQueue, TaskSpec, TaskType, Priority
from decimal import Decimal

# Initialize queue
queue = TaskQueue()

# Submit a task
task_spec = TaskSpec(
    type=TaskType.RESEARCH,
    payload={
        "query": "machine learning trends 2024",
        "sources": 10,
        "depth": "comprehensive"
    },
    priority=Priority.HIGH,
    max_retries=3,
    min_confidence=0.8,
    cost_limit_usd=Decimal("25.00"),
    tags=["research", "ml", "trends"]
)

task_id = queue.submit_task(task_spec)
print(f"Task submitted: {task_id}")

# Check task status
status = queue.get_task_status(task_id)
print(f"Task state: {status.state.value}")
print(f"Priority: {status.priority.name}")
print(f"Created: {status.created_at}")

# List tasks with filters
from orchestration_engine.schemas import TaskFilters, TaskState

filters = TaskFilters(
    states=[TaskState.QUEUED, TaskState.RUNNING],
    types=[TaskType.RESEARCH],
    limit=50
)

tasks = queue.list_tasks(filters)
print(f"Found {len(tasks)} matching tasks")

# Get queue statistics
stats = queue.get_queue_stats()
print(f"Queued: {stats.queued}")
print(f"Running: {stats.running}")  
print(f"Total: {stats.total_tasks}")
print(f"Worker utilization: {stats.worker_utilization:.1f}%")
```

## Database Schema

Tasks are stored in SQLite with the following structure:

- `~/.orchestration-engine/engine.db` (default location)
- WAL mode enabled for better concurrency
- Proper indexes for performance
- Foreign key constraints enforced

### Core Tables

- **tasks**: Main task queue with state management
- **task_runs**: Individual execution attempts with results
- **orchestras**: Multi-task workflow coordination
- **dead_letter_queue**: Permanently failed tasks

## Task Lifecycle

```
[queued] → [running] → [success]
    ↑          ↓
    ←── [retry] ←─── [failed]
                        ↓
              [permanently_failed] → [dead_letter_queue]
```

## Features Implemented

✅ **Task Queue**
- SQLite-backed persistent storage
- Priority-based scheduling
- State machine with retry logic
- Dead letter queue
- Worker concurrency support

✅ **Structured Schemas** 
- Pydantic models for all data structures
- Type safety and validation
- JSON serialization/deserialization
- Confidence level auto-calculation

✅ **CLI Interface**
- Complete command set
- JSON and table output formats
- Filtering and pagination
- Health monitoring

✅ **Database Layer**
- Connection pooling
- Transaction management
- Proper indexing
- Migration support

✅ **Comprehensive Tests**
- 28 schema validation tests
- Full task lifecycle coverage
- Database integration tests
- Error condition handling

## Next Steps (Phase 2+)

- Task execution engine with OpenClaw integration
- Orchestra templates and workflows
- Quality gates and verification
- Worker process management
- Metrics and monitoring dashboard

The foundation is solid and ready for building the execution layer on top!