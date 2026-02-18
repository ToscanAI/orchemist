# Orchestration Engine

Local orchestration engine for AI agent coordination — task queue, retry logic, quality gates, orchestra templates. Runs on top of OpenClaw.

## Vision

OpenClaw provides the musicians (sub-agents). This orchestration engine provides the conductor's score — the queue, retry logic, quality gates, and reusable orchestra templates that coordinate complex multi-agent workflows.

## Architecture Overview

```ascii
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Orchestra CLI  │────▶│ Task Queue      │────▶│ OpenClaw        │
│  orch submit    │     │ (SQLite)        │     │ Sub-Agents      │
│  orch run       │     │                 │     │                 │
│  orch status    │     │ ┌─────────────┐ │     │ ┌─────────────┐ │
└─────────────────┘     │ │Queued Tasks │ │     │ │  Haiku 4.5  │ │
                        │ │Running Tasks│ │     │ │  Sonnet 4   │ │
┌─────────────────┐     │ │Failed Tasks │ │     │ │  Opus 4.6   │ │
│ Quality Gates   │◀────│ └─────────────┘ │     │ └─────────────┘ │
│ - Code: Build   │     │                 │     │                 │
│ - Content: Fact │     │ ┌─────────────┐ │     │ ┌─────────────┐ │
│ - Research: Cite│     │ │ Retry Logic │ │     │ │ Progress    │ │
└─────────────────┘     │ │ Exp Backoff │ │     │ │ Streaming   │ │
                        │ │ Model Tiers │ │     │ │ sessions_   │ │
┌─────────────────┐     │ └─────────────┘ │     │ │ send        │ │
│Orchestra Templates     └─────────────────┘     │ └─────────────┘ │
│- Content Pipeline                              └─────────────────┘
│- Code Sprint                                            │
│- Deep Research                                          ▼
│- Translation                              ┌─────────────────┐
│- Security Audit       ┌─────────────────┐ │ Persistent      │
└─────────────────┘     │ Agent Memory    │ │ Results         │
                        │ - Episodic      │ │ - SQLite        │
                        │ - Semantic      │ │ - Metrics       │
                        │ - Procedural    │ │ - Cost Tracking │
                        └─────────────────┘ └─────────────────┘
```

## Key Components

### Task Queue
SQLite-backed persistent queue with states: queued → running → success/failed/retry. Supports priority levels, concurrency control, and dead letter queue for failed tasks.

### Quality Gates
Per-task-type verification:
- **Code**: Build passes, tests pass, lint clean
- **Content**: Fact-check agent, citation verification, word count
- **Research**: Source count, citation URLs valid, confidence levels
- **Translation**: Back-translation divergence score, reviewer score thresholds

### Orchestra Templates
Reusable multi-agent coordination patterns:
- **Content Pipeline**: Research → Write → Fact-Check → Fix → Human Review
- **Code Sprint**: Parallel workers with git integration, test verification
- **Deep Research**: Multi-source search → synthesis → citation verification
- **Translation Pipeline**: Voice calibration → translate → back-translate → review
- **Security Audit**: Scan → analyze → report → remediate

### Error Recovery
- Exponential backoff: 1s, 2s, 4s, 8s, max 60s
- Model tier escalation: Haiku→Sonnet→Opus
- Fallback chains per task type
- Dead letter queue for permanent failures

### Agent Memory
- **Episodic**: Past task executions, outcomes, lessons learned
- **Semantic**: Knowledge base, facts
- **Procedural**: Learned task patterns
- Cross-session learning with SQLite + optional vector embeddings

## Relationship to OpenClaw

This orchestration engine is a **meta-layer** that sits on top of OpenClaw:

1. **OpenClaw provides**: Sub-agent spawning, tool access, model switching, session management
2. **Orchestration Engine provides**: Task queuing, retry logic, quality gates, workflow templates
3. **Integration**: Uses `sessions_spawn()` to create OpenClaw sub-agents, `sessions_send()` for progress updates

Think of it as **OpenClaw = Operating System**, **Orchestration Engine = Application Framework**.

## Quick Start

```bash
# Install orchestration engine
pip install -e .

# Submit a task to the queue
orch submit --type content --template "blog-post" --priority high "Write about AI orchestration"

# Run an orchestra template
orch run content-pipeline --topic "Future of AI Agents" --word-count 2000

# Check queue status
orch status

# View metrics
orch metrics show

# Health check
orch health
```

## Documentation

- [Architecture](docs/architecture.md) - Complete system design
- [Task Queue](docs/task-queue.md) - Queue implementation details
- [Orchestra Templates](docs/orchestra-templates.md) - Workflow patterns
- [Quality Gates](docs/quality-gates.md) - Verification processes
- [Memory System](docs/memory-system.md) - Agent learning & persistence
- [Tech Stack](docs/tech-stack.md) - Implementation decisions
- [Getting Started](docs/getting-started.md) - Setup guide
- [API Reference](docs/api-reference.md) - CLI commands & APIs

## Contributing

This orchestration engine is designed for **reproducible, high-quality multi-agent workflows**. Every task type has structured schemas, every workflow has quality gates, every failure has recovery paths.

See [issues](https://github.com/ToscanRivera/orchestration-engine/issues) for current development priorities.

---

**Built to orchestrate AI agents at scale. No hallucinations. No lost work. Just reliable, coordinated intelligence.**