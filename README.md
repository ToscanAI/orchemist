# Orchestration Engine

Local orchestration engine for AI agent coordination — task queue, retry logic, quality gates, orchestra templates. Runs on top of OpenClaw.

## Status

| Phase | Status | PR | Description |
|-------|--------|-----|-------------|
| **Phase 1** | ✅ Complete | [#45](https://github.com/ToscanRivera/orchestration-engine/pull/45) | Task Queue + Schemas + CLI |
| **Phase 2** | 🔜 Next | — | Task Runner + Error Recovery + Concurrency |
| **Phase 3** | 📋 Planned | — | Quality Gates + Orchestra Templates |
| **Phase 4** | 📋 Planned | — | Memory + Metrics + MCP Integration |

## What's Built (Phase 1)

### SQLite Task Queue
Persistent, concurrent task scheduling with full state machine:

```
queued → running → success
                 → failed → retry (exponential backoff)
                          → permanently_failed → dead_letter_queue
```

- WAL mode for concurrent access
- Priority levels: Critical, High, Normal, Low
- Model tier escalation on retry: Haiku → Sonnet → Opus
- Dead letter queue for permanent failures
- Full audit trail via `task_runs` table

### Structured Schemas (Pydantic)
Type-safe data models for the entire system:
- `TaskSpec`, `TaskStatus`, `TaskResult`, `TaskSummary`
- `OrchestraSpec`, `OrchestraStatus`
- `QueueStats`, `TaskFilters`, `DeadLetterTask`
- Enums: `Priority`, `TaskType`, `TaskState`, `ModelTier`

### CLI (`orch`)
```bash
orch submit --type content --priority high '{"topic": "AI orchestration"}'
orch status                    # Queue stats
orch status <task-id>          # Specific task
orch list --state running      # Filter by state
orch list --type code --json   # JSON output
orch cancel <task-id>          # Cancel task
orch retry <task-id>           # Retry failed task
orch dead-letter               # View permanent failures
orch health                    # System health check
```

### Tests
- Schema validation tests (28 cases)
- Queue lifecycle tests (submit → pickup → complete/fail → retry → dead letter)
- All passing on Python 3.12 with pytest

## What's Next (Phase 2)

The Task Runner connects the queue to OpenClaw's `sessions_spawn()`, making tasks actually execute:

- **Task Runner** (#2) — Execute tasks via OpenClaw sub-agents
- **Error Recovery** (#4) — Exponential backoff + model escalation
- **Concurrency Manager** (#5) — Max 8 parallel workers
- **Progress Streaming** (#6) — Real-time status updates
- **Config System** (#7) — TOML configuration

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  CLI (orch)     │────▶│ Task Queue      │────▶│ OpenClaw        │
│  submit/status  │     │ (SQLite)        │     │ sessions_spawn  │
│  list/cancel    │     │                 │     │                 │
│  retry/health   │     │ Priority Queue  │     │ Haiku 4.5       │
└─────────────────┘     │ Retry Logic     │     │ Sonnet 4        │
                        │ Dead Letter     │     │ Opus 4.6        │
┌─────────────────┐     └─────────────────┘     └─────────────────┘
│ Quality Gates   │              │                       │
│ (Phase 3)       │◀─────────────┘                       │
└─────────────────┘                                      ▼
                        ┌─────────────────┐     ┌─────────────────┐
┌─────────────────┐     │ Agent Memory    │     │ Results Store   │
│ Orchestra       │     │ (Phase 4)       │     │ (SQLite)        │
│ Templates       │     └─────────────────┘     └─────────────────┘
│ (Phase 3)       │
└─────────────────┘
```

## Quick Start

```bash
# Clone and install
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
pip install -e .

# Submit a task
orch submit --type content --priority high '{"topic": "AI orchestration"}'

# Check status
orch status

# Run tests
pytest
```

### File Structure
```
orchestration-engine/
├── src/orchestration_engine/
│   ├── __init__.py          # Package init + version
│   ├── cli.py               # Click CLI (485 lines)
│   ├── db.py                # SQLite database layer (694 lines)
│   ├── queue.py             # TaskQueue class (522 lines)
│   └── schemas.py           # Pydantic models (428 lines)
├── tests/
│   ├── test_queue.py        # Queue lifecycle tests (712 lines)
│   └── test_schemas.py      # Schema validation tests (548 lines)
├── docs/                    # Architecture documentation (9 files)
├── pyproject.toml           # Python package config
├── pytest.ini               # Test configuration
├── PHASE1_USAGE.md          # Phase 1 usage guide
└── README.md
```

### Storage
- Database: `~/.orchestration-engine/engine.db`
- Config: `~/.orchestration-engine/config.toml`

## Relationship to OpenClaw

This is a **meta-layer** on top of OpenClaw:

| Layer | Provides | Think of it as... |
|-------|----------|-------------------|
| **OpenClaw** | Sub-agent spawning, tool access, model switching | Operating System |
| **Orchestration Engine** | Task queuing, retry, quality gates, templates | Application Framework |

Integration via `sessions_spawn()` for agent creation and `sessions_send()` for progress updates.

## Documentation

- [Architecture](docs/architecture.md) — System design
- [Task Queue](docs/task-queue.md) — Queue implementation
- [Orchestra Templates](docs/orchestra-templates.md) — Workflow patterns
- [Quality Gates](docs/quality-gates.md) — Verification
- [Error Recovery](docs/error-recovery.md) — Retry + escalation
- [Memory System](docs/memory-system.md) — Agent learning
- [Metrics](docs/metrics.md) — Analytics + cost tracking
- [MCP Integration](docs/mcp-integration.md) — Model Context Protocol

## Issues

[44 issues](https://github.com/ToscanRivera/orchestration-engine/issues) across 8 labels: core, cli, quality, templates, metrics, memory, mcp, documentation.

---

**Built to orchestrate AI agents at scale. No hallucinations. No lost work. Just reliable, coordinated intelligence.**
