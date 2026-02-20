# Orchestration Engine — Usage Guide

This document covers the complete implemented feature set as of Week 2.

> **Legacy note:** This file was originally called `PHASE1_USAGE.md`. Phase 1 is complete; we're now in Week 2. The CLI, task queue, task runner, error recovery, concurrency, progress tracking, and scenario runner are all implemented and tested (214 tests passing).

## Installation

```bash
cd /home/toscan/orchestration-engine

# Activate venv
source venv/bin/activate

# Or install in development mode
pip install -e .

# Set PYTHONPATH if needed
export PYTHONPATH=./src
```

## CLI Usage

All commands use the `orch` entrypoint (or `python -m orchestration_engine.cli`).

### Submit Tasks

```bash
# Submit a content generation task
orch submit \
  --type content \
  --payload '{"topic": "AI orchestration", "word_count": 1000}' \
  --priority high \
  --tag "blog" --tag "ai"

# Submit a code task with custom settings
orch submit \
  --type code \
  --payload '{"language": "python", "task": "create REST API"}' \
  --priority normal \
  --max-retries 2 \
  --timeout 7200 \
  --min-confidence 0.9

# Available task types: content, code, research, translation, review
# Available priorities: critical, high, normal, low
```

### Check Status

```bash
# Overall queue statistics
orch status

# Specific task status
orch status <task-id>
```

### List Tasks

```bash
# All tasks
orch list

# Filter by state
orch list --state queued
orch list --state running

# Filter by type and priority
orch list --type content --priority high

# JSON output
orch list --format json

# Pagination
orch list --limit 10 --offset 20
```

### Manage Tasks

```bash
orch cancel <task-id>       # Cancel queued or running task
orch retry <task-id>        # Manually retry a failed task
orch dead-letter            # List permanently failed tasks
orch health                 # System health check
```

## Programmatic Usage

```python
from orchestration_engine import TaskQueue, TaskSpec, TaskType, Priority
from decimal import Decimal

queue = TaskQueue()

# Submit a task
task_id = queue.submit_task(TaskSpec(
    type=TaskType.RESEARCH,
    payload={
        "query": "machine learning trends 2025",
        "sources": 10,
        "depth": "comprehensive"
    },
    priority=Priority.HIGH,
    max_retries=3,
    min_confidence=0.8,
    cost_limit_usd=Decimal("25.00"),
    tags=["research", "ml", "trends"]
))

print(f"Task submitted: {task_id}")

# Check task status
status = queue.get_task_status(task_id)
print(f"State: {status.state.value}")
print(f"Priority: {status.priority.name}")
print(f"Created: {status.created_at}")

# List tasks with filters
from orchestration_engine.schemas import TaskFilters, TaskState

tasks = queue.list_tasks(TaskFilters(
    states=[TaskState.QUEUED, TaskState.RUNNING],
    types=[TaskType.RESEARCH],
    limit=50
))
print(f"Found {len(tasks)} matching tasks")

# Queue health
stats = queue.get_queue_stats()
print(f"Queued: {stats.queued}")
print(f"Running: {stats.running}")
print(f"Worker utilization: {stats.worker_utilization:.0f}%")
```

## Task Runner

The task runner polls the queue and executes tasks via one of three executors.

```python
from orchestration_engine.runner import TaskRunner
from orchestration_engine.config import get_global_config

config = get_global_config()
runner = TaskRunner(config=config)

# Start the runner (non-blocking, runs in background thread)
runner.start()

# Execute a specific task immediately
runner.execute_task_immediately(task_id)

# Get runner status
status = runner.get_status()

# Stop cleanly
runner.stop()
```

### Executors

| Executor | When used |
|----------|----------|
| `DryRunExecutor` | Testing — returns mock results |
| `LocalExecutor` | Run shell commands (shlex-safe) |
| `OpenClawExecutor` | File-based contract with OpenClaw sub-agents |

## Error Recovery

Error recovery is automatic — the `RecoveryManager` handles all failure cases.

```python
from orchestration_engine.recovery import RecoveryManager
from orchestration_engine.config import get_global_config
from orchestration_engine.db import Database
from orchestration_engine.schemas import TaskType

db = Database()
config = get_global_config()
recovery = RecoveryManager(db, config)

# Handle a failure (called automatically by TaskRunner)
should_retry, retry_at, next_model = recovery.handle_task_failure(
    task_id="...",
    task_type=TaskType.CONTENT,
    error_message="timeout: task exceeded limit",
    model_tier="haiku-4-5"
)

# Get error statistics
stats = recovery.get_error_statistics()
print(stats["circuit_breakers"])
print(stats["retry_statistics"])
```

## Progress Tracking

```python
from orchestration_engine.progress import ProgressTracker
from orchestration_engine.db import Database

db = Database()
tracker = ProgressTracker(db)

# Get task progress
progress = tracker.get_task_progress(task_id)
print(progress.current_state)
print(progress.events)  # full event log

# Stream events (generator)
for event in tracker.stream_task_events(task_id):
    print(f"{event.event_type}: {event.message}")

# Get all active tasks
active = tracker.get_active_tasks()
```

## Scenario Runner

Evaluate pipeline output against YAML-defined acceptance criteria.

```python
from pathlib import Path
from scenario_runner import ScenarioRunner

runner = ScenarioRunner(scenarios_dir=Path("scenarios/content-pipeline"))

# Run a single scenario
scenario = runner.load_scenario(Path("scenarios/content-pipeline/happy-path-001.yaml"))
result = runner.run_scenario(
    scenario,
    pipeline_output={"article": "Full article text here..."}
)

print(f"Passed: {result.passed}")
print(f"Score: {result.weighted_score:.2f}")
print(f"Gates passed: {result.gates_passed}")

# Run all scenarios in a directory
suite = runner.run_suite(
    suite_dir=Path("scenarios/content-pipeline"),
    pipeline_outputs={
        "content-pipeline-happy-path-001": {"article": "..."},
        "content-pipeline-hallucination-trap-002": {"article": "..."},
    }
)
print(f"Pass rate: {suite.satisfaction_rate:.0%} ({suite.total_scenarios} scenarios)")
```

See `scenario_runner/README.md` and `scenarios/README.md` for details.

## Configuration

Configuration is loaded from `~/.orchestration-engine/config.toml` with environment variable overrides.

```toml
[engine]
max_workers = 4
log_level = "INFO"

[retry]
backoff_base = 1
backoff_max = 60
max_retries_default = 3
circuit_breaker_threshold = 5
circuit_breaker_reset_minutes = 30

[models]
escalation_enabled = true
```

Key environment variables:
- `ORCH_MAX_WORKERS`
- `ORCH_DB_PATH`
- `ORCH_LOG_LEVEL`

## Database

Default location: `~/.orchestration-engine/engine.db`

Core tables: `tasks`, `task_runs`, `orchestras`, `dead_letter_queue`, `workers`
Recovery tables: `retry_attempts`, `circuit_breaker_state`, `error_patterns`
Progress tables: `progress_events`, `task_progress_summary`

## Running Tests

```bash
cd /home/toscan/orchestration-engine
source venv/bin/activate
pytest                        # all 214 tests
pytest tests/test_schemas.py  # schemas only
pytest -v --tb=short          # verbose
```

## What's Implemented (Week 2)

✅ SQLite task queue (WAL mode, priority, retry, dead letter)  
✅ Pydantic V2 schemas (TaskSpec, TaskResult, TaskStatus, etc.)  
✅ CLI (`orch submit/status/list/cancel/retry/dead-letter/health`)  
✅ TOML configuration with env var overrides  
✅ Task Runner with 3 executors: DryRun, Local, OpenClaw  
✅ Error Recovery: classification, exponential backoff, circuit breakers  
✅ Concurrency: configurable worker pool with heartbeat monitoring  
✅ Progress tracking: event recording, task summaries  
✅ Scenario Runner: YAML scenarios, weighted scoring, gates  
✅ 3 graders: assertion (restricted eval), LLM judge, URL check  
✅ 3 content pipeline scenarios + 4 shared rubrics  

## What's NOT Implemented Yet

❌ MCP integration (deferred — v1.0+)  
❌ Memory system (deferred — v1.0+)  
❌ Advanced metrics/analytics (deferred — Week 4-5)  
❌ Template engine / phase sequencer (Week 3)  
❌ Scenario runner CLI (Week 6)  
❌ Digital twin / mock service layer  
❌ LangGraph integration  
