# Orchestration Engine

Local orchestration engine for AI agent coordination — task queue, execution pipeline, scenario-based quality testing, and orchestra templates. Runs on top of OpenClaw.

## Status (Updated 2026-02-20)

| Week | Status | PR | Description |
|------|--------|-----|-------------|
| **Phase 1** | ✅ Complete | [#45](https://github.com/ToscanRivera/orchestration-engine/pull/45) | Task Queue + Schemas + CLI |
| **Phase 2** | ✅ Complete | [#46](https://github.com/ToscanRivera/orchestration-engine/pull/46) | Task Runner + Error Recovery + Concurrency + Config |
| **Week 1** | ✅ Complete | [#62](https://github.com/ToscanRivera/orchestration-engine/pull/62) | DB API fix + Security hardening + 179 tests green |
| **Week 2** | ✅ Complete | [#63](https://github.com/ToscanRivera/orchestration-engine/pull/63) | Scenario runner + Graders + 3 scenarios + 214 tests |
| **Week 3** | 📋 Next | — | Template engine + Phase sequencer + First E2E scenario |
| **Week 4-5** | 📋 Planned | — | Full scenario suite (10 scenarios) + Reporting |
| **Week 6** | 📋 Planned | — | CI integration + Documentation |

## What's Built

### Core Infrastructure (Phase 1-2)

**SQLite Task Queue** — Persistent, concurrent task scheduling:
```
queued → running → success
                 → failed → retry (exponential backoff)
                          → permanently_failed → dead_letter_queue
```
- WAL mode + busy_timeout for concurrent access
- Priority levels: Critical, High, Normal, Low
- Model tier escalation on retry: Haiku → Sonnet → Opus
- Dead letter queue for permanent failures

**Task Runner** — Full execution pipeline:
- `DryRunExecutor` for testing (mock execution)
- `LocalExecutor` for shell commands (shlex-safe, no shell injection)
- `OpenClawExecutor` with file-based contract (`~/.orchestration-engine/tasks/{id}/input.json → output.json`)
- Worker pool with configurable concurrency

**Error Recovery** — Intelligent retry with circuit breakers:
- Error classification (transient, rate-limit, permanent, model-specific)
- Exponential backoff with configurable base/max
- Circuit breaker pattern (per task-type + model-tier)
- Thread-safe state management with `threading.Lock`

**Configuration** — TOML-based with env var overrides:
- Model tier mappings and escalation paths
- Queue polling intervals, retry limits
- Resource budgets (cost, time, concurrency)

### Scenario-Based Quality Testing (Week 2)

**Scenario Runner** — Outcome-driven testing:
- Loads YAML scenario definitions
- Grades pipeline output against acceptance criteria
- Weighted scoring with hard gates (all-or-nothing)
- Suite runner with satisfaction rate tracking

**Three Graders:**
| Grader | Purpose | Security |
|--------|---------|----------|
| **Assertion** | Binary pass/fail checks (`len(output) > 100`) | Restricted eval — pattern pre-scan + sandboxed namespace |
| **LLM Judge** | Rubric-based 0.0-1.0 scoring | Holdout enforced — only article + rubric sent to judge |
| **URL Check** | HTTP reachability of cited sources | stdlib only, HEAD→GET fallback, 5s timeout |

**3 Content Pipeline Scenarios:**
- `happy-path-001` — Standard article with sourcing requirements
- `hallucination-trap-002` — METR study topic (catches fabricated statistics)
- `ambiguous-brief-003` — Deliberately vague brief for robustness testing

**4 Shared Rubrics:** factual-accuracy, structural-quality, tone-check, no-fabrication

### Content Pipeline v2.3 (8 Phases)

The orchestration engine's primary consumer is an 8-phase content creation pipeline:

```
Research → Write → [Fact-Check | Flow Review | Red Team] → Consistency → Apply Fixes → Human Review
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                          Phases 3-5 run in parallel
```

- Phase 7 split: 7a mechanical fixes, 7b tone rewrites, 7c companion post red team
- Mandatory 24-hour cool-down before publication
- Cross-domain articles require practitioner persona in red team

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  CLI (orch)     │────▶│ Task Queue      │────▶│ OpenClaw        │
│  submit/status  │     │ (SQLite + WAL)  │     │ sessions_spawn  │
│  list/cancel    │     │                 │     │                 │
└─────────────────┘     │ Priority Queue  │     │ Haiku 4.5       │
                        │ Retry + Circuit │     │ Sonnet 4.6      │
┌─────────────────┐     │ Dead Letter     │     │ Opus 4.6        │
│ Scenario Runner │     └─────────────────┘     └─────────────────┘
│ (graders)       │              │                       │
│ assertion       │◀─────────────┘                       │
│ llm_judge       │                                      ▼
│ url_check       │     ┌─────────────────┐     ┌─────────────────┐
└─────────────────┘     │ Error Recovery  │     │ File Contract   │
                        │ + Circuit Break │     │ input.json →    │
┌─────────────────┐     └─────────────────┘     │ output.json     │
│ Orchestra       │                             └─────────────────┘
│ Templates       │
│ (Week 3)        │
└─────────────────┘
```

## Quick Start

```bash
# Clone and install
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
pip install -e .
pip install pyyaml  # for scenario runner

# Submit a task
orch submit --type content --priority high '{"topic": "AI orchestration"}'

# Check status
orch status

# Run tests
pytest  # 214 tests
```

## File Structure

```
orchestration-engine/
├── src/orchestration_engine/
│   ├── __init__.py        # Package init + version
│   ├── cli.py             # Click CLI
│   ├── config.py          # TOML configuration
│   ├── db.py              # SQLite database layer
│   ├── queue.py           # TaskQueue class
│   ├── runner.py          # TaskRunner + Executors
│   ├── schemas.py         # Pydantic V2 models
│   ├── recovery.py        # Error recovery + circuit breakers
│   ├── concurrency.py     # Worker pool management
│   └── progress.py        # Progress tracking
├── scenario_runner/
│   ├── __init__.py        # Exports
│   ├── models.py          # GradeResult, ScenarioResult, SuiteResult
│   ├── runner.py          # ScenarioRunner (load, grade, suite)
│   └── graders/
│       ├── assertion.py   # Restricted eval grader
│       ├── llm_judge.py   # LLM-as-judge (holdout enforced)
│       └── url_check.py   # HTTP reachability checker
├── scenarios/
│   ├── content-pipeline/  # 3 scenario YAML files
│   └── shared/
│       └── rubrics/       # 4 reusable rubric definitions
├── tests/                 # 214 tests across 7 files
├── docs/                  # Architecture documentation (9 files)
├── pyproject.toml
└── README.md
```

## Milestones

| Milestone | Target | Issues |
|-----------|--------|--------|
| **v0.1 — Engine Works** | Week 1 ✅ | DB fix, real executor, E2E proof |
| **v0.2 — Scenarios Grade** | Week 2 ✅ | Scenario runner, graders, 3 scenarios |
| **v0.3 — Pipeline Runs** | Week 3 | Template engine, phase sequencer, first E2E scenario |
| **v0.4 — Confidence** | Week 4-5 | 10 scenarios, reporting, multi-trial, calibration |
| **v0.5 — Integrated** | Week 6 | CI integration, git workflow, docs |
| **v1.0 — Production** | Week 8-12 | Parallel execution, translation pipeline, memory |

## Strategy

**Outcome-driven development:** Scenarios define what "done" looks like. Infrastructure is built until scenarios pass. This replaces unit-test-first thinking with system-level acceptance criteria.

Key documents:
- [Scenario Strategy](docs/orchestration-engine-scenario-strategy.md)
- [Architecture Audit v2](docs/orchestration-engine-audit-v2.md)

## Documentation

- [Architecture](docs/architecture.md) — System design
- [Task Queue](docs/task-queue.md) — Queue implementation
- [Orchestra Templates](docs/orchestra-templates.md) — Workflow patterns (v2.3)
- [Quality Gates](docs/quality-gates.md) — Verification
- [Error Recovery](docs/error-recovery.md) — Retry + escalation
- [Metrics](docs/metrics.md) — Analytics + cost tracking

## Relationship to OpenClaw

| Layer | Provides | Think of it as... |
|-------|----------|-------------------|
| **OpenClaw** | Sub-agent spawning, tool access, model switching | Operating System |
| **Orchestration Engine** | Task queuing, retry, quality gates, templates | Application Framework |
| **Scenario Runner** | Outcome-based testing, LLM judges, satisfaction tracking | Test Framework |

---

**214 tests. 3 scenarios. 4 rubrics. Zero hallucinations tolerated.**
