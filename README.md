# Orchestration Engine

A scenario-driven orchestration engine for multi-agent AI pipelines. Define workflows in YAML, execute with real AI models, grade results against acceptance criteria.

Built by [Conny Lazo](https://connylazo.com) and [Toscan](https://github.com/ToscanRivera) (an AI agent).

## Quick Start

### Prerequisites

- **Python 3.10+** required
- Git

> **Linux/Mac note:** Use `python3` instead of `python` if your system doesn't alias it.

> **Ubuntu/Debian note:** You may need to install the venv package first:
> ```bash
> sudo apt install python3.12-venv  # adjust version to match your python3 --version
> ```

### Install

```bash
# Clone the repo
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine

# Create a virtual environment (recommended)
python3 -m venv .venv

# Activate it
# Linux/Mac:
source .venv/bin/activate
# Windows (cmd):
.venv\Scripts\activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Install the engine
pip install .
```

### Verify

```bash
orch --help
```

You should see a list of commands: `run`, `submit`, `status`, `list`, `cancel`, `validate`.

### Dry Run (no API key needed)

```bash
orch run examples/hello-pipeline.yaml --mode dry-run --input '{"name": "World"}'
```

On Windows, use double quotes for the JSON:
```cmd
orch run examples/hello-pipeline.yaml --mode dry-run --input "{\"name\": \"World\"}"
```

**Expected:** Pipeline loads → validates → completes 2 phases. No API calls made.

### Real Run (requires Anthropic API key)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# Windows: set ANTHROPIC_API_KEY=sk-ant-...

orch run examples/hello-pipeline.yaml --mode standalone --api-key $ANTHROPIC_API_KEY --input '{"name": "René"}'
```

This makes real API calls to Claude. Each phase runs sequentially, producing text output.

### Run Tests

```bash
pip install -e ".[test]"
pytest        # 308 tests
pytest -v     # verbose output
```

---

## How It Works

Define a pipeline in YAML → the engine loads it, resolves dependencies, and runs each phase through an AI model → output from one phase feeds into the next.

**Example pipeline (`examples/hello-pipeline.yaml`):**

```yaml
name: hello-pipeline
description: A simple 2-phase greeting pipeline

phases:
  greet:
    prompt: "Say hello to {{name}} in a creative way."
    model: sonnet

  summarize:
    prompt: "Summarize this greeting in one sentence: {{greet.output}}"
    model: sonnet
    depends_on: [greet]
```

**Three execution modes:**

| Mode | What it does | API key needed? |
|------|-------------|-----------------|
| `dry-run` | Validates pipeline, no API calls | No |
| `standalone` | Runs with Anthropic API directly | Yes |
| `openclaw` | Runs via OpenClaw sub-agent spawning | No (uses OpenClaw) |

---

## Status

| Milestone | Status | PR | Description |
|-----------|--------|-----|-------------|
| **v0.1 — Engine Works** | ✅ Complete | [#62](https://github.com/ToscanRivera/orchestration-engine/pull/62) | DB API + Security hardening + 179 tests |
| **v0.2 — Scenarios Grade** | ✅ Complete | [#63](https://github.com/ToscanRivera/orchestration-engine/pull/63) | Scenario runner + 3 graders + 3 scenarios + 214 tests |
| **v0.3 — Pipeline Runs** | ✅ Complete | [#64](https://github.com/ToscanRivera/orchestration-engine/pull/64) | Template engine + Phase sequencer + `orch run` E2E + 308 tests |
| **v0.4 — Confidence** | 📋 Next | — | 10 scenarios, reporting, multi-trial, calibration |
| **v0.5 — Integrated** | 📋 Planned | — | CI integration, git workflow |

---

## What's Built

### Pipeline Engine (v0.3)

- **Template engine** — YAML pipeline definitions with `{{variable}}` interpolation and topological sort
- **Phase sequencer** — Runs phases in dependency order, forwards output between phases, aborts on failure
- **PipelineRunner** — 3 factory methods: `standalone()`, `openclaw()`, `dry_run()`
- **AnthropicExecutor** — Real API executor, zero framework dependencies (stdlib `urllib` only)
- **CLI** — `orch run <template> --mode standalone|openclaw|dry-run --api-key --input --output-dir`

### Task Queue & Execution (v0.1–v0.2)

- **SQLite task queue** — WAL mode, priority levels, retry with exponential backoff
- **Error recovery** — Circuit breakers, model tier escalation (Haiku → Sonnet → Opus)
- **3 executors** — DryRun, Local (shell), OpenClaw (file-based contract)

### Scenario-Based Testing (v0.2)

- **Scenario runner** — YAML definitions, weighted scoring, hard gates
- **3 graders:** Assertion (restricted eval), LLM Judge (holdout-enforced), URL Check
- **3 scenarios + 4 rubrics** for content pipeline testing

---

## Architecture

```
CLI (orch run)
    │
    ▼
Template Engine ──▶ Phase Sequencer ──▶ Executor
  (YAML parse)      (dependency order)    │
  (var interpolation) (output forwarding) ├─ AnthropicExecutor (API)
                                          ├─ OpenClawExecutor (sub-agents)
                                          └─ DryRunExecutor (testing)
    │
    ▼
Scenario Runner ──▶ Graders
  (acceptance test)   ├─ Assertion (eval)
                      ├─ LLM Judge (rubric)
                      └─ URL Check (HTTP)
```

---

## File Structure

```
orchestration-engine/
├── src/orchestration_engine/
│   ├── cli.py              # Click CLI (run, submit, status, list, cancel, validate)
│   ├── pipeline_runner.py  # PipelineRunner — standalone(), openclaw(), dry_run()
│   ├── templates.py        # YAML template loading + topological sort
│   ├── sequencer.py        # Phase sequencer with output forwarding
│   ├── executors/
│   │   └── anthropic_executor.py  # Direct Anthropic API (stdlib only)
│   ├── schemas.py          # Pydantic V2 models
│   ├── db.py               # SQLite database layer
│   ├── queue.py            # TaskQueue
│   ├── runner.py           # TaskRunner + legacy executors
│   ├── recovery.py         # Error recovery + circuit breakers
│   ├── config.py           # TOML configuration
│   └── ...
├── scenario_runner/
│   ├── runner.py           # ScenarioRunner
│   └── graders/            # assertion, llm_judge, url_check
├── scenarios/              # 3 scenario YAMLs + 4 rubrics
├── templates/              # Pipeline templates (content-pipeline.yaml)
├── examples/               # hello-pipeline.yaml (smoke test)
├── tests/                  # 308 tests
├── docs/                   # Architecture, API reference, strategy docs
├── pyproject.toml          # v0.3.0, MIT license
└── README.md
```

---

## Documentation

- [Getting Started](docs/getting-started.md) — Detailed setup guide
- [Architecture](ARCHITECTURE.md) — System design
- [API Reference](docs/api-reference.md) — CLI commands + Python classes
- [Tech Stack](docs/tech-stack.md) — Dependencies and choices
- [Scenario Strategy](docs/orchestration-engine-scenario-strategy.md) — Testing philosophy

---

## Relationship to OpenClaw

| Layer | Provides | Think of it as... |
|-------|----------|-------------------|
| **OpenClaw** | Sub-agent spawning, tool access, model switching | Operating System |
| **Orchestration Engine** | Pipeline templates, phase sequencing, quality gates | Application Framework |
| **Scenario Runner** | Outcome-based testing, LLM judges, satisfaction tracking | Test Framework |

The engine works **standalone** (direct API) or **with OpenClaw** (sub-agent spawning). No vendor lock-in.

---

## License

MIT

---

**308 tests. 3 scenarios. 4 rubrics. Zero hallucinations tolerated.**
